"""Generated candidates use real isolated HTTP/browser execution before library publication."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from autoskill import builder, candidate_publication, cli, db, execution_inputs, projects, runner, secrets, sessions
from autoskill.recorder import compile as compiler
from autoskill.recorder import session
from autoskill.server.app import create_app


class CandidateTarget(BaseHTTPRequestHandler):
    records: ClassVar[list] = []
    tokens: ClassVar[list] = []
    next_id = 40
    changing = 0

    def answer(self, status, payload, cookie=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        type(self).records.append((self.path, dict(self.headers), body))
        if self.path == "/login":
            token = "fresh-" + str(len(type(self).tokens) + 1)
            type(self).tokens.append(token)
            self.answer(200, {"token": token, "ok": True}, "session=" + token)
            return
        type(self).next_id += 1
        self.answer(201, {"id": type(self).next_id, "amount": body.get("amount"), "ok": True})

    def do_GET(self):
        type(self).records.append((self.path, dict(self.headers), None))
        if self.path == "/page":
            body = b'<html><body><p data-testid="value">ready</p></body></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/changing":
            type(self).changing += 1
            self.answer(200, {"ok": type(self).changing != 1})
            return
        if self.path == "/protected" and self.headers.get("Authorization") not in (
            "Bearer still-valid-recorded-token", "Bearer user-configured-persistent-key"
        ):
            self.answer(401, {"ok": False})
            return
        self.answer(200, {"ok": True, "id": type(self).next_id, "value": "medium"})

    def do_DELETE(self):
        type(self).records.append((self.path, dict(self.headers), None))
        self.answer(200, {"deleted": True})

    def log_message(self, *_args):
        return


class CandidatePublicationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": folder.name, "AUTOSKILL_SESSION": "candidate-tests", "AUTOSKILL_HEADED": "0",
                                              "AUTOSKILL_BRIDGE": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CandidateTarget)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        CandidateTarget.records, CandidateTarget.tokens, CandidateTarget.changing = [], [], 0
        self.project = projects.create("候选发布", f"http://127.0.0.1:{self.server.server_port}", project_id="candidate-project")["id"]

    def api(self, identity, path="/value", expected=None, **kwargs):
        case = builder.api_case(identity, identity, kwargs.pop("method", "GET"), path,
                                expected or {"status": 200, "json_equals": {"/ok": True}}, **kwargs)
        case["extensions"]["execution"] = {"api_only": True}
        return case

    def staged(self, cases, auth=None):
        return candidate_publication.stage(self.project, cases, auth or {}, {"fixture": [case["id"] for case in cases]})

    def execute(self, task, **options):
        queued = candidate_publication.queue(task["id"], start=False, **options)
        final = [runner.execute(identity) for identity in queued["run_ids"]]
        return queued, final

    def test_mixed_api_results_publish_only_passed_dependency_closed_subset(self):
        good = self.api("API-GOOD")
        bad = self.api("API-FAIL", expected={"status": 200, "json_equals": {"/value": "high"}})
        dependent = self.api("API-DEPENDENT", depends_on=[bad["id"]])
        observed = self.api("API-OBSERVED")
        observed["extensions"]["oracle"] = {"source": "observation"}
        cases = [good, bad, dependent, observed]
        queued, final = self.execute(self.staged(cases))
        self.assertEqual(final[0]["status"], "Fail")
        self.assertEqual([case["id"] for case in projects.cases(self.project)], [good["id"]])
        self.assertEqual(projects.case(self.project, good["id"]), good)
        receipt = candidate_publication.receipt(queued["run_id"])
        self.assertEqual(receipt["published_ids"], [good["id"]])
        self.assertEqual(set(receipt["unpublished"]), {bad["id"], dependent["id"], observed["id"]})
        self.assertEqual(len(db.rows("SELECT * FROM results WHERE run_id = ?", (queued["run_id"],))), 4)
        self.assertTrue(Path(final[0]["test_report"]["excel"]).is_file())
        args = cli.parser().parse_args(["run-status", queued["run_id"], "--brief"])
        with patch.object(runner, "execute", side_effect=AssertionError("status cannot execute")), \
                patch("autoskill.task_reports.for_run", side_effect=AssertionError("status cannot export")):
            brief, code = cli.dispatch(args)
        self.assertEqual((code, brief["status"]), (0, "Fail"))
        self.assertTrue(brief["publication"]["complete"])
        self.assertEqual(brief["publication"], receipt)
        self.assertNotIn("results", brief)

    def test_earlier_failure_in_required_repetitions_is_never_promoted_by_later_pass(self):
        case = self.api("API-CHANGING", "/changing")
        task = self.staged([case])
        queued = candidate_publication.queue(task["id"], repetitions=2, start=False)
        self.assertEqual(runner.execute(queued["run_ids"][0])["status"], "Fail")
        self.assertEqual(projects.cases(self.project), [])
        self.assertEqual(runner.execute(queued["run_ids"][1])["status"], "Pass")
        self.assertEqual(projects.cases(self.project), [])
        receipt = candidate_publication.receipt(queued["run_ids"][1])
        self.assertEqual(receipt["published_ids"], [])
        self.assertIn(case["id"], receipt["unpublished"])

    def test_cold_http_login_create_read_cleanup_uses_typed_inputs_and_new_tokens(self):
        login = self.api("API-LOGIN", "/login", method="POST", json={"user": "fixture"}, extract={"token": "/token"})
        create = self.api("API-CREATE", "/items", method="POST", json={"amount": "{{amount}}"},
                          headers={"Authorization": "Bearer {{token}}"}, depends_on=[login["id"]], extract={"item_id": "/id"},
                          expected={"status": 201, "json_equals": {"/amount": "{{amount}}"}})
        read = self.api("API-READ", "/items/{{item_id}}", headers={"Authorization": "Bearer {{token}}"}, depends_on=[create["id"]],
                        expected={"status": 200, "json_equals": {"/id": "{{item_id}}"}})
        delete = self.api("API-DELETE", "/items/{{item_id}}", method="DELETE", depends_on=[read["id"]],
                          expected={"status": 200, "json_equals": {"/deleted": True}})
        cases = [login, create, read, delete]
        task = self.staged(cases, {"api_case": login["id"]})
        saved = {"cookies": [{"name": "session", "value": "poison", "domain": "127.0.0.1", "path": "/"}], "origins": []}
        sessions.save(self.project, ui=saved, variables={"token": "poison"})
        before = sessions.load(self.project)
        with patch.object(runner, "make_driver", side_effect=AssertionError("HTTP must not start a browser")):
            for _ in range(2):
                queued, final = self.execute(task, inputs={"amount": 7})
                self.assertEqual(final[0]["status"], "Pass")
                self.assertEqual(set(candidate_publication.receipt(queued["run_id"])["published_ids"]), {case["id"] for case in cases})
        self.assertEqual(sessions.load(self.project), before)
        self.assertEqual(CandidateTarget.tokens, ["fresh-1", "fresh-2"])
        creates = [row for row in CandidateTarget.records if row[0] == "/items"]
        self.assertEqual([row[2]["amount"] for row in creates], [7, 7])
        self.assertNotIn("poison", json.dumps(CandidateTarget.records))
        self.assertEqual(projects.auth(self.project)["api_case"], login["id"])

    def test_compilation_keeps_failed_recording_and_auth_out_of_the_library(self):
        recording = {"project_id": self.project, "session_id": "fixture", "started_at": db.now(), "start_url": "/page", "as_login": True,
                     "steps": [{"assertion": "text", "locators": [{"by": "testid", "value": "value"}], "expected": "ready",
                                "recording_result": {"passed": False, "actual": "wrong", "expected": "ready"}}]}
        compiled = compiler.build(self.project, recording, {"log": {"entries": []}}, None, "页面检查")
        self.assertEqual(projects.cases(self.project), [])
        self.assertFalse(projects.auth(self.project).get("ui_case"))
        task = candidate_publication.stage(self.project, compiled["candidates"], compiled["candidate_auth"], compiled["candidate_source"],
                                            candidate_id=compiled["candidate_id"])
        queued, final = self.execute(task)
        self.assertEqual(final[0]["status"], "Pass")
        self.assertEqual(projects.cases(self.project), [])
        self.assertIn("录制已观察到业务失败", candidate_publication.receipt(queued["run_id"])["unpublished"][compiled["ui_case"]["id"]])

    def test_status_is_read_only_and_successful_ui_is_independent_of_failed_api(self):
        ui = builder.ui_case("UI-GOOD", "页面检查", {"start_url": "/page", "actions": [], "assertions": [
            {"assertion": "text", "locators": [{"by": "testid", "value": "value"}], "expected": "ready"}]})
        bad = self.api("API-BAD", expected={"status": 200, "json_equals": {"/value": "wrong"}})
        queued = candidate_publication.queue(self.staged([ui, bad])["id"], start=False)
        with patch.object(runner, "execute", side_effect=AssertionError("status cannot execute")):
            args = cli.parser().parse_args(["run-status", queued["run_id"]])
            status, code = cli.dispatch(args)
            brief, brief_code = cli.dispatch(cli.parser().parse_args(["run-status", queued["run_id"], "--brief"]))
        self.assertEqual((code, status["status"]), (0, "Queued"))
        self.assertEqual((brief_code, brief["state"], brief["publication"]), (0, status["state"], status["publication"]))
        self.assertFalse(brief["publication"]["complete"])
        for identity in queued["run_ids"]:
            runner.execute(identity)
        self.assertEqual([case["id"] for case in projects.cases(self.project)], [ui["id"]])
        self.assertEqual(candidate_publication.receipt(queued["run_id"])["published_ids"], [ui["id"]])

    def test_conflict_does_not_replace_an_existing_user_case(self):
        case = self.api("API-CONFLICT")
        queued = candidate_publication.queue(self.staged([case])["id"], start=False)
        edited = copy.deepcopy(case)
        edited["title"] = "用户自己的版本"
        projects.save_cases(self.project, [edited])
        runner.execute(queued["run_id"])
        self.assertEqual(projects.case(self.project, case["id"])["title"], edited["title"])
        self.assertEqual(candidate_publication.receipt(queued["run_id"])["published_ids"], [])

    def test_incomplete_completion_recovers_without_repeating_business_requests(self):
        case = self.api("API-RECOVER")
        queued = candidate_publication.queue(self.staged([case])["id"], start=False)
        with patch.object(runner, "sync_plan"):
            runner.execute(queued["run_id"])
        self.assertFalse(candidate_publication.receipt(queued["run_id"])["complete"])
        self.assertEqual(projects.cases(self.project), [])
        requests = len(CandidateTarget.records)
        runner.recover_runs()
        self.assertEqual(len(CandidateTarget.records), requests)
        receipt = candidate_publication.receipt(queued["run_id"])
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["published_ids"], [case["id"]])

    def test_unresolved_api_target_does_not_prevent_an_independent_target(self):
        good, captured = self.api("API-INDEPENDENT"), self.api("API-CAPTURED")
        captured["extensions"]["execution"]["unresolved_recorded_credentials"] = ["CAPTURED_TOKEN"]
        queued, final = self.execute(self.staged([good, captured]))
        self.assertEqual([run["status"] for run in final], ["Pass"])
        receipt = candidate_publication.receipt(queued["run_id"])
        self.assertEqual(receipt["published_ids"], [good["id"]])
        self.assertIn("来源", receipt["unpublished"][captured["id"]])

    def test_cli_candidate_inputs_and_status_are_a_complete_public_entry(self):
        case = self.api("API-CLI", "/items", method="POST", json={"amount": "{{amount}}"},
                        expected={"status": 201, "json_equals": {"/amount": "{{amount}}"}})
        task = self.staged([case])
        command = [sys.executable, "-m", "autoskill"]
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "skills/autoskill/app")}
        executed = subprocess.run([*command, "run", self.project, "--candidate", task["id"], "--api-only", "--inputs", "-"],
                                  input='{"amount":17}', capture_output=True, text=True, timeout=45, check=False, env=environment)
        self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
        receipt = json.loads(executed.stdout)
        self.assertTrue(receipt["publication"]["complete"])
        self.assertEqual(receipt["published_ids"], [case["id"]])
        self.assertEqual(CandidateTarget.records[-1][2]["amount"], 17)
        before = len(CandidateTarget.records)
        poll = [*command, "run-status", receipt["run_id"]]
        status = subprocess.run(poll, capture_output=True, text=True, timeout=20, check=False, env=environment)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        full = json.loads(status.stdout)
        self.assertEqual(full["publication"]["published_ids"], [case["id"]])
        self.assertEqual(full["results"][0]["case_id"], case["id"])
        status = subprocess.run([*poll, "--brief"], capture_output=True, text=True, timeout=20, check=False, env=environment)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        brief = json.loads(status.stdout)
        self.assertNotIn("results", brief)
        self.assertEqual({key: value for key, value in brief.items() if key != "test_report"},
                         {key: value for key, value in full.items() if key not in ("results", "test_report")})
        self.assertEqual(brief["test_report"]["status"], "ready")
        self.assertEqual(brief["test_report"]["report_id"], "run_" + receipt["run_id"])
        self.assertEqual(brief["test_report"]["excel"], full["test_report"]["excel"])
        self.assertNotIn("validation", brief["test_report"])
        self.assertEqual(len(CandidateTarget.records), before)

    def test_failed_candidate_rerun_api_keeps_version_scope_and_old_failed_excel(self):
        passed, failed = self.api("API-ALREADY-PASSED"), self.api("API-FIXABLE", "/changing")
        queued, initial = self.execute(self.staged([passed, failed]), inputs={"amount": 17})
        old_run = initial[0]
        self.assertEqual(old_run["status"], "Fail")
        self.assertEqual([case["id"] for case in projects.cases(self.project)], [passed["id"]])
        old_excel = Path(old_run["test_report"]["excel"])
        old_bytes = old_excel.read_bytes()
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            detail = client.get("/api/runs/" + old_run["id"]).json()
            self.assertEqual(detail["publication"]["candidate_id"], queued["candidate_id"])
            with patch("autoskill.server.jobs.ensure_worker") as dispatch, patch.dict(os.environ, {"AUTOSKILL_SESSION": "new-unrelated-task"}):
                response = client.post("/api/runs/" + old_run["id"] + "/rerun", json={"failed_only": True, "headed": False})
            self.assertEqual(response.status_code, 200, response.text)
            dispatch.assert_called_once()
            next_run = response.json()
            self.assertEqual(next_run["candidate_id"], queued["candidate_id"])
            self.assertEqual((next_run["total"], len(next_run["run_ids"])), (1, 1))
            stored = db.one("SELECT * FROM runs WHERE id = ?", (next_run["id"],))
            self.assertEqual(json.loads(stored["case_ids"]), [failed["id"]])
            self.assertEqual(str(runner.plan_folder(stored["plan_path"])), old_run["plan_path"])
            self.assertEqual(stored["session_id"], old_run["session_id"])
            self.assertEqual(execution_inputs.load(stored)["inputs"], {"amount": 17})
            final = runner.execute(next_run["id"])
            self.assertEqual(final["status"], "Pass")
            self.assertEqual(final["publication"]["published_ids"], [failed["id"]])
            self.assertEqual(projects.case(self.project, failed["id"]), failed)
            self.assertEqual(client.get("/api/runs/" + old_run["id"]).json()["status"], "Fail")
        self.assertEqual(old_excel.read_bytes(), old_bytes)

    def test_candidate_rerun_with_no_preparable_target_returns_complete_reasons_without_run_id(self):
        queued, _initial = self.execute(self.staged([self.api("API-READY")]))
        count = len(db.rows("SELECT id FROM runs"))
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client, \
                patch.object(execution_inputs, "capture", side_effect=ValueError("登录来源尚未补齐")), \
                patch("autoskill.server.jobs.ensure_worker") as dispatch:
            response = client.post("/api/runs/" + queued["run_id"] + "/rerun", json={})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIsNone(result["id"])
        self.assertEqual((result["total"], result["run_ids"]), (0, []))
        self.assertTrue(result["publication"]["complete"])
        self.assertEqual(result["publication"]["unpublished"], {"API-READY": "登录来源尚未补齐"})
        dispatch.assert_not_called()
        self.assertEqual(len(db.rows("SELECT id FROM runs")), count)

    def test_workflow_run_rerun_stays_in_its_original_task(self):
        queued, _initial = self.execute(self.staged([self.api("API-WORKFLOW")]))
        db.execute("UPDATE runs SET trigger = 'workflow', session_id = 'workflow-original' WHERE id = ?", (queued["run_id"],))
        count = len(db.rows("SELECT id FROM runs"))
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            detail = client.get("/api/runs/" + queued["run_id"]).json()
            self.assertEqual(detail["workflow_id"], "workflow-original")
            response = client.post("/api/runs/" + queued["run_id"] + "/rerun", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("原测试任务", response.json()["detail"])
        self.assertNotIn("归档", response.json()["detail"])
        self.assertEqual(len(db.rows("SELECT id FROM runs")), count)

    def test_existing_observation_only_dependency_does_not_qualify_a_new_candidate(self):
        dependency = self.api("API-OBSERVED-LOGIN")
        dependency["extensions"]["oracle"] = {"source": "observation"}
        projects.save_cases(self.project, [dependency])
        candidate = self.api("API-NEW", depends_on=[dependency["id"]])
        queued, final = self.execute(self.staged([candidate]))
        self.assertEqual(final[0]["status"], "Pass")
        self.assertEqual([case["id"] for case in projects.cases(self.project)], [dependency["id"]])
        self.assertIn("前置用例", candidate_publication.receipt(queued["run_id"])["unpublished"][candidate["id"]])

    def test_auth_only_frozen_extra_does_not_invalidate_its_actual_cold_execution(self):
        ui = builder.ui_case("UI-VIEW", "查看页面", {"start_url": "/page", "actions": [], "assertions": [
            {"assertion": "text", "locators": [{"by": "testid", "value": "value"}], "expected": "ready"}]})
        login = self.api("API-LOGIN", "/login", method="POST", extract={"token": "/token"})
        queued, final = self.execute(self.staged([ui, login], {"api_case": login["id"]}))
        self.assertEqual([run["status"] for run in final], ["Pass", "Pass"])
        receipt = candidate_publication.receipt(queued["run_id"])
        self.assertEqual(set(receipt["published_ids"]), {ui["id"], login["id"]})
        self.assertTrue(receipt["complete"])

    def test_real_record_stop_queues_private_candidate_and_pass_later_publishes(self):
        session.start(self.project, login=False, url="/page")
        observed = session.check("text", "testid=value", "ready", timeout_seconds=2)
        self.assertTrue(observed["passed"])
        with patch("autoskill.server.jobs.ensure_worker", return_value={"status": "fixture-queued"}):
            recorded = session.stop("页面显示预期内容")
        self.assertEqual(projects.cases(self.project), [])
        self.assertFalse(recorded["publication"]["complete"])
        self.assertGreaterEqual(len(recorded["run_ids"]), 1)
        completed = [runner.execute(identity) for identity in recorded["run_ids"]]
        self.assertEqual(completed[0]["status"], "Pass")
        receipt = candidate_publication.receipt(recorded["run_id"])
        self.assertEqual(receipt["published_ids"], [recorded["ui_case"]["id"]])
        self.assertTrue(receipt["complete"])
        self.assertEqual(projects.case(self.project, recorded["ui_case"]["id"]), recorded["candidates"][0])

    def test_captured_live_token_is_not_a_declared_persistent_api_key(self):
        token = "still-valid-recorded-token"
        base = projects.config(self.project)["base_url"]
        self.assertEqual(httpx.get(base + "/protected", headers={"Authorization": "Bearer " + token}, trust_env=False).status_code, 200)
        recording = {"project_id": self.project, "session_id": "credential-fixture", "started_at": db.now(), "start_url": "/page",
                     "steps": [{"assertion": "text", "locators": [{"by": "testid", "value": "value"}], "expected": "ready"}]}
        traffic = {"log": {"entries": [{"_resourceType": "fetch", "request": {"method": "GET",
                   "url": base + "/protected", "headers": [{"name": "Authorization", "value": "Bearer " + token}]},
                   "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"ok":true}'}}}]}}
        captured = compiler.build(self.project, recording, traffic, None, "捕获了有效令牌")
        api_case = next(case for case in captured["candidates"] if case["kind"] == "api")
        self.assertTrue(api_case["extensions"]["execution"]["unresolved_recorded_credentials"])
        with self.assertRaisesRegex(ValueError, "录制|凭据|来源"):
            runner.prepare(self.project, [api_case["id"]], candidate_cases=captured["candidates"], api_only=True)
        secrets.set_secret("EXPLICIT_API_KEY", "user-configured-persistent-key")
        declared = copy.deepcopy(api_case)
        declared["headers"]["Authorization"] = "Bearer ${ENV:EXPLICIT_API_KEY}"
        declared["extensions"]["execution"] = {"api_only": True}
        declared["extensions"].pop("oracle", None)
        run = runner.create(self.project, [declared["id"]], candidate_cases=[declared], api_only=True)
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        self.assertTrue(execution_inputs.load(run)["api_only"])
