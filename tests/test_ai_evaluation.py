"""Real local HTTP, SSE, and WebSocket exchanges exercise multi-turn quality and concurrency."""
import base64
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill.engines.ai_eval import AiEvaluationAdapter
from autoskill.engines.ai_oracles import reference_value


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def reset(self):
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.received = []


class ChatHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.received.append({**body, "cookie": self.headers.get("Cookie")})
            self.server.active += 1
            self.server.maximum = max(self.server.maximum, self.server.active)
        try:
            self.respond(body)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            with self.server.lock:
                self.server.active -= 1

    def respond(self, body):
        question = body["question"]
        answer = {"sales": 400, "north": 100, "south": 300, "wrong": -1, "clarify": "Which region?"}.get(question, 400)
        if question == "context":
            answer = body["messages"][0]["content"] if len(body["messages"]) == 3 else "lost"
        if question == "cookie":
            answer = "remembered" if self.headers.get("Cookie") else "lost"
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: heartbeat\ndata: {"type":"heartbeat"}\n\n')
            self.wfile.flush()
            time.sleep(body.get("delay", 0.02))
            content = json.dumps({"delta": json.dumps(answer)})
            self.wfile.write(f"data: {content}\n\n".encode())
            if not body.get("truncate"):
                self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            time.sleep(body.get("delay", 0.01))
            conversation_id = body.get("conversation_id") or "server-" + uuid.uuid4().hex
            data = json.dumps({"answer": answer, "conversation_id": conversation_id, "query": {"question": question}}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Set-Cookie", "chat=active; Path=/")
            self.end_headers()
            self.wfile.write(data)


class AiEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ChatServer(("127.0.0.1", 0), ChatHandler)
        cls.server.reset()
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/chat"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server.reset()
        self.adapter = AiEvaluationAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, spec, name="evidence"):
        source = self.root / "spec.json"
        source.write_text(json.dumps(spec), encoding="utf-8")
        return self.adapter.prepare({"id": "chat", "argv": ["spec.json"], "cwd": "."}, self.root, self.root / name, {})

    def spec(self, turns=None, **extra):
        return {"endpoint": {"url": self.url, "body": {"question": "{{question}}", "messages": "{{messages}}"},
                             "extract": {"conversation_id": "/conversation_id"}, "query_pointer": "/query"},
                "samples": [{"id": "sales-chat", "turns": turns or [{"question": "sales", "expected": 400}]}], **extra}

    def test_multiple_turns_are_one_case_with_shared_history_cookies_and_evidence(self):
        spec = self.spec([{"question": "sales", "expected": 400}, {"question": "context", "expected": "sales"},
                          {"question": "cookie", "expected": "remembered"}])
        prepared = self.prepare(spec)
        result = self.adapter.collect(prepared, self.adapter.execute(prepared))
        self.assertEqual((result["status"], result["business_status"]), ("Pass", "Pass"))
        self.assertEqual(len(result["cases"]), 1)
        case = result["cases"][0]
        self.assertEqual((len(case["turns"]), case["expected_turns"]), (3, 3))
        self.assertEqual([len(body["messages"]) for body in self.server.received], [1, 3, 5])
        self.assertTrue(result["evidence"][0]["sha256"])
        self.assertEqual(case["turns"][0]["query"], {"question": "sales"})

    def test_intermediate_failure_is_not_erased_by_the_last_successful_turn(self):
        result = self.adapter.execute(self.prepare(self.spec([{"question": "sales", "expected": 400},
                                                              {"question": "wrong", "expected": 100}, {"question": "south", "expected": 300}])))
        self.assertEqual(result["status"], "Fail")
        self.assertEqual([turn["status"] for turn in result["cases"][0]["turns"]], ["Pass", "Fail", "Pass"])
        self.assertEqual(result["metrics"]["quality_pass_rate"], 0)

    def test_reference_aggregation_uses_frozen_dataset_not_observed_response(self):
        data = [{"region": "north", "sales": 40}, {"region": "north", "sales": 60}, {"region": "south", "sales": 300}, {"region": "north", "sales": None}]
        reference = {"operation": "sum", "field": "sales", "where": [{"field": "region", "value": "north"}]}
        result = self.adapter.execute(self.prepare(self.spec([{"question": "north", "reference": reference}], dataset=data)))
        self.assertEqual(result["status"], "Pass")
        check = result["cases"][0]["assertions"][0]
        self.assertEqual((check["expected"], check["source"]), (100, "frozen_dataset"))
        self.assertEqual(reference_value(data, {"operation": "sum", "field": "sales", "group_by": "region"}), {"north": 100, "south": 300})

    def test_native_concurrency_uses_separate_conversations_and_aggregates_every_sample(self):
        spec = self.spec(load={"concurrency": 3, "iterations": 6})
        spec["endpoint"]["body"].update(delay=0.05, conversation_id="{{conversation_id}}")
        result = self.adapter.execute(self.prepare(spec))
        self.assertEqual(result["metrics"]["completed_cases"], 6)
        self.assertEqual(len({case["conversation_id"] for case in result["cases"]}), 6)
        self.assertGreaterEqual(self.server.maximum, 2)
        self.assertIsNotNone(result["metrics"]["p95_ms"])
        self.assertEqual(result["metrics"]["quality_pass_rate"], 1)

    def test_sse_heartbeat_is_not_first_answer_and_truncation_fails(self):
        spec = self.spec()
        spec["endpoint"].update(protocol="sse", body={"question": "{{question}}", "stream": True, "delay": 0.08})
        result = self.adapter.execute(self.prepare(spec))
        turn = result["cases"][0]["turns"][0]
        self.assertEqual(result["status"], "Pass")
        self.assertGreater(turn["timings"]["first_content_ms"] - turn["timings"]["first_byte_ms"], 50)
        spec["endpoint"]["body"]["truncate"] = True
        truncated = self.adapter.execute(self.prepare(spec, "truncated"))
        self.assertEqual(truncated["status"], "Fail")
        self.assertIn("完成事件", truncated["reason"])

    def test_idle_and_first_answer_timeout_are_distinct_and_cancellation_is_prompt(self):
        spec = self.spec()
        spec["endpoint"].update(protocol="sse", body={"question": "{{question}}", "stream": True, "delay": 0.3},
                                timeouts={"first_content": 0.05, "idle": 2, "total": 4})
        result = self.adapter.execute(self.prepare(spec))
        self.assertIn("首个可见", result["reason"])
        spec["endpoint"]["timeouts"]["first_content"] = 3
        prepared = self.prepare(spec, "cancel")
        began = time.monotonic()
        result = self.adapter.execute(prepared, cancelled=lambda: time.monotonic() - began > 0.08)
        self.assertEqual(result["status"], "Cancelled")
        self.assertLess(time.monotonic() - began, 0.5)

    def test_server_generated_conversation_id_is_bound_to_later_turn(self):
        spec = self.spec([{"question": "sales", "expected": 400}, {"question": "north", "expected": 100,
                          "endpoint": {"body": {"question": "{{question}}", "conversation_id": "{{conversation_id}}"}}}])
        result = self.adapter.execute(self.prepare(spec))
        self.assertEqual(result["status"], "Pass")
        self.assertTrue(self.server.received[1]["conversation_id"].startswith("server-"))
        self.assertEqual(result["cases"][0]["turns"][0]["bindings"]["conversation_id"], self.server.received[1]["conversation_id"])

    def test_transport_success_without_quality_checks_is_not_verified_business_pass(self):
        result = self.adapter.execute(self.prepare(self.spec([{"question": "sales"}])))
        self.assertEqual((result["status"], result["business_status"], result["verification"]), ("Pass", "Unverified", "unverified"))
        self.assertEqual(result["metrics"]["quality_pass_rate"], 0)

    def test_later_invalid_oracle_keeps_completed_failure_and_response_journal(self):
        spec = self.spec()
        spec["samples"] = [{"id": "known-failure", "question": "wrong", "expected": 400},
                           {"id": "bad-config", "question": "sales", "reference": {"operation": "invalid"}}]
        prepared = self.prepare(spec)
        result = self.adapter.collect(prepared, self.adapter.execute(prepared))
        self.assertEqual(result["status"], "Fail")
        self.assertEqual({case["id"]: case["status"] for case in result["cases"]}, {"known-failure:1": "Fail", "bad-config:1": "Blocked"})
        journal = [json.loads(file.read_bytes()) for file in (Path(prepared["evidence"]) / "conversation-journal").glob("*-turn-*.json")]
        self.assertEqual(len(journal), 2)
        self.assertEqual({item["state"] for item in journal}, {"completed", "observed"})
        self.assertTrue(all(item["request"]["body_hash"] for item in journal))

    def test_recovery_after_interruption_keeps_known_failure_without_replaying_requests(self):
        spec = self.spec([{"question": "wrong", "expected": 400}])
        prepared = self.prepare(spec)
        self.assertEqual(self.adapter.execute(prepared)["status"], "Fail")
        requests = len(self.server.received)
        recovered = self.adapter.recover(prepared, RuntimeError("worker stopped"))
        self.assertEqual((recovered["status"], recovered["business_status"]), ("Fail", "Fail"))
        self.assertEqual(len(self.server.received), requests)

    def _host_review(self, prepared, choose):
        from autoskill import dialogue_judge

        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "judge-workspace")}), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.adapter.execute, prepared)
            reviewed = []
            deadline = time.monotonic() + 8
            while not pending.done() and time.monotonic() < deadline:
                task = dialogue_judge.next_judgement(idempotency_key=uuid.uuid4().hex, owner="independent-reviewer")["task"]
                if task:
                    reviewed.append(task)
                    dialogue_judge.submit(task["judge_id"], attempt_id=task["attempt_id"], lease_token=task["lease_token"],
                                          idempotency_key=uuid.uuid4().hex, judgement=choose(task))
                else:
                    time.sleep(0.02)
            return pending.result(timeout=2), reviewed

    def test_independent_host_accepts_necessary_clarification_and_continues_the_same_case(self):
        spec = self.spec([{"question": "clarify", "expected": 100, "expected_phase": "final_answer"}], judge={"enabled": True, "timeout_seconds": 2})
        spec["samples"][0]["context"] = {"region": "north"}

        def review(task):
            if task["turn"] == 1:
                return {"verdict": "needs_clarification", "action": "reply", "reason": "目标尚未说明地区，补充已有测试事实后继续",
                        "clarification": {"necessary": True, "relevant": True, "already_answered": False},
                        "facts_used": ["/case/context/region"], "reply": "north", "evidence_refs": ["/answer", "/prompt"]}
            return {"verdict": "pass", "action": "finish", "reason": "明确地区后返回独立参考中的 100", "evidence_refs": ["/answer", "/frozen_reference/expected"]}

        result, reviewed = self._host_review(self.prepare(spec), review)
        self.assertEqual((result["status"], result["business_status"]), ("Pass", "Pass"), result)
        self.assertEqual(len(result["cases"]), 1)
        self.assertEqual(len(result["cases"][0]["turns"]), 2)
        self.assertTrue(result["cases"][0]["turns"][1]["dynamic"])
        self.assertEqual(len(reviewed), 2)
        self.assertEqual(reviewed[1]["input"]["history"][1]["content"], "Which region?")

    def test_independent_host_rejects_repeated_question_without_erasing_the_failure(self):
        spec = self.spec([{"question": "clarify"}], judge={"enabled": True, "timeout_seconds": 2})

        def review(_task):
            return {"verdict": "fail", "action": "finish", "reason": "地区已在上文提供，反问重复且未推进目标",
                    "clarification": {"necessary": False, "relevant": True, "already_answered": True}, "evidence_refs": ["/answer", "/prompt"]}

        result, _ = self._host_review(self.prepare(spec), review)
        self.assertEqual((result["status"], result["business_status"]), ("Fail", "Fail"))
        self.assertEqual(result["cases"][0]["assertions"][0]["source"], "host_ai_judge")

    def test_continue_adds_a_turn_and_host_clarification_cannot_erase_response_failure(self):
        spec = self.spec([{"question": "wrong", "expected": 400}], judge={"enabled": True, "timeout_seconds": 2})

        def review(task):
            if task["turn"] == 1:
                return {"verdict": "uncertain", "action": "continue", "reason": "需要继续回答原问题", "evidence_refs": ["/answer", "/prompt"]}
            return {"verdict": "pass", "action": "finish", "reason": "后续回答符合预期", "evidence_refs": ["/answer", "/frozen_reference/expected"]}

        result, _ = self._host_review(self.prepare(spec), review)
        self.assertEqual((result["status"], result["business_status"]), ("Fail", "Fail"))
        self.assertEqual([turn["status"] for turn in result["cases"][0]["turns"]], ["Fail", "Pass"])
        from autoskill.engines import ai_judging

        checks = [{"passed": False, "source": "explicit"}]
        self.assertEqual(ai_judging.merge(checks, "Fail", {"verdict": "needs_clarification", "action": "reply"}), (checks, "Fail"))

    def test_missing_host_review_remains_pending_and_never_invents_a_pass(self):
        spec = self.spec(judge={"enabled": True, "timeout_seconds": 0.15})
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "no-host")}):
            result = self.adapter.execute(self.prepare(spec))
        self.assertEqual((result["status"], result["business_status"]), ("Blocked", "Unverified"))
        self.assertTrue(result["cases"][0]["turns"][0]["judgement"]["judge_id"])

    def test_native_execution_preserves_earlier_failures_and_explicit_host_routing(self):
        from autoskill import native_assets, native_run, projects

        spec = self.spec()
        spec["samples"] = [{"id": "failed", "question": "wrong", "expected": 100},
                           {"id": "invalid", "question": "sales", "reference": {"operation": "invalid"}}]
        self.prepare(spec)
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "native-workspace"), "AUTOSKILL_HEADED": "0"}):
            pid = projects.create("AI 原生评估", self.url)["id"]
            asset = native_assets.import_asset(pid, str(self.root / "spec.json"), "ai-eval")
            run = native_run.enqueue(pid, asset["id"], host="dsh", host_session_id="host-session-a")
            manifest = json.loads(Path(run["input_manifest"]).read_bytes())
            self.assertEqual(manifest["runtime_context"], {"host": "dsh", "host_session_id": "host-session-a"})
            native_run.execute(run["id"])
            result = native_run.get(run["id"])
        self.assertEqual(result["status"], "Fail")
        self.assertEqual({row["case_id"]: row["status"] for row in result["results"]}, {"failed:1": "Fail", "invalid:1": "Fail"})
        self.assertTrue(result["outcome"]["evidence"])

    def test_cancel_one_evaluation_does_not_cancel_another_on_the_same_adapter(self):
        spec = self.spec()
        spec["endpoint"]["body"]["delay"] = 0.15
        cancelled, normal = self.prepare(spec, "one"), self.prepare(spec, "two")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(self.adapter.execute, cancelled), pool.submit(self.adapter.execute, normal)
            time.sleep(0.04)
            self.adapter.cancel(cancelled)
            self.assertEqual(first.result()["status"], "Cancelled")
            self.assertEqual(second.result()["status"], "Pass")

    def test_websocket_uses_explicit_done_and_preserves_actual_content(self):
        from websockets.sync.server import serve

        def handler(socket):
            body = json.loads(socket.recv())
            socket.send(json.dumps({"type": "heartbeat"}))
            socket.send(json.dumps({"delta": "400"}))
            if not body.get("truncate"):
                socket.send(json.dumps({"done": True}))

        with serve(handler, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            spec = self.spec()
            spec["endpoint"].update(url=f"ws://127.0.0.1:{server.socket.getsockname()[1]}", protocol="websocket")
            result = self.adapter.execute(self.prepare(spec))
            self.assertEqual(result["status"], "Pass")
            spec["endpoint"]["body"]["truncate"] = True
            self.assertEqual(self.adapter.execute(self.prepare(spec, "ws-truncated"))["status"], "Fail")
            server.shutdown()
            thread.join(timeout=2)

    def test_websocket_duplex_audio_uses_frozen_input_and_keeps_received_audio_evidence(self):
        from websockets.sync.server import serve

        data = bytes(range(64)) * 16
        audio_file = self.root / "synthetic.pcm"
        audio_file.write_bytes(data)

        def handler(socket):
            json.loads(socket.recv())
            for message in socket:
                if isinstance(message, bytes):
                    socket.send(message)
                    continue
                frame = json.loads(message)
                if frame.get("type") == "end_audio":
                    socket.send(json.dumps({"delta": "合成音频收到"}))
                    socket.send(json.dumps({"done": True}))
                    return
                socket.send(base64.b64decode(frame["audio"]))

        with serve(handler, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            for encoding in ("binary", "base64_json"):
                with self.subTest(encoding=encoding):
                    audio_file.write_bytes(data)
                    spec = self.spec([{"question": "voice", "expected": "合成音频收到"}])
                    spec["endpoint"].update(url=f"ws://127.0.0.1:{server.socket.getsockname()[1]}", protocol="websocket",
                                            audio_input={"file": audio_file.name, "encoding": encoding, "chunk_bytes": 128, "interval_ms": 2,
                                                         "end_event": {"type": "end_audio"}})
                    prepared = self.prepare(spec, encoding)
                    audio_file.write_bytes(b"changed after preparation")
                    result = self.adapter.execute(prepared)
                    self.assertEqual(result["status"], "Pass", result)
                    turn = result["cases"][0]["turns"][0]
                    self.assertEqual(turn["audio_sent"]["sha256"], hashlib.sha256(data).hexdigest())
                    self.assertEqual(turn["audio_received"]["sha256"], turn["audio_sent"]["sha256"])
                    self.assertEqual(Path(turn["audio_received"]["path"]).read_bytes(), data)
                    self.assertIsNotNone(turn["timings"]["first_audio_ms"])
                    self.assertEqual(turn["audio_sent"]["chunks"], 8)
            server.shutdown()
            thread.join(timeout=2)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        unittest.main()
