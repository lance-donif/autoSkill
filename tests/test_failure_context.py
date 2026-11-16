"""Real runner receipts preserve all failures and fence host diagnoses to frozen evidence."""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, data_refs, db, execution_inputs, failure_context, projects, runner, workflow
from autoskill import workflow_execution as execution
from autoskill import workflow_state as state
from autoskill.replay import api


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        if self.path == "/read-timeout":
            time.sleep(0.2)
        body = json.dumps({"priority": self.server.priority, "ok": self.server.ok}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.commits += 1
        time.sleep(0.2)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "19")
            self.end_headers()
            self.wfile.write(b'{"priority":"high"}')
        except (BrokenPipeError, ConnectionResetError):
            pass


class InterruptedWorker(BaseException):
    """Model process death outside the runner's adapter-exception handler."""


class FailureContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": temporary.name, "AUTOSKILL_HEADED": "0",
                                              "AUTOSKILL_CASE_SKILL": str(self.folder / "plan")})
        environment.start()
        self.addCleanup(environment.stop)
        self.server.requests, self.server.priority, self.server.ok = [], "medium", True
        self.server.commits = 0
        self.project = projects.create("Failure evidence", f"http://127.0.0.1:{self.server.server_port}", project_id="failures")["id"]
        self.case = builder.api_case("TC-API-001", "优先级", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "high", "/ok": True}})
        projects.save_cases(self.project, [self.case])
        self.case = projects.case(self.project, self.case["id"])

    def run_case(self, case=None):
        run = runner.create(self.project, [(case or self.case)["id"]], candidate_cases=[case] if case else None)
        run = runner.execute(run["id"])
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        return run, result

    def summary(self, run, cases=None):
        cases = cases or [self.case]
        return execution._result_summary(run, {"case_ids": [case["id"] for case in cases],
                                                "case_hashes": {case["id"]: execution_inputs.content_hash(case) for case in cases}})

    def context(self, run, result):
        reference = json.loads(result["detail"])["attempts"][0]
        return failure_context.read(self.project, run_id=run["id"], execution_attempt_id=reference["id"], case_id=result["case_id"])

    def payload(self, manifest, disposition="business_defect"):
        projection = failure_context.workflow_context(self.project, manifest)
        evidence = {entry["target_id"]: entry for entry in projection["target_contexts"]}
        return {"manifest_hash": manifest["manifest_hash"], "diagnoses": [
            {"target_id": target["target_id"], "disposition": disposition, "reason": "根据冻结凭据分析", "evidence_refs": evidence[target["target_id"]]["evidence_refs"][:1]}
            for target in manifest["diagnostic_targets"]]}

    def assert_error(self, code, operation, *args, **kwargs):
        with self.assertRaises(state.WorkflowError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def submit(self, workflow_id, task, payload):
        return workflow.submit(workflow_id, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                               idempotency_key=uuid.uuid4().hex, payload=payload)

    def queue_workflow(self, repetitions=2, *, method="GET", path="/round"):
        identity = workflow.create(self.project, {"title": "重复验证", "sources": [{"id": "s1", "text": "优先级应为 high"}],
                                                  "validation": {"repetitions": repetitions}}, idempotency_key=uuid.uuid4().hex)["workflow_id"]
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        self.submit(identity, task, {"requirements": [{"id": "r1", "text": "优先级为 high", "source_ids": ["s1"]}]})
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        rules = {"status": 200, "json_equals": {"/priority": "high"}}
        self.submit(identity, task, {"scenarios": [{"id": "sc1", "title": "优先级", "requirement_ids": ["r1"], "oracle_ids": ["o1"]}],
                                     "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"], "checks": rules}]})
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        candidate = builder.api_case("TC-API-002", "重复验证", method, path, rules)
        self.submit(identity, task, {"cases": [candidate], "bindings": {"sc1": [candidate["id"]]}})
        workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
        run = db.one("SELECT run_id FROM workflow_items WHERE workflow_id = ? AND phase = 'execute'", (identity,))
        return identity, run["run_id"]

    def _lost_write_response(self, repetitions):
        projects.update(self.project, timeout_seconds=0.04)
        with patch.object(runner, "spawn", return_value=None), patch("autoskill.server.jobs.ensure_worker", return_value={}):
            identity, run_id = self.queue_workflow(repetitions, method="POST", path="/commit")
            self.assertEqual(runner.execute(run_id)["status"], "Blocked")
            task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
            self.assertEqual(task["phase"], "diagnose")
            self.assertEqual(self.server.commits, 1)
            manifest = task["context"]
            self.assertTrue(manifest["diagnostic_targets"][0]["effects_unknown"])
            self.assertTrue(workflow.diagnostic_context(identity)["target_contexts"][0]["effects_unknown"])
            legacy = copy.deepcopy(manifest)
            for target in legacy["diagnostic_targets"]:
                target.pop("effects_unknown", None)
                target["target_id"] = state.digest({key: value for key, value in target.items() if key != "target_id"})
            legacy["manifest_hash"] = state.digest({"schema_version": 1, "diagnostic_targets": legacy["diagnostic_targets"]})
            self.assert_error("evidence_unavailable", failure_context.validate_diagnoses, self.project, legacy, self.payload(legacy, "retry"))
            self.assert_error("evidence_unavailable", self.submit, identity, task, self.payload(manifest, "retry"))
            self.assert_error("evidence_unavailable", self.submit, identity, task, self.payload(manifest, "repair"))
            self.submit(identity, task, self.payload(manifest, "incomplete"))
            self.assertEqual(workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]["phase"], "knowledge")
            self.assertEqual(db.one("SELECT COUNT(*) n FROM workflow_items WHERE workflow_id = ? AND phase = 'execute'", (identity,))["n"], 1)
            self.assertEqual(self.server.commits, 1)

    def test_committed_write_with_lost_response_cannot_authorize_retry(self):
        self._lost_write_response(1)

    def test_committed_write_with_lost_response_stops_predeclared_repetitions(self):
        self._lost_write_response(3)

    def test_read_timeout_remains_eligible_for_evidence_bound_retry(self):
        projects.update(self.project, timeout_seconds=0.04)
        with patch.object(runner, "spawn", return_value=None), patch("autoskill.server.jobs.ensure_worker", return_value={}):
            identity, run_id = self.queue_workflow(1, path="/read-timeout")
            self.assertEqual(runner.execute(run_id)["status"], "Blocked")
            task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
            self.assertFalse(any(target.get("effects_unknown") for target in task["diagnostic_targets"]))
            result = self.submit(identity, task, self.payload(task["context"], "retry"))
            self.assertTrue(result["accepted"])
            self.assertEqual(self.server.commits, 0)

    def test_malformed_result_detail_still_produces_diagnosis_and_explicit_gap(self):
        for corruption in ([], {"attempts": 1}, {"attempts": []}, {"attempts": [{}]},
                           {"attempts": [None], "business_status": [], "performance_status": {}},
                           {"attempts": [{"id": []}]}, {"attempts": [{"id": "retained-id", "path": [], "sha256": {}}]}):
            with self.subTest(corruption=corruption), patch.object(runner, "spawn", return_value=None), \
                    patch("autoskill.server.jobs.ensure_worker", return_value={}):
                identity, run_id = self.queue_workflow(2)
                runner.execute(run_id)
                db.execute("UPDATE results SET detail = ? WHERE run_id = ?", (json.dumps(corruption), run_id))
                task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
                self.assertEqual(task["phase"], "diagnose")
                self.assertIn("evidence_gap", {target["type"] for target in task["diagnostic_targets"]})
                self.assertFalse(task["context"]["execution"]["evidence_valid"])
                self.assertEqual(db.one("SELECT COUNT(*) n FROM workflow_items WHERE workflow_id = ? AND phase = 'execute'", (identity,))["n"], 1)
                accepted = self.submit(identity, task, self.payload(task["context"], "incomplete"))
                self.assertTrue(accepted["accepted"])

    def test_real_http_context_points_to_original_assertion_without_running_again(self):
        run, result = self.run_case()
        before = db.rows("SELECT * FROM results")
        with patch.object(runner, "prepare", side_effect=AssertionError("read must not prepare")), \
                patch.object(runner, "execute", side_effect=AssertionError("read must not execute")), \
                patch.object(execution_inputs, "write_json", side_effect=AssertionError("read must not write")):
            context = self.context(run, result)
        self.assertEqual(self.server.requests, ["/priority"])
        self.assertEqual(db.rows("SELECT * FROM results"), before)
        failed = [row for row in context["assertions"] if row["passed"] is False]
        self.assertEqual([(row["expected"], row["actual"], row["pointer"]) for row in failed], [("high", "medium", "/outcome/assertions/2")])
        self.assertEqual(context["execution_attempt_id"], json.loads(result["detail"])["attempts"][0]["id"])
        self.assertTrue(context["historical"])
        self.assertEqual(context["missing_artifacts"], [])

    def test_all_failed_assertions_get_independent_targets_and_require_exact_coverage(self):
        self.server.ok = False
        run, _result = self.run_case()
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        self.assertEqual({target["pointer"] for target in manifest["diagnostic_targets"]}, {"/outcome/assertions/1", "/outcome/assertions/2"})
        self.assertEqual({target["type"] for target in manifest["diagnostic_targets"]}, {"assertion"})
        payload = self.payload(manifest)
        accepted = failure_context.validate_diagnoses(self.project, manifest, payload)
        self.assertEqual((accepted["disposition"], accepted["origin"]), ("business_defect", "host_analysis"))
        for diagnoses in (payload["diagnoses"][:1], [*payload["diagnoses"], payload["diagnoses"][0]],
                          [{**payload["diagnoses"][0], "target_id": "foreign"}, payload["diagnoses"][1]]):
            self.assert_error("evidence_scope_mismatch", failure_context.validate_diagnoses, self.project, manifest, {**payload, "diagnoses": diagnoses})

    def test_cross_attempt_cross_assertion_and_artifact_path_evidence_is_rejected(self):
        self.server.ok = False
        run, _result = self.run_case()
        other, other_result = self.run_case()
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        base = self.payload(manifest)
        variants = [self.context(other, other_result)["assertions"][1]["evidence_ref"], base["diagnoses"][1]["evidence_refs"][0],
                    {**base["diagnoses"][0]["evidence_refs"][0], "pointer": "/outcome/assertions/99"},
                    {"kind": "artifact", "run_id": run["id"], "case_id": self.case["id"], "artifact_id": "../../outside", "sha256": "fake"}]
        for reference in variants:
            payload = copy.deepcopy(base)
            payload["diagnoses"][0]["evidence_refs"] = [reference]
            self.assert_error("evidence_scope_mismatch", failure_context.validate_diagnoses, self.project, manifest, payload)
        self.assert_error("evidence_scope_mismatch", failure_context.read, self.project, run_id=run["id"],
                          execution_attempt_id="wfa-lease-is-not-an-execution-receipt", case_id=self.case["id"])

    def test_missing_and_tampered_main_receipts_produce_gaps_before_diagnosis(self):
        for change in ("missing", "tampered"):
            with self.subTest(change=change):
                run, result = self.run_case()
                summary = self.summary(run)
                reference = json.loads(result["detail"])["attempts"][0]
                file = data_refs.resolve(reference["path"])
                if change == "missing":
                    file.unlink()
                else:
                    receipt = json.loads(file.read_bytes())
                    receipt["outcome"]["assertions"][1]["actual"] = "high"
                    execution_inputs.write_json(file, receipt)
                self.assert_error("evidence_unavailable", self.context, run, result)
                manifest = failure_context.build_manifest(self.project, [summary])
                self.assertEqual(len(manifest["diagnostic_targets"]), 1)
                target = manifest["diagnostic_targets"][0]
                self.assertEqual((target["type"], target["execution_attempt_id"], target["expected_hash"]), ("evidence_gap", reference["id"], reference["sha256"]))
                self.assertNotIn("pointer", target)
                self.assert_error("evidence_unavailable", failure_context.validate_diagnoses, self.project, manifest, self.payload(manifest, "retry"))
                accepted = failure_context.validate_diagnoses(self.project, manifest, self.payload(manifest, "incomplete"))
                self.assertEqual(accepted["disposition"], "incomplete")
                self.assertEqual(self.summary(run)["conclusion"], "violated")

    def test_lost_evidence_after_manifest_freeze_only_accepts_incomplete(self):
        run, result = self.run_case()
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        payload = self.payload(manifest)
        reference = json.loads(result["detail"])["attempts"][0]
        data_refs.resolve(reference["path"]).unlink()
        self.assert_error("evidence_unavailable", failure_context.validate_diagnoses, self.project, manifest, payload)
        payload["diagnoses"][0].update(disposition="incomplete", evidence_refs=[])
        self.assertEqual(failure_context.validate_diagnoses(self.project, manifest, payload)["disposition"], "incomplete")
        self.assertEqual(manifest["diagnostic_targets"][0]["type"], "assertion")

    def test_optional_screenshot_loss_does_not_erase_business_failure(self):
        run = runner.create(self.project, [self.case["id"]])
        session = runner.Session(run)
        self.addCleanup(session.close)
        artifact = runner.evidence_dir(run["id"]) / "optional.png"

        def invoke(_attempt):
            outcome = api.run_case(session.client, self.case, session.config, session.variables)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"optional screenshot bytes")
            return {**outcome, "screenshots": [artifact.name]}

        outcome = session.attempt(self.case, "case", invoke)
        runner.record(run, self.case, session.summarize(self.case, outcome), False, db.now())
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        artifact.unlink()
        context = self.context(run, result)
        self.assertEqual(context["outcome"]["business_status"], "Fail")
        self.assertEqual(context["missing_artifacts"][0]["artifact_id"], "optional.png")
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        self.assertEqual({target["type"] for target in manifest["diagnostic_targets"]}, {"assertion"})
        self.assertEqual(failure_context.validate_diagnoses(self.project, manifest, self.payload(manifest))["disposition"], "business_defect")
        self.assertFalse(execution_inputs.result_evidence(run, result))
        self.assertEqual(self.summary(run)["conclusion"], "violated")

    def test_failure_before_any_assertion_uses_real_outcome_pointer(self):
        case = copy.deepcopy(self.case)
        case["path"] = "/{{unresolved}}"
        run, result = self.run_case(case)
        self.assertEqual(json.loads(result["detail"]).get("assertions", []), [])
        manifest = failure_context.build_manifest(self.project, [self.summary(run, [case])])
        target = manifest["diagnostic_targets"][0]
        self.assertEqual((target["type"], target["pointer"], target["failure_category"]), ("execution_error", "/outcome", "missing_variable"))
        accepted = failure_context.validate_diagnoses(self.project, manifest, self.payload(manifest, "repair"))
        self.assertEqual(accepted["disposition"], "repair")

    def test_started_journal_is_incomplete_not_a_fabricated_assertion(self):
        run = runner.create(self.project, [self.case["id"]])
        session = runner.Session(run)
        self.addCleanup(session.close)

        def interrupt(_attempt):
            api.run_case(session.client, self.case, session.config, session.variables)
            raise InterruptedWorker

        with self.assertRaises(InterruptedWorker):
            session.attempt(self.case, "case", interrupt)
        manifest = failure_context.build_manifest(self.project, [{"run_id": run["id"], "results": [{"case_id": self.case["id"]}]}])
        self.assertEqual(len(manifest["diagnostic_targets"]), 1)
        target = manifest["diagnostic_targets"][0]
        self.assertEqual((target["type"], target["state"], target["pointer"]), ("incomplete_attempt", "started", ""))
        self.assertEqual(self.server.requests, ["/priority"])
        self.assert_error("evidence_unavailable", failure_context.validate_diagnoses, self.project, manifest, self.payload(manifest, "retry"))
        accepted = failure_context.validate_diagnoses(self.project, manifest, self.payload(manifest, "incomplete"))
        self.assertEqual(accepted["disposition"], "incomplete")

    def test_historical_context_never_borrows_latest_success_or_live_case(self):
        failed, failed_result = self.run_case()
        changed = copy.deepcopy(self.case)
        changed["assertions"]["json_equals"]["/priority"] = "medium"
        projects.save_cases(self.project, [changed])
        passed, _result = self.run_case(changed)
        self.assertEqual(passed["status"], "Pass")
        historical = self.context(failed, failed_result)
        self.assertEqual((historical["run_id"], historical["assertions"][2]["expected"], historical["assertions"][2]["passed"]), (failed["id"], "high", False))
        manifest = failure_context.build_manifest(self.project, [self.summary(failed), self.summary(passed, [changed])])
        self.assertEqual({target["run_id"] for target in manifest["diagnostic_targets"]}, {failed["id"]})

    def test_two_attempts_preserve_first_fail_even_when_retry_passes(self):
        run = runner.create(self.project, [self.case["id"]])
        session = runner.Session(run)
        self.addCleanup(session.close)
        session.api_replay(session.client, self.case, session.variables)
        self.server.priority = "high"
        last = session.api_replay(session.client, self.case, session.variables, kind="retry")
        runner.record(run, self.case, session.summarize(self.case, last), False, db.now())
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        references = json.loads(result["detail"])["attempts"]
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        self.assertEqual(result["status"], "Fail")
        self.assertEqual({target["execution_attempt_id"] for target in manifest["diagnostic_targets"]}, {references[0]["id"]})
        self.assertNotEqual(references[0]["id"], references[1]["id"])

    def test_multiple_cases_keep_each_failed_attempt_in_one_manifest(self):
        second = builder.api_case("TC-API-002", "第二场景", "GET", "/second", {"status": 200, "json_equals": {"/priority": "high"}})
        projects.save_cases(self.project, [self.case, second])
        second = projects.case(self.project, second["id"])
        run = runner.create(self.project, [self.case["id"], second["id"]])
        run = runner.execute(run["id"])
        manifest = failure_context.build_manifest(self.project, [self.summary(run, [self.case, second])])
        self.assertEqual({target["case_id"] for target in manifest["diagnostic_targets"]}, {self.case["id"], second["id"]})
        payload = self.payload(manifest)
        self.assertEqual(len(failure_context.validate_diagnoses(self.project, manifest, payload)["diagnoses"]), 2)

    def test_collect_freezes_earlier_round_failure_even_after_final_round_passes(self):
        with patch.object(runner, "spawn", return_value=None), patch("autoskill.server.jobs.ensure_worker", return_value={}):
            identity, first_run = self.queue_workflow()
            runner.execute(first_run)
            self.assertIsNone(workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"])
            workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
            second = db.one("SELECT run_id FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' AND run_id != ?", (identity, first_run))
            self.server.priority = "high"
            self.assertEqual(runner.execute(second["run_id"])["status"], "Pass")
            diagnosis = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        context = diagnosis["context"]
        self.assertEqual(diagnosis["phase"], "diagnose")
        self.assertEqual(len(context["validation_results"]), 2)
        self.assertEqual({target["run_id"] for target in context["diagnostic_targets"]}, {first_run})
        self.assertEqual({target["type"] for target in context["diagnostic_targets"]}, {"assertion"})
        self.assertEqual(workflow.diagnostic_context(identity)["manifest_hash"], context["manifest_hash"])

    def test_collect_retains_prior_fail_when_its_receipt_disappears_between_rounds(self):
        with patch.object(runner, "spawn", return_value=None), patch("autoskill.server.jobs.ensure_worker", return_value={}):
            identity, first_run = self.queue_workflow()
            runner.execute(first_run)
            workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
            result = db.one("SELECT * FROM results WHERE run_id = ?", (first_run,))
            reference = json.loads(result["detail"])["attempts"][0]
            data_refs.resolve(reference["path"]).unlink()
            workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
            second = db.one("SELECT run_id FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' AND run_id != ?", (identity, first_run))
            self.server.priority = "high"
            runner.execute(second["run_id"])
            diagnosis = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        context = diagnosis["context"]
        self.assertEqual(context["validation_results"][0]["conclusion"], "violated")
        self.assertFalse(context["validation_results"][0]["evidence_valid"])
        self.assertEqual({target["type"] for target in context["diagnostic_targets"]}, {"evidence_gap"})
        self.assertEqual(context["diagnostic_targets"][0]["execution_attempt_id"], reference["id"])

    def test_frozen_input_tampering_is_primary_gap_even_when_receipt_exists(self):
        run, result = self.run_case()
        manifest_file = data_refs.resolve(run["input_manifest"])
        document = json.loads(manifest_file.read_bytes())
        document["cases"][self.case["id"]]["title"] = "changed"
        execution_inputs.write_json(manifest_file, document)
        self.assert_error("evidence_unavailable", self.context, run, result)
        manifest = failure_context.build_manifest(self.project, [{"run_id": run["id"], "results": [{"case_id": self.case["id"]}]}])
        self.assertEqual({target["type"] for target in manifest["diagnostic_targets"]}, {"evidence_gap"})

    def test_missing_run_keeps_frozen_attempt_identity_and_expected_receipt_hash(self):
        run, result = self.run_case()
        summary = self.summary(run)
        reference = json.loads(result["detail"])["attempts"][0]
        db.execute("DELETE FROM results WHERE run_id = ?", (run["id"],))
        db.execute("DELETE FROM runs WHERE id = ?", (run["id"],))
        manifest = failure_context.build_manifest(self.project, [summary])
        target = manifest["diagnostic_targets"][0]
        self.assertEqual((target["type"], target["execution_attempt_id"], target["expected_hash"]),
                         ("evidence_gap", reference["id"], reference["sha256"]))
        self.assertEqual(target["input_hash"], summary["input_hash"])

    def test_corrupt_unfinished_journal_is_enumerated_as_gap_without_inventing_case(self):
        run = runner.create(self.project, [self.case["id"]])
        session = runner.Session(run)
        self.addCleanup(session.close)

        def interrupt(_attempt):
            raise InterruptedWorker

        with self.assertRaises(InterruptedWorker):
            session.attempt(self.case, "case", interrupt)
        file = next((runner.evidence_dir(run["id"]) / "attempts").glob("*.json"))
        file.write_bytes(b"{incomplete disk write")
        manifest = failure_context.build_manifest(self.project, [{"run_id": run["id"], "results": [{"case_id": self.case["id"]}]}])
        journal_gap = next(target for target in manifest["diagnostic_targets"] if target["execution_attempt_id"] == file.stem)
        self.assertEqual(journal_gap["type"], "evidence_gap")
        self.assertIsNone(journal_gap["case_id"])
        self.assertIsNone(journal_gap["expected_hash"])
        self.assertNotIn("pointer", journal_gap)

    def test_aggregation_stops_for_gaps_and_preserves_all_per_target_decisions(self):
        self.server.ok = False
        run, _result = self.run_case()
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        payload = self.payload(manifest)
        payload["diagnoses"][0]["disposition"] = "repair"
        payload["diagnoses"][1]["disposition"] = "retry"
        self.assertEqual(failure_context.validate_diagnoses(self.project, manifest, payload)["disposition"], "repair")
        payload["diagnoses"][1]["disposition"] = "business_defect"
        self.assertEqual(failure_context.validate_diagnoses(self.project, manifest, payload)["disposition"], "business_defect")
        payload["diagnoses"][0]["disposition"] = "incomplete"
        accepted = failure_context.validate_diagnoses(self.project, manifest, payload)
        self.assertEqual(accepted["disposition"], "incomplete")
        self.assertEqual({row["disposition"] for row in accepted["diagnoses"]}, {"incomplete", "business_defect"})

    def test_legacy_clients_only_expand_explicit_incomplete_without_rebuilding_old_items(self):
        run, _result = self.run_case()
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        accepted = failure_context.validate_diagnoses(self.project, manifest, {"disposition": "incomplete", "reason": "证据不足"})
        self.assertEqual(len(accepted["diagnoses"]), len(manifest["diagnostic_targets"]))
        self.assert_error("validation_error", failure_context.validate_diagnoses, self.project, manifest, {"disposition": "retry", "reason": "旧客户端"})
        old_context = {"execution": {"run_id": run["id"]}}
        with patch.object(failure_context, "build_manifest", side_effect=AssertionError("legacy reads must stay frozen")):
            self.assertTrue(failure_context.workflow_context(self.project, old_context)["legacy"])
            old = failure_context.validate_diagnoses(self.project, old_context, {"disposition": "incomplete", "reason": "旧任务"})
        self.assertEqual((old["manifest_hash"], old["diagnoses"]), (None, []))

    def test_manifest_revision_and_scope_tampering_are_rejected(self):
        run, _result = self.run_case()
        manifest = failure_context.build_manifest(self.project, [self.summary(run)])
        payload = self.payload(manifest)
        self.assert_error("revision_conflict", failure_context.validate_diagnoses, self.project, manifest, {**payload, "manifest_hash": "other"})
        damaged = copy.deepcopy(manifest)
        damaged["diagnostic_targets"][0]["pointer"] = "/outcome"
        self.assert_error("revision_conflict", failure_context.validate_diagnoses, self.project, damaged, payload)

    def test_interruption_uses_original_operation_intent_without_execution_attempt(self):
        identity = workflow.create(self.project, {"title": "未知影响", "sources": [{"id": "s1", "text": "业务约束"}]}, idempotency_key=uuid.uuid4().hex)["workflow_id"]
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        binding = {"workflow_id": identity, "item_id": task["id"], "attempt_id": task["attempt_id"], "lease_token": task["lease_token"]}
        intent = workflow.operation_intent("click", {"target": "submit"}, binding=binding, idempotency_key="external-once")
        workflow.operation_receipt(intent, {"error": "返回结果未知"}, status="unknown")
        workflow.release(identity, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"], idempotency_key=uuid.uuid4().hex)
        investigation = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        self.assertEqual(investigation["phase"], "investigate")
        manifest = investigation["context"]
        operation_id = intent["operation_id"]
        target = manifest["diagnostic_targets"][0]
        self.assertEqual((target["type"], target["item_id"], target["operation_id"]), ("interruption", task["id"], operation_id))
        self.assertNotIn("execution_attempt_id", target)
        self.assertNotIn("pointer", target)
        self.assertEqual(self.payload(manifest, "incomplete")["diagnoses"][0]["evidence_refs"][0]["kind"], "operation")
        self.assertEqual(workflow.diagnostic_context(identity)["manifest_hash"], manifest["manifest_hash"])
        self.assert_error("evidence_unavailable", failure_context.validate_diagnoses, self.project, manifest, self.payload(manifest, "retry"))
        db.execute("UPDATE workflow_operations SET payload = ? WHERE id = ?", (state.packed({"target": "changed"}), operation_id))
        evidence = failure_context.workflow_context(self.project, manifest)["target_contexts"][0]
        self.assertFalse(evidence["evidence_available"])
        self.assertEqual(evidence["error"], "evidence_unavailable")
        db.execute("UPDATE workflow_operations SET kind = 'execute' WHERE id = ?", (operation_id,))
        evidence = failure_context.workflow_context(self.project, manifest)["target_contexts"][0]
        self.assertFalse(evidence["evidence_available"])
        self.assertEqual(evidence["error"], "evidence_unavailable")


if __name__ == "__main__":
    unittest.main()
