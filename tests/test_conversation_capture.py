"""Recorded request evidence becomes a multi-turn asset with independent quality checks."""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import conversation_capture, domain, engines, native_assets, projects

spec = importlib.util.spec_from_file_location("capture_ticket_desk", ROOT / "skills/autoskill/examples/ticket-desk/server.py")
ticket_desk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_desk)
PID = "conversation-test"


class ConversationCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ticket_desk.make_server(0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace")})
        environment.start()
        self.addCleanup(environment.stop)
        projects.create("合成问数", self.base, project_id=PID)
        self.client = httpx.Client(base_url=self.base)
        self.addCleanup(self.client.close)
        login = self.client.post("/api/login", json={"username": "qa_admin", "password": "Passw0rd!demo"})
        self.token = login.json()["token"]
        self.client.post("/api/chat/config", json={"wrong_answer": False, "delay_ms": 0, "truncate_stream": False}).raise_for_status()

    def har(self, protocol="http"):
        entries, conversation = [], None
        for question in ("2025年销售额是多少？", "北京呢？"):
            body = {"question": question, "conversation_id": conversation}
            url = self.base + ("/api/chat/stream" if protocol == "sse" else "/api/chat")
            headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
            response = self.client.post(url, json=body, headers=headers)
            response.raise_for_status()
            if protocol == "sse":
                metadata = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
                conversation = json.loads(metadata)["conversation_id"]
            else:
                conversation = response.json()["conversation_id"]
            entries.append({"_resourceType": "fetch", "request": {"url": url, "method": "POST",
                            "headers": [{"name": key, "value": value} for key, value in headers.items()],
                            "postData": {"mimeType": "application/json", "text": json.dumps(body, ensure_ascii=False)}},
                            "response": {"status": response.status_code, "headers": [{"name": key, "value": value} for key, value in response.headers.items()],
                                         "content": {"mimeType": response.headers["content-type"], "text": response.text}}})
        return {"log": {"entries": entries}}

    def capture_asset(self, protocol="http", **mapping):
        captured = conversation_capture.capture(PID, {"session_id": "fixture", "steps": [{"action": "fill", "value": "2025年销售额是多少？"}]}, self.har(protocol))
        if protocol == "sse":
            mapping = {"conversation_response_pointer": "/conversation_id", **mapping}
        return captured, conversation_capture.build_asset(PID, captured["id"], mapping=mapping)

    def execute(self, asset):
        folder = self.root / ("materialized-" + asset["id"])
        native_assets.materialize(PID, asset["id"], folder, asset["head"])
        adapter = engines.get("ai-eval")
        prepared = adapter.prepare(asset, folder, self.root / ("evidence-" + asset["id"]), {})
        try:
            return adapter.execute(prepared)
        finally:
            adapter.cleanup(prepared)

    def test_curl_is_parsed_without_shell_execution_and_keeps_authentication(self):
        marker = self.root / "should-not-exist"
        question = "$(touch " + str(marker) + ")"
        body = json.dumps({"question": question, "conversation_id": "recorded-id"})
        text = f"curl '{self.base}/api/chat' -H 'Authorization: Bearer fixture-token' --data-raw '{body}'"
        with patch("subprocess.run", side_effect=AssertionError("must not execute cURL")):
            request = conversation_capture.parse_curl(text)
        self.assertEqual(request["method"], "POST")
        self.assertEqual(json.loads(request["postData"]["text"])["question"], question)
        self.assertEqual(request["headers"], [{"name": "Authorization", "value": "Bearer fixture-token"}])
        self.assertFalse(marker.exists())
        file = self.root / "source.curl"
        file.write_text(text, encoding="utf-8")
        captured = conversation_capture.import_source(PID, file)
        stored = conversation_capture.get(PID, captured["id"])
        self.assertEqual(domain.read_file(PID, stored["source"]["path"]), text.encode())
        self.assertEqual(stored["requests"][0]["request"], request)

    def test_curl_ansi_json_and_form_encoding(self):
        request = conversation_capture.parse_curl("curl https://fixture.invalid/chat --json $'{\"question\":\"中文\\n问题\"}'")
        self.assertEqual(request["postData"]["text"], '{"question":"中文\n问题"}')
        encoded = conversation_capture.parse_curl("curl https://fixture.invalid/chat --data-urlencode 'question=hello world&more'")
        self.assertEqual(encoded["postData"]["text"], "question=hello+world%26more")
        with self.assertRaisesRegex(ValueError, "文件请求体"):
            conversation_capture.parse_curl("curl https://fixture.invalid/chat --data-binary @private-file")

    def test_http_capture_keeps_raw_evidence_and_does_not_invent_answer_oracles(self):
        har = self.har()
        captured = conversation_capture.capture(PID, {"session_id": "fixture"}, har)
        stored = conversation_capture.get(PID, captured["id"])
        self.assertEqual(stored["requests"][0]["request"], har["log"]["entries"][0]["request"])
        self.assertEqual(json.loads(domain.read_file(PID, stored["source"]["path"])), har)
        self.assertEqual(stored["requests"][0]["business_status"], "Unverified")
        asset = conversation_capture.build_asset(PID, captured["id"])
        spec = json.loads(domain.read_file(PID, f"native-assets/{asset['id']}/files/spec.json"))
        self.assertFalse(any("expected" in turn for turn in spec["samples"][0]["turns"]))
        self.assertEqual(spec["samples"][0]["turns"][1]["endpoint"]["body"]["conversation_id"], "{{conversation_id}}")
        self.assertEqual(spec["endpoint"]["extract"], {"conversation_id": "/conversation_id"})
        outcome = self.execute(asset)
        self.assertEqual(outcome["status"], "Pass")
        self.assertEqual(outcome["business_status"], "Unverified")

    def test_concurrent_multi_turn_replay_binds_server_ids_and_independent_reference_data(self):
        dataset = self.client.get("/api/chat/dataset").json()["rows"]
        _, asset = self.capture_asset(dataset=dataset, load={"concurrency": 3, "iterations": 6}, turn_overrides=[
            {"reference": {"operation": "sum", "field": "sales", "where": [{"field": "year", "value": 2025}]}},
            {"reference": {"operation": "sum", "field": "sales", "where": [{"field": "year", "value": 2025}, {"field": "city", "value": "北京"}]}},
        ])
        outcome = self.execute(asset)
        self.assertEqual(outcome["business_status"], "Pass")
        self.assertEqual(outcome["metrics"]["sent_turns"], 12)
        self.assertEqual(len({case["conversation_id"] for case in outcome["cases"]}), 6)
        for case in outcome["cases"]:
            self.assertEqual([turn["answer"] for turn in case["turns"]], [700, 200])

    def test_sse_capture_retains_raw_text_and_replays_with_explicit_binding(self):
        captured, asset = self.capture_asset("sse", turn_overrides=[{"expected": 700}, {"expected": 200}])
        stored = conversation_capture.get(PID, captured["id"])
        self.assertIsNone(stored["requests"][0]["response_json"])
        self.assertIn("event: done", stored["requests"][0]["response"]["content"]["text"])
        outcome = self.execute(asset)
        self.assertEqual(outcome["business_status"], "Pass")
        self.assertTrue(all(turn["protocol_complete"] for turn in outcome["cases"][0]["turns"]))

    def test_unknown_question_or_answer_field_requires_explicit_mapping(self):
        har = self.har()
        entry = har["log"]["entries"][0]
        entry["request"]["postData"]["text"] = json.dumps({"custom_prompt": "2025年销售额是多少？"})
        entry["response"]["content"]["text"] = json.dumps({"payload": {"answer": 700}})
        har["log"]["entries"] = [entry]
        captured = conversation_capture.capture(PID, {}, har)
        with self.assertRaisesRegex(ValueError, "映射"):
            conversation_capture.build_asset(PID, captured["id"])
        with self.assertRaisesRegex(ValueError, "answer_pointer"):
            conversation_capture.build_asset(PID, captured["id"], mapping={"turns": [1], "question_pointer": "/custom_prompt"})
        asset = conversation_capture.build_asset(PID, captured["id"], mapping={"turns": [1], "question_pointer": "/custom_prompt", "answer_pointer": "/payload/answer"})
        self.assertEqual(asset["turns"], 1)

    def test_fixture_wrong_answer_delay_and_truncation_have_distinct_evidence(self):
        _, http_asset = self.capture_asset(turn_overrides=[{"expected": 700}, {"expected": 200}], performance={"total_ms": 40})
        _, sse_asset = self.capture_asset("sse", turn_overrides=[{"expected": 700}, {"expected": 200}])
        self.client.post("/api/chat/config", json={"wrong_answer": True}).raise_for_status()
        wrong = self.execute(http_asset)
        self.assertEqual(wrong["business_status"], "Fail")
        self.assertFalse(wrong["cases"][0]["turns"][0]["assertions"][0]["passed"])
        self.client.post("/api/chat/config", json={"wrong_answer": False, "delay_ms": 70}).raise_for_status()
        delayed = self.execute(http_asset)
        self.assertEqual(delayed["business_status"], "Pass")
        self.assertEqual(delayed["status"], "Fail")
        self.assertFalse(delayed["cases"][0]["turns"][0]["performance_assertions"][0]["passed"])
        self.client.post("/api/chat/config", json={"delay_ms": 0, "truncate_stream": True}).raise_for_status()
        truncated = self.execute(sse_asset)
        self.assertEqual(truncated["status"], "Fail")
        self.assertEqual(truncated["business_status"], "Unverified")
        self.assertFalse(truncated["cases"][0]["turns"][0]["protocol_complete"])

    def test_static_resources_do_not_create_conversation_assets(self):
        result = conversation_capture.capture(PID, {}, {"log": {"entries": [{"_resourceType": "script", "request": {"url": self.base + "/app.js", "method": "GET"},
                                                                           "response": {"content": {"mimeType": "application/javascript"}}}]}})
        self.assertEqual(result, {"requests": 0, "conversation_candidates": []})
        self.assertIsNone(domain.manifest(PID)["head"])

    def test_cdp_headers_and_stream_body_keep_independent_original_sources(self):
        full = self.har("sse")
        har = copy.deepcopy(full)
        for entry in har["log"]["entries"]:
            entry["request"]["headers"] = []
            entry["response"]["content"].pop("text")
        cdp = copy.deepcopy(full["log"]["entries"])
        for index, entry in enumerate(cdp):
            entry.update(_capture_source="cdp_network", _body_source="streamResourceContent", _body_complete=True,
                         _request_headers_source="requestWillBeSentExtraInfo", _cdp={"request_id": str(index), "session_id": "fixture"})
            entry["request"]["headers"].append({"name": "Cookie", "value": "synthetic=network-observed"})
        cdp.insert(0, {"_resourceType": "script", "request": {"method": "GET", "url": self.base + "/chat.js"}, "response": {}})
        events = self.root / "raw-events.jsonl"
        events.write_text('{"method":"Network.requestWillBeSentExtraInfo"}\n', encoding="utf-8")
        evidence = {"source": "cdp_network", "available": True, "entries": cdp, "error": "", "events_path": str(events)}
        captured = conversation_capture.capture(PID, {"conversation_evidence": evidence}, har)
        stored = conversation_capture.get(PID, captured["id"])
        self.assertEqual(captured["requests"], 2)
        self.assertTrue(stored["source"]["path"].endswith("source.cdp.json"))
        prefix = f"conversations/{captured['id']}/"
        self.assertEqual(json.loads(domain.read_file(PID, prefix + "source.har")), har)
        self.assertEqual(json.loads(domain.read_file(PID, prefix + "source.cdp.json")), evidence)
        self.assertEqual(domain.read_file(PID, prefix + "source.cdp.jsonl"), events.read_bytes())
        request = stored["requests"][0]
        self.assertEqual(request["capture_source"], "cdp_network")
        self.assertTrue(request["body_complete"])
        self.assertEqual(request["source_links"], [{"source": "source.cdp.json", "entry": 1}, {"source": "source.har", "entry": 0}])
        self.assertIn("event: done", request["response"]["content"]["text"])
        self.assertTrue(any(header["name"] == "Cookie" for header in request["request"]["headers"]))
        asset = conversation_capture.build_asset(PID, captured["id"], mapping={"conversation_response_pointer": "/conversation_id", "turn_overrides": [{"expected": 700}, {"expected": 200}]})
        self.assertEqual(self.execute(asset)["business_status"], "Pass")

    def test_unmatched_cdp_requests_are_ordered_and_partial_bodies_stay_partial(self):
        full = self.har()
        first, second = copy.deepcopy(full["log"]["entries"])
        first.update(_capture_source="cdp_network", _body_complete=False, startedDateTime="2026-11-13T00:00:01Z")
        first["response"]["content"].pop("text")
        second["startedDateTime"] = "2026-11-13T00:00:02+00:00"
        captured = conversation_capture.capture(PID, {"conversation_evidence": {"source": "cdp_network", "available": True, "entries": [first]}},
                                                {"log": {"entries": [second]}})
        requests = conversation_capture.get(PID, captured["id"])["requests"]
        self.assertEqual([item["body_json"]["question"] for item in requests], ["2025年销售额是多少？", "北京呢？"])
        self.assertFalse(requests[0]["body_complete"])
        self.assertNotIn("text", requests[0]["response"]["content"])

    def test_compiler_source_keeps_document_authentication_without_mutating_sources(self):
        document = {"_resourceType": "document", "request": {"method": "GET", "url": self.base + "/chat.html", "headers": []},
                    "response": {"status": 200, "content": {"mimeType": "text/html", "text": "fixture page"}}}
        har = {"log": {"creator": {"name": "fixture"}, "entries": [document]}}
        cdp = copy.deepcopy(document)
        cdp["request"]["headers"] = [{"name": "Cookie", "value": "synthetic=observed"}]
        cdp["_request_headers_source"] = "requestWillBeSentExtraInfo"
        evidence = {"entries": [cdp]}
        before = copy.deepcopy((har, evidence))
        compiled = conversation_capture.compiler_source(har, evidence)
        self.assertEqual(len(compiled["log"]["entries"]), 1)
        self.assertEqual(compiled["log"]["entries"][0]["request"]["headers"], cdp["request"]["headers"])
        self.assertEqual(compiled["log"]["creator"], {"name": "fixture"})
        self.assertEqual((har, evidence), before)


if __name__ == "__main__":
    unittest.main()
