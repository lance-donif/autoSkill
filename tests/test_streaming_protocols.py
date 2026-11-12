"""Real localhost HTTP/SSE/WebSocket conversations and PCM audio; no external model calls."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import builder, secrets
from autoskill.replay import api
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
import uvicorn

HANDSHAKES = []


async def sse(request):
    mode = request.query_params.get("mode", "ok")
    if mode == "deny":
        return JSONResponse({"error": "forbidden"}, status_code=403, headers={"Set-Cookie": "denial=seen; Path=/"})
    async def events():
        if mode == "plain-error":
            yield "event: error\ndata: provider failed without JSON\n\n"
            yield "data: [DONE]\n\n"
            return
        if mode == "silent":
            await asyncio.sleep(1)
            yield "data: [DONE]\n\n"
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(.06)
        yield 'event: reasoning\ndata: {"delta":"正在思考"}\n\n'
        if mode == "timeout":
            for _ in range(50):
                await asyncio.sleep(.02)
                yield ": still thinking\n\n"
            return
        await asyncio.sleep(.06)
        yield 'data: {"choices":[{"delta":{"content":"最终答案"}}]}\n\n'
        if mode != "incomplete":
            yield "data: [DONE]\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")


async def websocket(websocket):
    HANDSHAKES.append(websocket.url.path)
    mode = websocket.query_params.get("handshake")
    if mode == "deny":
        await websocket.send_denial_response(JSONResponse({"error": "forbidden"}, status_code=403))
        return
    if mode == "redirect":
        await websocket.send_denial_response(Response(status_code=302, headers={"Location": "ws://" + websocket.headers["host"] + "/redirect-target"}))
        return
    headers = []
    if websocket.query_params.get("set_cookie"):
        headers = [(b"set-cookie", b"session=one; Domain=127.0.0.1; Path=/"), (b"set-cookie", b"other=two; Path=/")]
    await websocket.accept(headers=headers)
    if mode == "backpressure":
        await asyncio.sleep(1.5)  # deliberately never consume the client's queued large frames
        return
    items, audio = {}, bytearray()
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                audio.extend(message["bytes"])
                continue
            value = json.loads(message.get("text", "{}"))
            op = value.get("op") or value.get("type")
            if op == "input_audio_buffer.append":
                audio.extend(base64.b64decode(value["audio"]))
            elif op == "input_audio_buffer.commit":
                await asyncio.sleep(.05)
                await websocket.send_json({"type": "response.output_text.delta", "delta": "收到测试音频"})
                await websocket.send_json({"type": "response.audio.delta", "delta": base64.b64encode(audio).decode()})
                await websocket.send_json({"type": "response.done", "received_audio_bytes": len(audio)})
            elif op == "incomplete":
                await websocket.send_json({"type": "response.output_text.delta", "delta": "尚未完成"})
                await websocket.close()
                break
            elif op == "failed":
                await websocket.send_json({"type": "response.done", "response": {"status": "failed"}})
            elif op in ("odd_audio", "empty_audio"):
                await websocket.send_json({"type": "response.audio.delta", "delta": base64.b64encode(b"x" if op == "odd_audio" else b"").decode()})
                await websocket.send_json({"type": "response.done"})
            elif op == "headers":
                state = set(websocket.headers.get("cookie", "").split("; "))
                await websocket.send_json({"type": "headers", "cookie": websocket.headers.get("cookie", ""),
                    "origins": websocket.headers.getlist("origin"), "hosts": websocket.headers.getlist("host"),
                    "fixed": websocket.query_params.get("fixed"), "added": websocket.query_params.get("added"),
                    "has_session": "session=one" in state, "has_other": "other=two" in state})
            else:
                key = value.get("id", "fixture")
                if op in ("create", "update"):
                    items[key] = value["value"]
                elif op == "delete":
                    items.pop(key, None)
                await websocket.send_json({"type": "result", "op": op, "value": items.get(key), "total": len(items)})
    except Exception:
        # A timeout/negative test deliberately disconnects its peer.
        return


class StreamingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        async def cookie_probe(request):
            return JSONResponse(dict(request.cookies))
        app = Starlette(routes=[Route("/sse", sse), Route("/cookie-probe", cookie_probe),
                                WebSocketRoute("/ws", websocket), WebSocketRoute("/redirect-target", websocket)])
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", http="h11", ws="wsproto", lifespan="off", log_level="critical"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(.01)
        if not cls.server.started:
            raise RuntimeError("Local protocol fixture failed to start")
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("AUTOSKILL_WORKSPACE")
        os.environ["AUTOSKILL_WORKSPACE"] = self.temp.name
        self.config = {"base_url": self.base, "timeout_seconds": 2, "_asset_root": self.temp.name,
                       "_output_root": str(Path(self.temp.name) / "outputs")}
        HANDSHAKES.clear()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("AUTOSKILL_WORKSPACE", None)
        else:
            os.environ["AUTOSKILL_WORKSPACE"] = self.previous
        self.temp.cleanup()

    def run_protocol(self, transport, *, params=None, assertions=None, path=None, headers=None, client=None):
        case = builder.api_case("TC-API-001", "协议对话", "GET", path or ("/sse" if transport["type"] == "sse" else "/ws"),
                                assertions or {"status": 200 if transport["type"] == "sse" else 101}, params=params or {}, headers=headers or {})
        case["extensions"]["transport"] = transport
        if client is not None:
            return api.run_case(client, case, self.config, {})
        with api.new_client(self.config) as client:
            return api.run_case(client, case, self.config, {})

    def test_sse_waits_for_done_and_keeps_reasoning_separate(self):
        result = self.run_protocol({"type": "sse", "timeout_seconds": 2,
                                   "assertions": {"complete": True, "text_contains": ["最终答案"], "reasoning_contains": ["正在思考"], "min_events": 3}})
        self.assertEqual(result["status"], "Pass", result)
        self.assertGreaterEqual(result["duration_ms"], 100)
        self.assertGreater(result["metrics"]["first_text_ms"], result["metrics"]["first_event_ms"])

    def test_sse_partial_answer_is_not_a_pass(self):
        result = self.run_protocol({"type": "sse", "timeout_seconds": 2}, params={"mode": "incomplete"})
        self.assertEqual((result["status"], result["category"]), ("Fail", "stream_incomplete"))

    def test_sse_heartbeats_cannot_extend_the_total_deadline(self):
        result = self.run_protocol({"type": "sse", "timeout_seconds": .13}, params={"mode": "timeout"})
        self.assertNotEqual(result["status"], "Pass")
        self.assertEqual(result["category"], "timeout")
        self.assertLess(result["duration_ms"], 1500)

    def test_sse_first_event_deadline_is_enforced_when_server_sends_nothing(self):
        result = self.run_protocol({"type": "sse", "timeout_seconds": 5,
                                   "first_event_timeout_seconds": .1}, params={"mode": "silent"})
        self.assertEqual(result["category"], "timeout", result)
        self.assertLess(result["duration_ms"], 1000)

    def test_sse_cancellation_interrupts_a_silent_stream(self):
        started = time.monotonic()
        self.config["_cancel_check"] = lambda: time.monotonic() - started > .15
        result = self.run_protocol({"type": "sse", "timeout_seconds": 5}, params={"mode": "silent"})
        self.assertEqual((result["status"], result["category"]), ("Blocked", "cancelled"), result)
        self.assertLess(result["duration_ms"], 1000)

    def test_websocket_full_crud_in_one_conversation(self):
        steps = []
        for op, value, total in (("create", "原始", 1), ("read", "原始", 1), ("update", "修改", 1), ("delete", None, 0), ("read", None, 0)):
            steps.extend([{"send": {"op": op, "id": "item-1", "value": value}},
                          {"receive": {"until": {"pointer": "/op", "equals": op}, "expect_json": {"/value": value, "/total": total}}}])
        result = self.run_protocol({"type": "websocket", "steps": steps, "timeout_seconds": 3,
                                   "assertions": {"min_messages": 5, "complete": True}})
        self.assertEqual(result["status"], "Pass", result)
        self.assertEqual(result["metrics"]["event_count"], 5)

    def test_websocket_voice_pcm_roundtrip_and_private_wav(self):
        file = Path(self.temp.name) / "voice.wav"
        frames = b"".join(struct.pack("<h", (i % 100) * 100) for i in range(640))
        with wave.open(str(file), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000); output.writeframes(frames)
        for encoding in ("base64", "binary"):
            with self.subTest(encoding=encoding):
                result = self.run_protocol({"type": "websocket", "timeout_seconds": 3,
                    "steps": [{"send_audio": {"file": "voice.wav", "encoding": encoding, "chunk_ms": 20, "realtime": True}},
                              {"send": {"type": "input_audio_buffer.commit"}},
                              {"receive": {"until": {"pointer": "/type", "equals": "response.done"}}}],
                    "audio_output": {"filename": encoding + ".wav", "sample_rate": 16000},
                    "assertions": {"min_audio_frames": 1, "min_audio_bytes": len(frames), "text_contains": ["收到测试音频"], "complete": True}})
                self.assertEqual(result["status"], "Pass", result)
                self.assertEqual(result["metrics"]["sent_audio_bytes"], len(frames))
                with wave.open(str(Path(self.temp.name) / "outputs" / (encoding + ".wav")), "rb") as received:
                    self.assertEqual(received.readframes(640), frames)
                self.assertEqual(len(result["artifacts"]), 1)

    def test_websocket_close_without_done_is_not_a_pass(self):
        result = self.run_protocol({"type": "websocket", "timeout_seconds": 2,
            "steps": [{"send": {"op": "incomplete"}}, {"receive": {"until": {"pointer": "/type", "equals": "response.done"}}}]})
        self.assertEqual((result["status"], result["category"]), ("Fail", "stream_incomplete"))

    def test_failed_model_response_done_is_not_success(self):
        result = self.run_protocol({"type": "websocket", "steps": [
            {"send": {"op": "failed"}}, {"receive": {"until": "response.done"}}]})
        self.assertEqual((result["status"], result["category"]), ("Fail", "remote_error"), result)

    def test_send_after_previous_ack_needs_its_own_completion(self):
        result = self.run_protocol({"type": "websocket", "steps": [
            {"send": {"op": "read"}}, {"receive": {}}, {"send": {"op": "update", "value": "unverified"}}]})
        self.assertEqual((result["status"], result["category"]), ("Fail", "stream_incomplete"), result)

    def test_invalid_assertion_is_rejected_before_opening_a_conversation(self):
        result = self.run_protocol({"type": "sse", "assertions": {"text_regex": "["}})
        self.assertEqual((result["status"], result["category"]), ("Blocked", "environment"), result)
        self.assertIsNone(result["request"])

    def test_bad_audio_path_is_blocked_and_cannot_read_outside_assets(self):
        result = self.run_protocol({"type": "websocket", "timeout_seconds": 2,
                                   "steps": [{"send_audio": {"file": "../credentials.json"}}]})
        self.assertEqual(result["status"], "Blocked", result)
        self.assertEqual(result["category"], "environment")

    def test_stream_output_redacts_values_named_by_credentials(self):
        secrets.set_secret("STREAM_SENTINEL", "fixture-secret-123456")
        case = builder.api_case("TC-API-001", "凭据响应", "GET", "/ws", {"status": 101})
        case["extensions"]["transport"] = {"type": "websocket", "steps": [
            {"send": {"op": "create", "value": "${ENV:STREAM_SENTINEL}"}},
            {"receive": {"expect_json": {"/value": "${ENV:STREAM_SENTINEL}"}}}]}
        with api.new_client(self.config) as client:
            result = api.run_case(client, case, self.config, {})
        self.assertEqual(result["status"], "Pass", result)
        self.assertNotIn("fixture-secret-123456", json.dumps(result))

    def test_unknown_transport_assertions_cannot_be_silently_ignored(self):
        for rules in ({"text_equals": "not returned"}, {"min_sent_audio_bytez": 99999}):
            with self.subTest(rules=rules):
                result = self.run_protocol({"type": "sse", "assertions": rules})
                self.assertEqual((result["status"], result["category"]), ("Blocked", "environment"), result)
                self.assertIsNone(result["request"])

    def test_plain_text_sse_error_followed_by_done_is_still_failure(self):
        result = self.run_protocol({"type": "sse"}, params={"mode": "plain-error"})
        self.assertEqual((result["status"], result["category"]), ("Fail", "remote_error"), result)

    def test_rejected_websocket_handshake_can_verify_status_and_error_body(self):
        result = self.run_protocol({"type": "websocket", "steps": [{"receive": {}}]}, params={"handshake": "deny"},
                                   assertions={"status": 403, "json_equals": {"/error": "forbidden"}})
        self.assertEqual(result["status"], "Pass", result)
        self.assertEqual(result["response"]["status"], 403)
        expected_success = self.run_protocol({"type": "websocket", "steps": [{"receive": {}}]}, params={"handshake": "deny"})
        self.assertNotEqual(expected_success["status"], "Pass")

    def test_rejected_sse_response_can_verify_status_body_and_update_current_cookies(self):
        with api.new_client(self.config) as client:
            result = self.run_protocol({"type": "sse"}, params={"mode": "deny"}, client=client,
                assertions={"status": 403, "json_equals": {"/error": "forbidden"}})
            self.assertEqual(result["status"], "Pass", result)
            self.assertEqual(client.get(self.base + "/cookie-probe").json(), {"denial": "seen"})

    def test_negative_handshake_does_not_ignore_explicit_conversation_expectations(self):
        for rules, wanted in (({"complete": False}, "Pass"), ({"complete": True}, "Fail"), ({"text_contains": ["answer"]}, "Fail")):
            with self.subTest(rules=rules):
                result = self.run_protocol({"type": "websocket", "steps": [{"receive": {}}], "assertions": rules},
                    params={"handshake": "deny"}, assertions={"status": 403, "json_equals": {"/error": "forbidden"}})
                self.assertEqual(result["status"], wanted, result)

    def test_websocket_redirect_is_observed_without_contacting_target_or_forwarding_auth(self):
        secrets.set_secret("REDIRECT_AUTH", "redirect-private-token")
        result = self.run_protocol({"type": "websocket", "steps": [{"receive": {}}]}, params={"handshake": "redirect"},
            headers={"Authorization": "Bearer ${ENV:REDIRECT_AUTH}"},
            assertions={"status": 302, "header_contains": {"location": "/redirect-target"}})
        self.assertEqual(result["status"], "Pass", result)
        self.assertNotIn("/redirect-target", HANDSHAKES)
        self.assertNotIn("redirect-private-token", json.dumps(result))

    def test_mixed_case_custom_cookie_origin_and_host_are_sent_once_and_queries_preserved(self):
        result = self.run_protocol({"type": "websocket", "steps": [
            {"send": {"op": "headers"}}, {"receive": {"until": "headers", "expect_json": {
                "/cookie": "explicit=one", "/origins": ["https://configured.example"], "/hosts": ["virtual.example"],
                "/fixed": "one", "/added": "two"}}}]}, params={"fixed": "one", "added": "two"},
            headers={"cOoKiE": "explicit=one", "oRiGiN": "https://configured.example", "hOsT": "virtual.example"})
        self.assertEqual(result["status"], "Pass", result)

    def test_handshake_cookies_are_shared_only_with_the_current_http_client(self):
        steps = [{"send": {"op": "headers"}}, {"receive": {"until": "headers"}}]
        with api.new_client(self.config) as first:
            result = self.run_protocol({"type": "websocket", "steps": steps}, params={"set_cookie": "yes"}, client=first)
            self.assertEqual(result["status"], "Pass", result)
            self.assertEqual(first.get(self.base + "/cookie-probe").json(), {"session": "one", "other": "two"})
            again = self.run_protocol({"type": "websocket", "steps": steps}, client=first,
                assertions={"status": 101, "json_equals": {"/events/0/has_session": True, "/events/0/has_other": True}})
            self.assertEqual(again["status"], "Pass", again)
        with api.new_client(self.config) as second:
            clean = self.run_protocol({"type": "websocket", "steps": steps}, client=second,
                assertions={"status": 101, "json_equals": {"/events/0/cookie": ""}})
            self.assertEqual(clean["status"], "Pass", clean)

    def test_empty_misaligned_and_truncated_input_audio_cannot_pass_a_voice_turn(self):
        for name, payload in (("empty.pcm", b""), ("odd.pcm", b"x"), ("invalid.wav", b"not a WAV")):
            (Path(self.temp.name) / name).write_bytes(payload)
        wav = Path(self.temp.name) / "truncated.wav"
        with wave.open(str(wav), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000); output.writeframes(b"\0\0" * 200)
        wav.write_bytes(wav.read_bytes()[:-2])
        for name in ("empty.pcm", "odd.pcm", "invalid.wav", "truncated.wav"):
            with self.subTest(file=name):
                result = self.run_protocol({"type": "websocket", "steps": [
                    {"send_audio": {"file": name}}, {"send": {"type": "input_audio_buffer.commit"}}, {"receive": {"until": "response.done"}}]})
                self.assertEqual((result["status"], result["category"]), ("Blocked", "environment"), result)

    def test_audio_output_requires_actual_complete_pcm16_frames(self):
        for op in ("odd_audio", "empty_audio"):
            with self.subTest(op=op):
                result = self.run_protocol({"type": "websocket", "steps": [
                    {"send": {"op": op}}, {"receive": {"until": "response.done"}}], "audio_output": {"filename": op + ".wav"},
                    "assertions": {"min_audio_frames": 1, "min_audio_bytes": 1}})
                self.assertNotEqual(result["status"], "Pass", result)
                self.assertFalse((Path(self.temp.name) / "outputs" / (op + ".wav")).exists())

    def test_realtime_input_wait_obeys_cancellation_and_total_budget(self):
        (Path(self.temp.name) / "long.pcm").write_bytes(b"\0\0" * 32000)
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                start = time.monotonic()
                if cancelled:
                    self.config["_cancel_check"] = lambda: time.monotonic() - start > .12
                result = self.run_protocol({"type": "websocket", "timeout_seconds": 5 if cancelled else .12, "steps": [
                    {"send_audio": {"file": "long.pcm", "chunk_ms": 1000, "realtime": True}}, {"receive": {}}]})
                self.assertEqual(result["category"], "cancelled" if cancelled else "timeout", result)
                self.assertLess(result["duration_ms"], 800)
                self.config.pop("_cancel_check", None)

    def test_cancellation_interrupts_a_real_backpressured_websocket_send(self):
        started = time.monotonic()
        self.config["_cancel_check"] = lambda: time.monotonic() - started > .25
        result = self.run_protocol({"type": "websocket", "timeout_seconds": 5, "steps": [
            {"send": "x" * (8 * 1024 * 1024)}, {"send": "y" * (8 * 1024 * 1024)}, {"receive": {}}]}, params={"handshake": "backpressure"})
        self.assertEqual((result["status"], result["category"]), ("Blocked", "cancelled"), result)
        self.assertLess(result["duration_ms"], 1200)

    def test_stalled_close_handshake_cannot_pass_after_the_total_deadline(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        stop = threading.Event()
        def peer():
            connection = None
            try:
                connection, _ = listener.accept()
                connection.settimeout(2)
                request = bytearray()
                while not request.endswith(b"\r\n\r\n"):
                    request.extend(connection.recv(1))
                values = dict(line.split(":", 1) for line in request.decode().split("\r\n")[1:] if ":" in line)
                key = values["Sec-WebSocket-Key"].strip()
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
                payload = b'{"type":"ack"}'
                connection.sendall(bytes([0x81, len(payload)]) + payload)
                stop.wait(1)  # refuse to acknowledge the client's close frame
            except OSError:
                pass
            finally:
                if connection is not None:
                    connection.close()
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        previous = self.config["base_url"]
        self.config["base_url"] = "http://127.0.0.1:" + str(listener.getsockname()[1])
        try:
            result = self.run_protocol({"type": "websocket", "timeout_seconds": .08,
                "steps": [{"receive": {"until": "ack"}}, {"close": True}]})
            self.assertNotEqual(result["status"], "Pass", result)
            self.assertEqual(result["category"], "timeout", result)
            self.assertLess(result["duration_ms"], 800)
        finally:
            self.config["base_url"] = previous
            stop.set()
            listener.close()
            thread.join(2)
