"""Unified public state is backed by real HTTP attempts and independent artifacts."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, data_refs, db, execution_inputs, host_bindings, host_context, native_assets, native_run, projects, report_locations, runner, task_reports, task_state


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).calls += 1
        if self.path == "/disconnect":
            self.close_connection = True
            return
        time.sleep(.01)
        value = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        self.wfile.write(value)


class TaskStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.site.serve_forever, kwargs={"poll_interval": .01}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "workspace"), "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        self.project = projects.create("State contract", f"http://127.0.0.1:{self.site.server_port}", project_id="state-test")["id"]

    def execute(self, rules=None, *, path="/"):
        selected = rules if rules is not None else {"status": 200, "json_equals": {"/ok": True}}
        case = builder.api_case("TC-API-001", "Observed response", "GET", path, {"status": 200, "json_equals": {"/ok": True}, **selected})
        if not selected:
            case["extensions"]["oracle"] = {"source": "observation"}
        projects.save_cases(self.project, [case])
        final = runner.execute(runner.create(self.project)["id"])
        row = db.one("SELECT * FROM results WHERE run_id = ?", (final["id"],))
        return final, row

    def test_observed_checks_do_not_establish_business_pass(self):
        final, row = self.execute({})
        self.assertEqual(final["status"], "Pass")
        state = task_state.result(row)
        self.assertEqual((state["phase"], state["outcome"], state["checks"]["business"]), ("settled", "unverified", "unverified"))
        self.assertEqual(state["checks"]["performance"], "not_applicable")
        self.assertEqual(task_state.run(final)["outcome"], "unverified")

    def test_business_pass_and_sla_failure_have_separate_axes(self):
        final, row = self.execute({"json_equals": {"/ok": True}, "max_ms": 1})
        for state in (task_state.result(row), task_state.run(final)):
            self.assertEqual((state["lifecycle"], state["phase"], state["outcome"]), ("completed", "settled", "fail"))
            self.assertEqual(state["checks"], {"business": "pass", "performance": "fail", "quality": "not_applicable"})
            self.assertEqual(state["evidence"], "valid")

    def test_unmeasured_declared_target_is_unverified_rather_than_not_applicable(self):
        _run, declared = self.execute({"max_ms": 100}, path="/disconnect")
        _run, absent = self.execute(path="/disconnect")
        self.assertEqual(declared["status"], "Blocked")
        self.assertEqual(task_state.result(declared)["checks"]["performance"], "unverified")
        self.assertEqual(task_state.result(absent)["checks"]["performance"], "not_applicable")

    def test_malformed_projection_fields_do_not_hide_a_retained_failure(self):
        _final, row = self.execute({"json_equals": {"/ok": False}})
        detail = json.loads(row["detail"])
        variants = ({"business_status": []}, {"performance_status": {}}, {"quality_status": []},
                    {"last_attempt_status": {}}, {"performance_assertions": None}, {"assertions": [None]},
                    {"turns": [None, {"performance_assertions": None}]},
                    {"verdict_scope": "performance", "assertions": [None]})
        for fields in variants:
            with self.subTest(fields=fields):
                state = task_state.result({**row, "detail": json.dumps({**detail, **fields})})
                self.assertEqual(state["outcome"], "fail")
                self.assertEqual(state["checks"]["business"], "fail")
        self.assertEqual(db.one("SELECT * FROM results WHERE id = ?", (row["id"],)), row)

    def test_malformed_business_axis_does_not_establish_a_pass(self):
        _final, row = self.execute()
        detail = {**json.loads(row["detail"]), "business_status": []}
        state = task_state.result({**row, "detail": json.dumps(detail)})
        self.assertEqual((state["checks"]["business"], state["outcome"]), ("unverified", "unverified"))

    def test_mixed_malformed_items_retain_a_real_performance_failure(self):
        _final, row = self.execute({"max_ms": 1})
        detail = json.loads(row["detail"])
        self.assertTrue(any(item["passed"] is False for item in detail["performance_assertions"]))
        detail["performance_assertions"].insert(0, None)
        detail["assertions"].insert(0, None)
        detail["turns"] = [None, {"performance_assertions": [None]}]
        state = task_state.result({**row, "detail": json.dumps(detail)})
        self.assertEqual((state["checks"]["performance"], state["outcome"]), ("fail", "fail"))

    def test_edited_result_cannot_turn_real_failure_into_verified_pass(self):
        final, row = self.execute({"json_equals": {"/ok": False}})
        detail = json.loads(row["detail"])
        receipt = data_refs.resolve(detail["attempts"][0]["path"])
        original = receipt.read_bytes()
        detail.update(business_status="Pass", business_verified=True)
        for ref in detail["attempts"]:
            ref.update(status="Pass", business_status="Pass")
        altered = {**row, "status": "Pass", "detail": json.dumps(detail)}
        self.assertFalse(execution_inputs.result_evidence(final, altered))
        self.assertNotEqual(task_state.result(altered)["outcome"], "pass")
        self.assertEqual(receipt.read_bytes(), original)

    def test_receipt_loss_invalidates_cached_state_and_revision(self):
        final, row = self.execute()
        self.assertEqual(task_state.result(row)["evidence"], "valid")
        revision = task_state.revision()
        receipt = data_refs.resolve(json.loads(row["detail"])["attempts"][0]["path"])
        original = receipt.read_bytes()
        receipt.unlink()
        self.assertNotEqual(task_state.revision(), revision)
        self.assertEqual(task_state.result(row)["outcome"], "unverified")
        receipt.write_bytes(original)
        self.assertEqual(task_state.run(final)["outcome"], "pass")

    def test_concurrent_native_outcome_edit_cannot_poison_later_proof_reads(self):
        source = self.folder / "native-source"
        source.mkdir()
        (source / "test_fixture.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n def test_ok(self): self.assertEqual(2 + 2, 4)\n",
            encoding="utf-8")
        asset = native_assets.import_asset(self.project, str(source), "selenium-unittest")
        run_id = native_run.enqueue(self.project, asset["id"])["id"]
        native_run.execute(run_id)
        row = db.one("SELECT * FROM results WHERE run_id=?", (run_id,))
        self.assertEqual(task_state.result(row)["outcome"], "pass")
        outcome_file = self.folder / "workspace/.local/runs" / run_id / "native/outcome.json"
        read = task_state.watch_json
        changed = False

        def replace_after_read(file):
            nonlocal changed
            value = read(file)
            if file == outcome_file and not changed:
                replacement = json.loads(outcome_file.read_bytes())
                replacement["cases"][0]["status"] = "Fail"
                outcome_file.write_text(json.dumps(replacement), encoding="utf-8")
                changed = True
            return value

        with patch.object(task_state, "watch_json", side_effect=replace_after_read):
            task_state.result(row)
        self.assertTrue(changed)
        for _ in range(3):
            state = task_state.result(row)
            self.assertEqual((state["evidence"], state["outcome"]), ("invalid", "unverified"))

    def test_missing_bug_report_is_failed_on_read_without_regeneration(self):
        final, _row = self.execute()
        report = final["test_report"]
        Path(report["bugs"]).unlink()
        before = Handler.calls
        with patch("autoskill.reporting.write_task", side_effect=AssertionError("A read cannot render")) as render:
            self.assertEqual(task_reports.peek(report["report_id"])["status"], "failed")
            self.assertEqual(task_state.run(final)["report"], "failed")
            render.assert_not_called()
        repaired = task_reports.retry(report["report_id"])
        self.assertEqual(repaired["status"], "ready")
        self.assertEqual(Handler.calls, before)

    def test_project_report_location_binding_changes_revision_without_database_write(self):
        final, _row = self.execute()
        report_id = final["test_report"]["report_id"]
        location = report_locations._index(report_id)
        original = location.read_bytes()
        location.unlink()
        self.assertEqual(task_reports.peek(report_id)["status"], "not_generated")
        revision = task_state.revision()
        before = db.one("SELECT * FROM runs WHERE id=?", (final["id"],))
        location.write_bytes(original)
        self.assertNotEqual(task_state.revision(), revision)
        self.assertEqual(task_reports.peek(report_id)["status"], "ready")
        self.assertEqual(db.one("SELECT * FROM runs WHERE id=?", (final["id"],)), before)

    def test_failed_export_never_retries_when_run_metadata_changes(self):
        final, _row = self.execute()
        report = final["test_report"]
        folder = task_reports._folder(report["report_id"])
        saved = json.loads((folder / "receipt.json").read_bytes())
        saved.update(status="failed", error="Export interrupted", retryable=True)
        execution_inputs.write_json(folder / "receipt.json", saved)
        db.update("runs", final["id"], {"finished_at": "2099-01-01T00:00:00.000+00:00"})
        with patch("autoskill.reporting.write_task", side_effect=AssertionError("Only explicit retry may render")) as render:
            self.assertEqual(task_reports.for_run(final["id"])["status"], "failed")
            render.assert_not_called()

    def test_current_definition_change_does_not_rewrite_historical_run(self):
        final, row = self.execute()
        case = projects.case(self.project, row["case_id"])
        self.assertTrue(task_state.current_result(self.project, case, row)["current"])
        case["assertions"]["json_equals"]["/ok"] = False
        projects.save_cases(self.project, [case])
        current = task_state.current_result(self.project, projects.case(self.project, row["case_id"]), row)
        self.assertFalse(current["current"])
        self.assertEqual((current["outcome"], current["historical_outcome"]), ("unverified", "pass"))
        self.assertEqual(task_state.run(final)["outcome"], "pass")

    def test_light_host_revision_does_not_read_or_render_full_context(self):
        binding = host_bindings.upsert({"host": "dsh", "host_session_id": "state-host", "task_session_id": "state-task", "idempotency_key": "bind"})["binding"]
        with patch("autoskill.host_context._runs", side_effect=AssertionError("Version checks must stay light")):
            value = host_context.read("dsh", "state-host", binding_id=binding["id"], revision_only=True)
        self.assertEqual(set(value), {"host_session_id", "state_revision"})
        self.assertEqual(value["state_revision"], task_state.revision())

    def test_report_and_state_reads_preserve_all_artifact_bytes(self):
        final, row = self.execute()
        files = list((self.folder / "workspace").rglob("*.json"))
        before = {file: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}
        task_state.result(row)
        task_state.run(final)
        task_reports.peek(final["test_report"]["report_id"])
        self.assertEqual({file: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}, before)


if __name__ == "__main__":
    unittest.main()
