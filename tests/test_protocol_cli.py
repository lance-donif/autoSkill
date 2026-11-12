"""API specification portability plus a real local HTTP CRUD command round trip."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import artifacts, paths, projects, protocols_cli, secrets


def parser():
    value = argparse.ArgumentParser()
    protocols_cli.register(value.add_subparsers(dest="api_action", required=True))
    return value


class CRUDSite(BaseHTTPRequestHandler):
    items = {}
    updates = []

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        item = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        type(self).items["1"] = item
        type(self).updates.append("create")
        self.reply(201, {"id": "1", **item})

    def do_GET(self):
        item = type(self).items.get(self.path.rsplit("/", 1)[-1])
        self.reply(200 if item is not None else 404, item if item is not None else {"error": "missing"})

    def do_PATCH(self):
        item = type(self).items[self.path.rsplit("/", 1)[-1]]
        item.update(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        type(self).updates.append("update")
        self.reply(200, item)

    def do_DELETE(self):
        type(self).items.pop(self.path.rsplit("/", 1)[-1], None)
        type(self).updates.append("delete")
        self.reply(200, {"deleted": True})


class ProtocolCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, AUTOSKILL_WORKSPACE=self.temp.name)
        self.env.start()
        projects.create("Protocol", "http://127.0.0.1:1", project_id="protocol")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_request_parser_exposes_long_stream_and_audio_latency_assertions(self):
        args = parser().parse_args(["request", "protocol", "--transport", "sse", "--path", "/stream", "--timeout-seconds", "900",
                                   "--first-event-timeout-seconds", "120", "--max-first-audio-ms", "30000", "--expect-reasoning", "plan"])
        case = protocols_cli.build_case("protocol", {}, **vars(args))
        transport = case["extensions"]["transport"]
        self.assertEqual(transport["timeout_seconds"], 900)
        self.assertEqual(transport["idle_timeout_seconds"], 900)
        self.assertEqual(transport["first_event_timeout_seconds"], 120)
        self.assertEqual(transport["assertions"]["max_first_audio_ms"], 30000)
        self.assertEqual(transport["assertions"]["reasoning_contains"], ["plan"])

    def test_sensitive_header_query_and_body_values_save_as_named_refs(self):
        spec = {"method": "POST", "path": "/api/tickets?token=query-private", "headers": {"Authorization": "Bearer header-private"},
                "json": {"password": "body-private", "name": "Synthetic"}, "assertions": {"status": 201, "json_equals": {"/name": "Synthetic"}}}
        case = protocols_cli.build_case("protocol", spec)
        serialized = json.dumps(case)
        for value in ("query-private", "header-private", "body-private"):
            self.assertNotIn(value, serialized)
        self.assertEqual(case["path"], "/api/tickets")
        self.assertEqual(secrets.resolve(case["params"])["token"], "query-private")
        self.assertEqual(secrets.resolve(case["headers"])["Authorization"], "Bearer header-private")
        projects.save_cases("protocol", [case])
        self.assertEqual(projects.case("protocol", case["id"])["api_source"], "manual")

    def test_websocket_voice_inputs_are_staged_and_session_secrets_are_ref_names(self):
        sample = Path(self.temp.name) / "private-source.pcm"
        sample.write_bytes(b"\x00\x01" * 160)
        spec = {"path": "/voice", "transport": {"type": "websocket", "until": {"pointer": "/type", "equals": "response.done"},
                "steps": [{"send": {"type": "session.update", "api_key": "voice-private"}},
                          {"send_audio": {"file": str(sample), "event": {"type": "audio.append"}, "field": "audio"}},
                          {"receive": {"until": {"pointer": "/type", "equals": "response.done"}, "min_messages": 1}}],
                "audio_output": {"event": "audio.delta", "field": "/delta", "filename": "reply.wav", "sample_rate": 16000}}}
        case = protocols_cli.build_case("protocol", spec, min_audio_bytes=100, min_audio_frames=1)
        serialized = json.dumps(case)
        self.assertNotIn(str(sample), serialized)
        self.assertNotIn("voice-private", serialized)
        staged = case["extensions"]["transport"]["steps"][1]["send_audio"]["file"]
        self.assertTrue(staged.startswith("files/uploads/"))
        self.assertEqual(Path(artifacts.resolve_upload_assets(projects.path("protocol"), [staged])[0]).read_bytes(), sample.read_bytes())
        projects.save_cases("protocol", [case])

    def test_http_time_budget_uses_http_extension_without_stream_validation(self):
        case = protocols_cli.build_case("protocol", {"path": "/api/items"}, timeout_seconds=700)
        self.assertNotIn("transport", case["extensions"])
        self.assertEqual(case["extensions"]["timeout_seconds"], 700)

    def test_negative_stream_handshakes_keep_status_expectations_without_forcing_completion(self):
        for kind, status in (("sse", 401), ("sse", 403), ("sse", 302), ("websocket", 401), ("websocket", 403), ("websocket", 302)):
            transport = {"type": kind, **({"steps": [{"receive": {"until": "done"}}]} if kind == "websocket" else {})}
            case = protocols_cli.build_case("protocol", {"path": "/chat", "transport": transport}, expect_status=status)
            with self.subTest(kind=kind, status=status):
                self.assertEqual(case["assertions"]["status"], status)
                self.assertNotIn("/complete", case["assertions"].get("json_equals", {}))
                self.assertNotIn("complete", case["extensions"]["transport"].get("assertions", {}))
        explicit = protocols_cli.build_case("protocol", {"path": "/sse", "transport": {"type": "sse", "assertions": {"complete": False}},
                                                          "assertions": {"status": 401, "json_equals": {"/complete": False}}})
        self.assertFalse(explicit["extensions"]["transport"]["assertions"]["complete"])

    def test_invalid_spec_is_rejected_before_request(self):
        invalid = [({"path": "https://example.test/other"}, {}), ({"path": "//example.test/other"}, {}),
                   ({"path": "/api"}, {"timeout_seconds": float("inf")}),
                   ({"path": "/ws", "method": "POST", "transport": "websocket"}, {}),
                   ({"path": "/ws", "transport": "websocket"}, {}),
                   ({"path": "/sse", "transport": "sse"}, {"timeout_seconds": 30, "idle_timeout_seconds": 60}),
                   ({"path": "/api", "transport": {"type": "http", "steps": [], "timeout_seconds": 10}}, {})]
        for spec, overrides in invalid:
            with self.subTest(spec=spec, overrides=overrides), self.assertRaises(ValueError):
                protocols_cli.build_case("protocol", spec, **overrides)

    def test_real_local_http_crud_commands_save_and_replayable_cases(self):
        CRUDSite.items, CRUDSite.updates = {}, []
        server = ThreadingHTTPServer(("127.0.0.1", 0), CRUDSite)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            projects.create("Live protocol", f"http://127.0.0.1:{server.server_port}", project_id="live")
            requests = [
                ["--method", "POST", "--path", "/api/items", "--json-body", '{"name":"Synthetic-CLI"}', "--expect-status", "201", "--expect-json", '{"/id":"1"}', "--save-case", "Create"],
                ["--method", "GET", "--path", "/api/items/1", "--expect-json", '{"/name":"Synthetic-CLI"}', "--save-case", "Read"],
                ["--method", "PATCH", "--path", "/api/items/1", "--json-body", '{"name":"Updated-CLI"}', "--expect-json", '{"/name":"Updated-CLI"}', "--save-case", "Update"],
                ["--method", "DELETE", "--path", "/api/items/1", "--expect-json", '{"/deleted":true}', "--save-case", "Delete"],
                ["--method", "GET", "--path", "/api/items/1", "--expect-status", "404", "--expect-json", '{"/error":"missing"}', "--save-case", "Verify deletion"],
            ]
            for request in requests:
                outcome = protocols_cli.dispatch(parser().parse_args(["request", "live", *request]))
                self.assertEqual(outcome["status"], "Pass", outcome)
                self.assertIn("saved_case", outcome)
            self.assertEqual(CRUDSite.items, {})
            self.assertEqual(CRUDSite.updates, ["create", "update", "delete"])
            self.assertEqual(len(projects.cases("live")), 5)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
