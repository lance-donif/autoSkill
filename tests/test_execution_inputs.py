"""Real HTTP executions qualify only retained, immutable inputs and independent attempts."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, data_refs, db, domain, execution_inputs, paths, plan, projects, reporting, runner, secrets
from autoskill.core import store


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("X-Test-Credential")))
        status = 200
        if self.path == "/protected" and sum(path == "/protected" for path, _ in self.server.requests) == 1:
            status = 401
        data = json.dumps({"ok": True, "id": "created", "priority": "medium", "token": "synthetic-session"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ExecutionInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0",
                                              "AUTOSKILL_CASE_SKILL": str(self.folder / "selected-plan")})
        environment.start()
        self.addCleanup(environment.stop)
        self.server.requests = []
        self.pid = projects.create("Input evidence", f"http://127.0.0.1:{self.server.server_port}", project_id="inputs")["id"]
        self.case = builder.api_case("TC-API-001", "Business result", "GET", "/business", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [self.case])
        self.case = projects.case(self.pid, self.case["id"])

    def execute(self, prepared=None):
        run = runner.enqueue(prepared) if prepared else runner.create(self.pid, [self.case["id"]])
        final = runner.execute(run["id"])
        results = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run["id"],))
        return final, results

    def overwrite(self, cases):
        for kind, filename in store.FILES.items():
            store.write(projects.path(self.pid) / "testcases" / filename, [case for case in cases if case["kind"] == kind])

    def test_prepare_has_no_database_writes_and_enqueue_joins_callers_transaction(self):
        before = db.rows("SELECT * FROM projects")
        with patch.object(db, "connect", side_effect=AssertionError("Preparation must not touch SQLite")):
            prepared = runner.prepare(self.pid)
        self.assertEqual(db.rows("SELECT * FROM projects"), before)
        self.assertEqual(db.rows("SELECT * FROM runs"), [])
        self.assertTrue(Path(prepared["input_manifest"]).is_file())
        with self.assertRaisesRegex(ValueError, "rollback"), db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            runner.enqueue(prepared, connection=conn)
            raise ValueError("rollback")
        self.assertEqual(db.rows("SELECT * FROM runs"), [])
        runner.enqueue(prepared)
        self.assertEqual(len(db.rows("SELECT * FROM runs")), 1)

    def test_execution_uses_frozen_dependency_config_credentials_and_case(self):
        prerequisite = builder.api_case("TC-API-002", "Prerequisite", "GET", "/before", {"status": 200, "json_equals": {"/ok": True}}, extract={"id": "/id"})
        self.case["headers"] = {"X-Test-Credential": "${ENV:SNAPSHOT_TEST}"}
        self.case["extensions"]["depends_on"] = [prerequisite["id"]]
        projects.save_cases(self.pid, [self.case, prerequisite])
        secrets.set_secret("SNAPSHOT_TEST", "frozen-synthetic")
        prepared = runner.prepare(self.pid, [self.case["id"]])
        old = execution_inputs.load(prepared)
        changed = copy.deepcopy(old["cases"])
        changed[self.case["id"]]["path"] = "/after"
        changed[prerequisite["id"]]["path"] = "/changed-prerequisite"
        self.overwrite(list(changed.values()))
        config = projects.config(self.pid)
        config["base_url"] = "http://127.0.0.1:1"
        store.write(projects.path(self.pid) / "qa-config.json", config)
        secrets.set_secret("SNAPSHOT_TEST", "changed-synthetic")
        final, results = self.execute(prepared)
        self.assertEqual(final["status"], "Pass")
        self.assertEqual(self.server.requests, [("/before", None), ("/business", "frozen-synthetic")])
        self.assertTrue(execution_inputs.result_evidence(final, results[-1], old["cases"][self.case["id"]]))
        self.assertFalse(execution_inputs.result_current(self.pid, changed[self.case["id"]], results[-1]))

    def test_same_revision_import_cannot_reuse_a_pass_or_a_dependency_pass(self):
        prerequisite = builder.api_case("TC-API-002", "Prerequisite", "GET", "/before", {"status": 200, "json_equals": {"/ok": True}})
        self.case["extensions"]["depends_on"] = [prerequisite["id"]]
        projects.save_cases(self.pid, [self.case, prerequisite])
        self.case = projects.case(self.pid, self.case["id"])
        final, results = self.execute()
        self.assertTrue(execution_inputs.result_current(self.pid, self.case, results[-1]))
        current = projects.cases(self.pid)
        next(case for case in current if case["id"] == prerequisite["id"])["path"] = "/different"
        self.overwrite(current)
        self.assertFalse(execution_inputs.result_current(self.pid, self.case, results[-1]))
        changed = copy.deepcopy(self.case)
        changed["assertions"]["json_equals"]["/ok"] = False
        self.overwrite([changed, prerequisite])
        self.assertEqual(changed["revision"], results[-1]["case_revision"])
        self.assertFalse(execution_inputs.result_current(self.pid, changed, results[-1]))
        self.assertEqual(reporting.build(self.pid)["passed"], 0)
        self.assertEqual(final["status"], "Pass")

    def test_candidates_are_tested_without_publishing_and_real_business_failure_is_evidence(self):
        candidate = copy.deepcopy(self.case)
        candidate["revision"] += 1
        candidate["assertions"]["json_equals"] = {"/priority": "high"}
        prepared = runner.prepare(self.pid, [candidate["id"]], candidate_cases=[candidate])
        final, results = self.execute(prepared)
        self.assertEqual(final["status"], "Fail")
        self.assertEqual(projects.case(self.pid, self.case["id"]), self.case)
        self.assertTrue(execution_inputs.result_evidence(final, results[0], candidate))
        detail = json.loads(results[0]["detail"])
        self.assertEqual((detail["verification"], detail["business_status"], detail["business_verified"]), ("verified", "Fail", False))

    def test_unresolved_expected_variable_blocks_before_sending_a_request(self):
        self.case["assertions"]["json_equals"] = {"/ok": "{{expected_ok}}"}
        projects.save_cases(self.pid, [self.case])
        final, results = self.execute()
        self.assertEqual((final["status"], results[0]["category"]), ("Blocked", "missing_variable"))
        self.assertIn("expected_ok", results[0]["reason"])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(json.loads(results[0]["detail"])["business_status"], "Unverified")

    def test_expected_variable_preserves_the_type_extracted_by_a_prerequisite(self):
        prerequisite = builder.api_case("TC-API-002", "Expected value", "GET", "/expected", {"status": 200, "json_equals": {"/ok": True}},
                                        extract={"expected_ok": "/ok"})
        self.case["extensions"]["depends_on"] = [prerequisite["id"]]
        self.case["assertions"]["json_equals"] = {"/ok": "{{expected_ok}}"}
        projects.save_cases(self.pid, [self.case, prerequisite])
        final, results = self.execute()
        self.assertEqual(final["status"], "Pass")
        actual = json.loads(results[-1]["detail"])["assertions"][-1]
        self.assertIs(actual["expected"], True)
        self.assertIs(actual["actual"], True)

    def test_sources_are_frozen_and_derived_knowledge_does_not_invalidate_its_own_run(self):
        prefix = "workflows/test/"
        changes = {prefix + "sources.json": {"documents": ["requirement"]}, prefix + "requirements.json": [{"expected": "high"}],
                   prefix + "definition.json": {"goal": "priority", "validation": {"repetitions": 1}, "version": 1},
                   prefix + "knowledge.json": [], prefix + "artifacts/source.bin": b"original-source"}
        head = domain.commit(self.pid, changes, expected_head=None)["head"]
        final, results = self.execute()
        self.assertTrue(execution_inputs.result_current(self.pid, self.case, results[0]))
        changed = {prefix + "knowledge.json": [{"finding": "preserved"}], prefix + "diagnosis.json": {"status": "done"},
                   "knowledge/settings.json": {"schema_version": 1, "team_id": "alpha", "business_scope": "orders"},
                   "knowledge/entries/entry-1/revisions/1.json": {"text": "derived observation"},
                   prefix + "definition.json": {"goal": "priority", "validation": {"repetitions": 1}, "version": 2, "change_id": "new"}}
        head = domain.commit(self.pid, changed, expected_head=head)["head"]
        self.assertTrue(execution_inputs.result_current(self.pid, self.case, results[0]))
        domain.commit(self.pid, {prefix + "requirements.json": [{"expected": "medium"}]}, expected_head=head)
        self.assertFalse(execution_inputs.result_current(self.pid, self.case, results[0]))
        manifest = execution_inputs.load(final)
        ref = manifest["sources"]["files"]["domain/" + prefix + "artifacts/source.bin"]
        self.assertEqual((paths.local() / "execution-inputs/objects" / ref["sha256"]).read_bytes(), b"original-source")

    def test_missing_or_changed_attempt_evidence_cannot_qualify_as_current(self):
        final, results = self.execute()
        self.assertTrue(execution_inputs.result_evidence(final, results[0]))
        ref = json.loads(results[0]["detail"])["attempts"][0]
        file = data_refs.resolve(ref["path"])
        receipt = json.loads(file.read_bytes())
        receipt["outcome"]["status"] = "Fail"
        execution_inputs.write_json(file, receipt)
        self.assertFalse(execution_inputs.result_evidence(final, results[0]))
        file.unlink()
        self.assertFalse(execution_inputs.result_current(self.pid, self.case, results[0]))

    def test_a_completed_assertion_free_ui_run_counts_as_passed(self):
        case = builder.ui_case("TC-UI-001", "Actions only", {"start_url": "/", "actions": [], "assertions": []})
        projects.save_cases(self.pid, [case])
        prepared = runner.prepare(self.pid, [case["id"]])
        driver = Mock()
        driver.screenshot.side_effect = lambda file: file.write_bytes(b"synthetic-page")
        with patch.object(runner, "make_driver", return_value=driver):
            final, results = self.execute(prepared)
        self.assertEqual(final["status"], "Pass")
        detail = json.loads(results[0]["detail"])
        self.assertTrue(detail["target_started"])
        self.assertFalse(any(step.get("check") for step in detail["steps"]))
        self.assertTrue(execution_inputs.result_evidence(final, results[0]))
        self.assertEqual(reporting.build(self.pid)["passed"], 1)

    def test_a_self_reported_pass_without_attempt_receipts_is_not_execution_evidence(self):
        run = runner.create(self.pid, [self.case["id"]])
        runner.record(run, self.case, {"status": "Pass", "assertions": [{"passed": True}]}, False, db.now())
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        self.assertFalse(execution_inputs.result_evidence(run, result))
        self.assertFalse(execution_inputs.result_current(self.pid, self.case, result))

    def test_malformed_detail_and_attempt_shapes_are_invalid_evidence(self):
        final, results = self.execute()
        result = results[0]
        self.assertTrue(execution_inputs.result_evidence(final, result))
        detail = json.loads(result["detail"])
        variants = [[], None, 1, *[{**detail, "attempts": value} for value in (None, 1, {}, [None], [{}])]]
        for malformed in variants:
            with self.subTest(detail=malformed):
                altered = {**result, "detail": json.dumps(malformed)}
                self.assertFalse(execution_inputs.result_evidence(final, altered))
                self.assertFalse(execution_inputs.result_current(self.pid, self.case, altered))
        self.assertEqual(db.one("SELECT * FROM results WHERE id = ?", (result["id"],)), result)
        self.assertTrue(execution_inputs.result_evidence(final, result))

    def test_malformed_receipt_and_outcome_shapes_are_invalid_evidence(self):
        final, results = self.execute()
        result = results[0]
        detail = json.loads(result["detail"])
        file = data_refs.resolve(detail["attempts"][0]["path"])
        original = file.read_bytes()
        receipt = json.loads(original)
        self.assertTrue(execution_inputs.result_evidence(final, result))
        try:
            for malformed in ([], None, {**receipt, "outcome": []}, {**receipt, "outcome": None}):
                with self.subTest(receipt=malformed):
                    file.write_text(json.dumps(malformed), encoding="utf-8")
                    altered_detail = copy.deepcopy(detail)
                    altered_detail["attempts"][0]["sha256"] = execution_inputs.content_hash(malformed)
                    altered = {**result, "detail": json.dumps(altered_detail)}
                    self.assertFalse(execution_inputs.result_evidence(final, altered))
                    self.assertFalse(execution_inputs.result_current(self.pid, self.case, altered))
        finally:
            file.write_bytes(original)
        self.assertEqual(db.one("SELECT * FROM results WHERE id = ?", (result["id"],)), result)
        self.assertTrue(execution_inputs.result_evidence(final, result))

    def test_tampered_frozen_input_is_blocked_before_any_request(self):
        prepared = runner.prepare(self.pid)
        file = Path(prepared["input_manifest"])
        content = json.loads(file.read_bytes())
        content["cases"][self.case["id"]]["path"] = "/tampered"
        file.write_text(json.dumps(content))
        final, results = self.execute(prepared)
        self.assertEqual((final["status"], results, self.server.requests), ("Blocked", [], []))

    def test_api_relogin_retains_the_failed_attempt_before_the_success(self):
        login = builder.api_case("TC-API-002", "Login", "GET", "/login", {"status": 200, "json_equals": {"/ok": True}}, extract={"token": "/token"})
        self.case["path"] = "/protected"
        self.case["extensions"]["depends_on"] = [login["id"]]
        projects.save_cases(self.pid, [self.case, login])
        projects.set_auth(self.pid, {"api_case": login["id"]})
        final, results = self.execute()
        self.assertEqual(final["status"], "Pass")
        refs = json.loads(results[-1]["detail"])["attempts"]
        self.assertEqual([ref["status"] for ref in refs], ["Fail", "Pass"])
        self.assertEqual(len({ref["path"] for ref in refs}), 2)
        first = json.loads(data_refs.resolve(refs[0]["path"]).read_bytes())
        self.assertEqual(first["outcome"]["response"]["status"], 401)
        self.assertTrue(execution_inputs.result_evidence(final, results[-1]))

    def test_captcha_retry_keeps_distinct_screenshots_and_first_business_failure(self):
        case = builder.ui_case("TC-UI-001", "Retry", {"start_url": "/", "actions": [
            {"action": "captcha", "selector": "#captcha", "image": [{"by": "css", "value": "#image"}]}],
            "assertions": [{"assertion": "text", "selector": "#priority", "expected": "high"}]})
        projects.save_cases(self.pid, [case])
        prepared = runner.prepare(self.pid, [case["id"]])
        driver = Mock()
        outcomes = iter([False, True])
        def replay(_driver, _case, _config, folder, screenshot):
            passed = next(outcomes)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / screenshot).write_bytes(b"true" if passed else b"false")
            return {"status": "Pass" if passed else "Fail", "category": "" if passed else "assertion_failed",
                    "reason": "" if passed else "Expected high, observed medium", "screenshots": [screenshot],
                    "assertions": [{"name": "Priority", "passed": passed, "expected": "high", "actual": "high" if passed else "medium"}]}
        with patch.object(runner, "make_driver", return_value=driver), patch.object(runner.ui, "run_case", side_effect=replay):
            final, results = self.execute(prepared)
        self.assertEqual(final["status"], "Fail")
        detail = json.loads(results[0]["detail"])
        self.assertEqual((detail["business_status"], detail["last_attempt_status"]), ("Fail", "Pass"))
        shots = [json.loads(data_refs.resolve(ref["path"]).read_bytes())["outcome"]["screenshots"][0] for ref in detail["attempts"]]
        self.assertEqual(len(set(shots)), 2)
        self.assertTrue(execution_inputs.result_evidence(final, results[0]))

    def test_worker_syncs_original_plan_and_preserves_observation_only_check_provenance(self):
        source = self.folder / "design.json"
        source.write_text(json.dumps([{"id": "TC-001", "title": "Requirement"}]))
        plan.write(self.pid, str(source))
        plan.link("TC-001", self.pid, self.case["id"], 1)
        prepared = runner.prepare(self.pid)
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.folder / "unrelated-plan")}):
            final, results = self.execute(prepared)
        self.assertEqual(plan.show()["summary"]["通过"], 1)
        self.assertTrue(execution_inputs.result_evidence(final, results[0]))
        self.case["extensions"]["oracle"] = {"source": "observation"}
        projects.save_cases(self.pid, [self.case])
        final, results = self.execute()
        self.assertEqual(final["status"], "Pass")
        detail = json.loads(results[0]["detail"])
        self.assertFalse(detail["business_verified"])
        self.assertTrue(detail["execution_only_pass"])
        self.assertTrue(execution_inputs.result_evidence(final, results[0]))
        self.assertEqual(plan.show()["summary"]["通过"], 1)
        self.assertEqual(reporting.build(self.pid)["passed"], 1)


if __name__ == "__main__":
    unittest.main()
