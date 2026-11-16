"""Cold HTTP inputs remain isolated, typed and bound to their actual execution scope."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, execution_inputs, paths, projects, runner, secrets, sessions


class TypedHttpTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw) if raw else None
        self.server.requests.append((self.path, body, self.headers.get("Cookie", "")))
        content = json.dumps({"ok": True, "authenticated": True, "token": "fresh-http-token"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        if self.path == "/login":
            self.send_header("Set-Cookie", "sid=fresh-http-session; Path=/")
        self.end_headers()
        self.wfile.write(content)


class ApiExecutionInputTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": folder.name, "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("独立接口输入", "http://127.0.0.1:9", project_id="api-inputs")["id"]
        self.ui = builder.ui_case("UI-LOGIN", "页面登录", {"start_url": "/login", "actions": [], "assertions": []})
        self.login = builder.api_case("API-LOGIN", "接口登录", "POST", "/login", {"status": 200, "json_equals": {"/authenticated": True}}, extract={"token": "/token"})
        self.case = builder.api_case("API-CHECK", "业务检查", "POST", "/check", {"status": 200, "json_equals": {"/ok": "{{expected}}"}},
                                     depends_on=[self.login["id"]], json={"payload": "{{payload}}"})
        projects.save_cases(self.pid, [self.ui, self.login, self.case])
        self.live_auth = {"ui_case": self.ui["id"], "state_variables": {"token": {"name": "browser-token"}}}
        projects.set_auth(self.pid, self.live_auth)

    def capture(self, **options):
        return execution_inputs.capture(self.pid, [self.case["id"]], "playwright", persist=False, **options)

    def test_freeze_never_reads_browser_state_and_retains_json_types_and_candidate_auth(self):
        supplied = {"expected": True, "payload": {"quantity": 3, "discount": 0.25, "missing": None, "items": [False, "订单"]},
                    "api_key": "${ENV:COLD_INPUT_KEY}"}
        original = copy.deepcopy(supplied)
        candidate_auth = {"api_case": self.login["id"]}
        secrets.set_secret("COLD_INPUT_KEY", "synthetic-frozen-key")
        with patch.object(sessions, "load", side_effect=AssertionError("Cold HTTP must not read saved sessions")), \
                patch.object(execution_inputs, "_playwright_browser", side_effect=AssertionError("HTTP must not probe browsers")), \
                patch.object(db, "connect", side_effect=AssertionError("Freezing must not write runtime state")):
            receipt, frozen = execution_inputs.freeze(self.pid, [self.case["id"]], "playwright", candidate_auth=candidate_auth,
                                                      api_only=True, inputs=supplied)
        self.assertEqual(frozen["saved_session"], {})
        self.assertEqual(frozen["engine"]["adapter"], "http")
        self.assertEqual(frozen["engine"]["runtime"], {})
        self.assertEqual(set(frozen["cases"]), {self.case["id"], self.login["id"]})
        self.assertEqual(frozen["inputs"], original)
        self.assertEqual({key: value for key, value in frozen["variables"].items() if key != "RAND"}, original)
        self.assertEqual(frozen["credentials"]["COLD_INPUT_KEY"], "synthetic-frozen-key")
        self.assertEqual(frozen["auth"], candidate_auth)
        supplied["payload"]["quantity"] = 999
        candidate_auth["api_case"] = "OTHER"
        self.assertEqual(frozen["inputs"], original)
        self.assertEqual(frozen["variables"]["payload"]["quantity"], 3)
        self.assertEqual(projects.auth(self.pid), self.live_auth)
        self.assertEqual(db.rows("SELECT id FROM runs"), [])
        stored = json.loads(Path(receipt["input_manifest"]).read_bytes())
        self.assertEqual(execution_inputs.content_hash(stored), receipt["input_hash"])
        self.assertEqual(stored, frozen)

    def test_scope_distinguishes_cold_mode_typed_inputs_auth_and_referenced_credentials(self):
        auth = {"api_case": self.login["id"]}
        base = self.capture(candidate_auth=auth, api_only=True, inputs={"expected": True})
        scope = base["scope_hashes"][self.case["id"]]
        self.assertEqual(scope, self.capture(candidate_auth=auth, api_only=True, inputs={"expected": True})["scope_hashes"][self.case["id"]])
        variants = [self.capture(candidate_auth=auth, inputs={"expected": True}),
                    self.capture(candidate_auth=auth, api_only=True, inputs={"expected": "true"}),
                    self.capture(candidate_auth={}, api_only=True, inputs={"expected": True})]
        self.assertTrue(all(value["scope_hashes"][self.case["id"]] != scope for value in variants))
        secrets.set_secret("COLD_INPUT_KEY", "before")
        before = self.capture(api_only=True, inputs={"key": "${ENV:COLD_INPUT_KEY}"})
        secrets.set_secret("COLD_INPUT_KEY", "after")
        after = self.capture(api_only=True, inputs={"key": "${ENV:COLD_INPUT_KEY}"})
        self.assertNotEqual(before["scope_hashes"], after["scope_hashes"])

    def test_ordinary_scope_keeps_the_legacy_hash_shape(self):
        snapshot = self.capture()
        case_ids = [self.case["id"], self.login["id"], self.ui["id"]]
        subset = {key: snapshot["cases"][key] for key in case_ids}
        names = secrets.references([subset, snapshot["config"], snapshot["auth"]])
        legacy = execution_inputs.content_hash({
            "cases": subset, "config": snapshot["config"], "auth": snapshot["auth"],
            "sources": snapshot["sources"]["scope"], "engine": snapshot["engine"],
            "credentials": {key: snapshot["credentials"].get(key) for key in sorted(names)},
        })
        self.assertEqual(snapshot["scope_hashes"][self.case["id"]], legacy)
        explicit_empty = self.capture(api_only=False, inputs={})
        self.assertEqual(explicit_empty["scope_hashes"], snapshot["scope_hashes"])

    def test_cold_auth_scope_ignores_unused_browser_fields_but_keeps_api_configuration(self):
        candidate_auth = {"api_case": self.login["id"], "ui_case": "UNPUBLISHED-UI",
                          "state_variables": {"token": {"name": "unused-browser-token"}}}
        frozen = self.capture(candidate_auth=candidate_auth, api_only=True)
        self.assertEqual(frozen["auth"], {"api_case": self.login["id"]})
        self.assertEqual(candidate_auth["ui_case"], "UNPUBLISHED-UI")
        self.assertEqual(projects.auth(self.pid), self.live_auth)
        projects.set_auth(self.pid, {**self.live_auth, "api_case": self.login["id"]})
        self.assertEqual(frozen["scope_hashes"], self.capture(api_only=True)["scope_hashes"])
        projects.set_auth(self.pid, {"api_case": self.login["id"], "api_check_case": self.case["id"]})
        self.assertNotEqual(frozen["scope_hashes"], self.capture(api_only=True)["scope_hashes"])

    def test_declared_api_default_uses_current_definition_and_explicit_inputs_stay_separate(self):
        self.case["extensions"]["execution"] = {"api_only": True}
        projects.save_cases(self.pid, [self.case])
        current = self.capture()
        self.assertTrue(current["api_only"])
        self.assertNotIn(self.ui["id"], current["cases"])
        explicit = self.capture(inputs={"expected": True})
        self.assertNotEqual(current["scope_hashes"], explicit["scope_hashes"])
        self.case["extensions"].pop("execution")
        projects.save_cases(self.pid, [self.case])
        changed = self.capture()
        self.assertFalse(changed["api_only"])
        self.assertNotEqual(current["scope_hashes"], changed["scope_hashes"])
        mixed = execution_inputs.capture(self.pid, [self.ui["id"], self.case["id"]], "playwright", persist=False)
        self.assertFalse(mixed["api_only"])

    def test_explicit_ui_target_and_recursive_ui_dependency_cannot_be_frozen_as_http(self):
        with self.assertRaisesRegex(ValueError, "页面用例或页面依赖"):
            execution_inputs.freeze(self.pid, [self.ui["id"]], "playwright", api_only=True)
        self.login["extensions"]["depends_on"] = [self.ui["id"]]
        projects.save_cases(self.pid, [self.login])
        with self.assertRaisesRegex(ValueError, "UI-LOGIN"):
            execution_inputs.freeze(self.pid, [self.case["id"]], "playwright", api_only=True)
        self.assertFalse((paths.local() / "execution-inputs").exists())
        self.assertEqual(db.rows("SELECT id FROM runs"), [])

    def test_explicit_api_mode_without_targets_selects_only_api_and_invalid_inputs_do_not_freeze(self):
        selected = execution_inputs.capture(self.pid, None, "playwright", api_only=True, persist=False)
        self.assertEqual(set(selected["selected"]), {self.login["id"], self.case["id"]})
        for value in ([], {"RAND": "fixed"}, {"number": float("nan")}, {"items": {1, 2}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                execution_inputs.freeze(self.pid, [self.case["id"]], "playwright", inputs=value)
        self.assertFalse((paths.local() / "execution-inputs").exists())

    def test_recorded_credential_marker_applies_to_actual_http_dependency_closure(self):
        marked = copy.deepcopy(self.login)
        marked["extensions"]["execution"] = {"unresolved_recorded_credentials": ["CAPTURED_TOKEN"]}
        marked["headers"] = {"Authorization": "Bearer ${ENV:CAPTURED_TOKEN}"}
        secrets.set_secret("CAPTURED_TOKEN", "still-valid-fixture-token")
        with self.assertRaisesRegex(ValueError, "API-LOGIN.*API 登录或明确输入来源"):
            execution_inputs.freeze(self.pid, [self.case["id"]], "playwright", api_only=True, candidate_cases=[marked])
        self.assertFalse((paths.local() / "execution-inputs").exists())
        self.assertEqual(db.rows("SELECT id FROM runs"), [])
        unrelated = copy.deepcopy(marked)
        unrelated["id"] = "API-UNUSED"
        frozen = self.capture(api_only=True, candidate_cases=[unrelated], candidate_auth={"api_check_case": unrelated["id"]})
        self.assertIn(unrelated["id"], frozen["cases"])
        self.assertNotIn(unrelated["id"], frozen["selected"])
        persistent = copy.deepcopy(marked)
        persistent["extensions"]["execution"] = {"api_only": True}
        self.assertTrue(self.capture(api_only=True, candidate_cases=[persistent])["api_only"])

    def test_real_parameterized_http_evidence_does_not_claim_other_default_inputs(self):
        target = ThreadingHTTPServer(("127.0.0.1", 0), TypedHttpTarget)
        target.requests = []
        threading.Thread(target=target.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        config = projects.config(self.pid)
        config["base_url"] = f"http://127.0.0.1:{target.server_port}"
        config["allowed_origins"] = [config["base_url"]]
        execution_inputs.write_json(projects.path(self.pid) / "qa-config.json", config)
        self.case["extensions"]["execution"] = {"api_only": True}
        projects.save_cases(self.pid, [self.case])
        projects.set_auth(self.pid, {**self.live_auth, "api_case": self.login["id"]})
        inputs = {"expected": True, "payload": {"count": 0, "flag": False, "empty": None, "items": [1, "2"]}}
        with patch.object(sessions, "load", side_effect=AssertionError("No browser or prior login state")), \
                patch.object(sessions, "save", side_effect=AssertionError("No shared login mutation")), \
                patch.object(runner, "make_driver", side_effect=AssertionError("No browser fallback")):
            queued = runner.create(self.pid, [self.case["id"]], inputs=inputs)
            completed = runner.execute(queued["id"])
        self.assertEqual(completed["status"], "Pass", completed)
        self.assertEqual(target.requests, [("/login", None, ""), ("/check", {"payload": inputs["payload"]}, "sid=fresh-http-session")])
        result = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (queued["id"], self.case["id"]))
        case = projects.case(self.pid, self.case["id"])
        self.assertTrue(execution_inputs.result_evidence(completed, result, case))
        self.assertFalse(execution_inputs.result_current(self.pid, case, result))
        matching = self.capture(inputs=inputs)
        self.assertTrue(execution_inputs.result_current(self.pid, case, result, current=matching))
        changed = self.capture(inputs={**inputs, "expected": False})
        self.assertFalse(execution_inputs.result_current(self.pid, case, result, current=changed))
        target.requests.clear()
        missing = runner.create(self.pid, [case["id"]], inputs={"expected": True})
        self.assertEqual(runner.execute(missing["id"])["status"], "Blocked")
        login = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (missing["id"], self.login["id"]))
        blocked = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (missing["id"], case["id"]))
        self.assertEqual(login["status"], "Pass")
        self.assertEqual(blocked["category"], "missing_variable")
        self.assertIn("{{payload}}", blocked["reason"])
        self.assertIn("--inputs", blocked["reason"])
        self.assertIn("提取结果", blocked["reason"])
        self.assertNotIn("没有成功", blocked["reason"])
        self.assertEqual(target.requests, [("/login", None, "")])
        case["assertions"]["json_equals"] = {"/ok": True}
        case["json"] = {"payload": inputs["payload"]}
        projects.save_cases(self.pid, [case])
        default_run = runner.create(self.pid, [case["id"]])
        self.assertTrue(execution_inputs.load(default_run)["api_only"])
        self.assertEqual(runner.execute(default_run["id"])["status"], "Pass")
        default_result = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (default_run["id"], case["id"]))
        self.assertTrue(execution_inputs.result_current(self.pid, projects.case(self.pid, case["id"]), default_result))
