"""Postman HTTP exports must never pretend to replay stream/audio conversations."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, cli, projects  # noqa: E402
from autoskill.replay import postman  # noqa: E402


class PostmanProtocolTests(unittest.TestCase):
    def http(self, case_id="TC-HTTP"):
        return builder.api_case(case_id, "HTTP request", "GET", "/health", {"status": 200, "json_equals": {"/ok": True}})

    def conversation(self, protocol):
        case = builder.api_case("TC-" + protocol.upper(), "Streaming conversation", "GET", "/conversation",
                                {"status": 101 if protocol == "websocket" else 200, "json_equals": {"/complete": True}})
        case["extensions"]["transport"] = {"type": protocol, "until": {"pointer": "/type", "equals": "response.done"},
                                                "assertions": {"complete": True, "text_contains": ["answer"]}}
        if protocol == "websocket":
            case["extensions"]["transport"]["steps"] = [{"send": {"type": "response.create"}}, {"receive": {}}]
        return case

    def test_sse_and_websocket_export_fail_with_case_and_transport(self):
        for protocol in ("sse", "websocket"):
            case = self.conversation(protocol)
            with self.subTest(protocol=protocol), self.assertRaises(ValueError) as caught:
                postman.export("Conversations", [case], "http://example.test")
            message = str(caught.exception)
            self.assertIn(case["id"], message)
            self.assertIn(protocol, message)
            self.assertIn("HTTP", message)
            self.assertIn("autoskill run", message)

    def test_mixed_plan_rejects_the_whole_incomplete_collection(self):
        cases = [self.http(), self.conversation("sse"), self.conversation("websocket")]
        with self.assertRaises(ValueError) as caught:
            postman.export("Mixed prerequisite plan", cases, "http://example.test")
        self.assertIn("TC-SSE", str(caught.exception))
        self.assertIn("TC-WEBSOCKET", str(caught.exception))

    def test_required_ui_login_cannot_be_dropped_from_http_chain(self):
        login = builder.ui_case("TC-LOGIN", "UI login prerequisite", {"start_url": "/login", "actions": [
            {"action": "click", "selector": "#sign-in"}]})
        request = self.http()
        request["extensions"]["depends_on"] = [login["id"]]
        with self.assertRaises(ValueError) as caught:
            postman.export("UI-authenticated HTTP", [login, request], "http://example.test")
        self.assertIn(login["id"], str(caught.exception))
        self.assertIn("不能省略", str(caught.exception))

    def test_realtime_audio_cannot_be_reduced_to_handshake_status(self):
        case = self.conversation("websocket")
        case["extensions"]["transport"]["steps"] = [
            {"send_audio": {"file": "files/audio/input.wav", "event": {"type": "input_audio_buffer.append"}}},
            {"receive": {"until": {"pointer": "/type", "equals": "response.done"}}}]
        case["extensions"]["transport"]["assertions"].update(min_audio_bytes=1000, min_audio_frames=1)
        with self.assertRaises(ValueError):
            postman.export("Voice", [case], "http://example.test")

    def test_direct_request_and_assertion_builders_also_refuse_streams(self):
        case = self.conversation("sse")
        with self.assertRaises(ValueError):
            postman._item(case, set())
        with self.assertRaises(ValueError):
            postman.test_script(case)

    def test_unknown_or_malformed_transport_is_not_assumed_http(self):
        for transport in ({"type": "grpc"}, "websocket", None):
            case = self.http()
            case["extensions"]["transport"] = transport
            with self.subTest(transport=transport), self.assertRaises(ValueError):
                postman.export("Unknown", [case], "http://example.test")

    def test_ordinary_http_order_variables_and_secret_references_remain_exportable(self):
        first = self.http("TC-LOGIN")
        first["extensions"]["extract"] = {"token": "/token"}
        second = self.http("TC-READ")
        second["headers"] = {"Authorization": "Bearer ${ENV:POSTMAN_TOKEN}"}
        second["params"] = {"created": "{{token}}"}
        second["extensions"]["transport"] = {"type": "http"}
        with patch.dict(os.environ, {"POSTMAN_TOKEN": "private-value-never-exported"}):
            collection, environment = postman.export("HTTP regression", [first, second], "http://example.test/")
        self.assertEqual([item["name"].split()[0] for item in collection["item"]], ["TC-LOGIN", "TC-READ"])
        self.assertEqual(collection["item"][1]["request"]["header"][0]["value"], "Bearer {{POSTMAN_TOKEN}}")
        self.assertEqual(collection["variable"], [{"key": "token", "value": ""}])
        secret = next(value for value in environment["values"] if value["key"] == "POSTMAN_TOKEN")
        self.assertEqual((secret["value"], secret["type"]), ("", "secret"))
        self.assertNotIn("private-value-never-exported", json.dumps((collection, environment)))

    def test_real_cli_export_reports_unsupported_and_writes_no_fake_collection(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": temporary, "AUTOSKILL_BRIDGE": "off"}):
            projects.create("Protocol export", "http://127.0.0.1:1", project_id="protocol-export")
            projects.save_cases("protocol-export", [self.http(), self.conversation("sse")])
            out = Path(temporary) / "postman-output"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["export", "postman", "protocol-export", "--out", str(out)])
            self.assertEqual(code, 2)
            feedback = json.loads(output.getvalue())
            self.assertIn("TC-SSE", feedback["error"])
            self.assertIn("仅支持 HTTP", feedback["error"])
            self.assertFalse(out.exists())

    def test_cli_and_dashboard_preserve_ui_prerequisite_for_core_validation(self):
        from fastapi.testclient import TestClient
        from autoskill.server.app import create_app

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": temporary, "AUTOSKILL_BRIDGE": "off"}):
            projects.create("UI prerequisite", "http://127.0.0.1:1", project_id="ui-prerequisite")
            login = builder.ui_case("TC-UI-LOGIN", "UI login", {"start_url": "/login"})
            request = self.http()
            request["extensions"]["depends_on"] = [login["id"]]
            projects.save_cases("ui-prerequisite", [login, request])
            out = Path(temporary) / "postman-output"
            with self.assertRaises(ValueError) as caught:
                cli.export_postman("ui-prerequisite", str(out))
            self.assertIn(login["id"], str(caught.exception))
            self.assertFalse(out.exists())
            with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
                response = client.get("/api/projects/ui-prerequisite/postman")
                self.assertEqual(response.status_code, 400)
                self.assertIn(login["id"], response.json()["detail"])
                # A UI case elsewhere in the project is irrelevant when it is no
                # longer required by this selected HTTP chain.
                request["extensions"].pop("depends_on")
                projects.save_cases("ui-prerequisite", [request])
                supported = client.get("/api/projects/ui-prerequisite/postman")
                self.assertEqual(supported.status_code, 200, supported.text)
                self.assertEqual(len(supported.json()["item"]), 1)
            result = cli.export_postman("ui-prerequisite", str(out))
            self.assertEqual(result["requests"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
