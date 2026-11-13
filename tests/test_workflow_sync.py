"""Background reconciliation keeps pure polling current without claiming host work."""
import hashlib
import json
import time
import unittest
from unittest.mock import Mock, patch

import test_workflow as fixture

from autoskill import builder, db, domain, execution_inputs, projects, runner, task_state, workflow
from autoskill.server import jobs


class WorkflowSyncTests(unittest.TestCase):
    setUpClass = classmethod(fixture.WorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.WorkflowTests.tearDownClass.__func__)
    setUp = fixture.WorkflowTests.setUp
    key = fixture.WorkflowTests.key
    create = fixture.WorkflowTests.create
    claim = fixture.WorkflowTests.claim
    submit = fixture.WorkflowTests.submit
    to_materialize = fixture.WorkflowTests.to_materialize
    queue_candidate = fixture.WorkflowTests.queue_candidate

    def wait_for(self, predicate, *, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if value := predicate():
                return value
            time.sleep(0.01)
        raise AssertionError("The committed workflow transition was not observable within one second")

    def start_worker(self):
        worker = jobs.Worker()
        worker.start()
        self.addCleanup(worker.thread.join, 5)
        self.addCleanup(worker.close)
        return worker

    def test_pure_status_polling_sees_background_terminal_collection_within_one_second(self):
        identity, case, run_id = self.queue_candidate("medium")
        calls = fixture.AnswerHandler.calls
        self.assertEqual(runner.execute(run_id)["status"], "Pass")
        before = workflow.status(identity)
        self.assertEqual(before["cycle_state"], "waiting_execution")
        self.start_worker()
        current = self.wait_for(lambda: (value if (value := workflow.status(identity))["conclusion"] == "satisfied" else None))
        self.assertEqual(current["cycle_state"], "awaiting_host")
        self.assertFalse(current["complete"])
        self.assertEqual(current["counts"].get("leased", 0), 0)
        self.assertEqual(projects.case(self.project, case["id"]), case)
        self.assertEqual(fixture.AnswerHandler.calls - calls, 1)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs WHERE session_id = ?", (identity,))["n"], 1)

    def test_status_reads_do_not_collect_and_reconciliation_is_idempotent(self):
        identity, _case, run_id = self.queue_candidate("medium")
        runner.execute(run_id)
        first = workflow.status(identity)
        self.assertEqual(first, workflow.status(identity))
        self.assertTrue(workflow.needs_reconcile())
        self.assertEqual(workflow.reconcile_pending()["advanced"], [identity])
        final = workflow.status(identity)
        for _ in range(3):
            self.assertEqual(workflow.reconcile_pending(), {"advanced": [], "errors": []})
            self.assertEqual(workflow.status(identity)["revision"], final["revision"])
        self.assertFalse(workflow.needs_monitoring())
        self.assertEqual(sum(item["phase"] == "knowledge" for item in final["items"]), 1)

    def test_terminal_status_does_not_generate_or_retry_a_report(self):
        from autoskill import task_reports

        identity = self.create()
        db.execute("UPDATE workflow_items SET state = 'completed' WHERE workflow_id = ?", (identity,))
        with patch.object(task_reports, "for_workflow", side_effect=AssertionError("A status read must not render reports")):
            first = workflow.status(identity)
            second = workflow.status(identity)
        self.assertTrue(first["complete"])
        self.assertEqual(first["test_report"]["status"], "not_generated")
        self.assertEqual(second, first)

    def test_default_cycle_is_current_and_all_history_requires_explicit_scope(self):
        identity = self.create()
        original = workflow.status(identity)
        workflow.revise(identity, {"sources": [{"id": "s1", "text": "新周期来源"}]},
                        expected_hash=original["definition_hash"], idempotency_key=self.key())
        current = workflow.status(identity)
        history = workflow.status(identity, cycle="all", limit=1)
        self.assertTrue(all(item["cycle_id"] == current["cycle_id"] for item in current["items"]))
        self.assertGreater(history["total"], current["total"])
        self.assertNotEqual(history["items"][0]["cycle_id"], current["cycle_id"])
        with self.assertRaises(workflow.WorkflowError):
            workflow.status(identity, cursor=history["next_cursor"])
        self.assertIsNotNone(workflow.status(identity, cursor=history["next_cursor"], cycle="all"))

    def test_live_execution_is_monitored_without_restarting_it(self):
        identity, _case, run_id = self.queue_candidate("medium")
        self.assertTrue(workflow.needs_monitoring())
        self.assertFalse(workflow.needs_reconcile())
        with patch.object(runner, "spawn") as spawn:
            self.assertEqual(workflow.reconcile_pending(), {"advanced": [], "errors": []})
            spawn.assert_not_called()
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"], "Queued")
        self.assertEqual(workflow.status(identity)["cycle_state"], "waiting_execution")

    def test_expired_safe_lease_is_fenced_without_claiming_a_replacement(self):
        identity = self.create()
        task = self.claim(identity)
        checkpoint = {"read_sources": True}
        workflow.heartbeat(identity, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                           idempotency_key=self.key(), checkpoint=checkpoint)
        db.execute("UPDATE workflow_attempts SET lease_until = '2000-01-01' WHERE id = ?", (task["attempt_id"],))
        self.start_worker()
        self.wait_for(lambda: db.one("SELECT state FROM workflow_attempts WHERE id = ?", (task["attempt_id"],))["state"] == "expired")
        self.assertEqual(db.one("SELECT state FROM workflow_items WHERE id = ?", (task["id"],))["state"], "ready")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM workflow_attempts WHERE item_id = ?", (task["id"],))["n"], 1)
        self.assertFalse(workflow.needs_monitoring())
        replacement = self.claim(identity)
        self.assertEqual(replacement["checkpoint"], checkpoint)
        self.assertNotEqual(replacement["attempt_id"], task["attempt_id"])

    def test_background_startup_recovers_committed_publication_without_resubmitting(self):
        identity = self.create()
        task = self.claim(identity)
        payload = {"requirements": [{"id": "r1", "text": "应为 high", "source_ids": ["s1"]}]}
        publish = domain.publish
        def interrupted(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError("crash after HEAD")
        with patch.object(domain, "publish", side_effect=interrupted), self.assertRaises(OSError):
            self.submit(identity, task, payload)
        self.assertTrue(workflow.needs_reconcile())
        self.start_worker()
        def design_ready():
            current = workflow.status(identity)
            return current if any(item["phase"] == "design" for item in current["items"]) else None
        current = self.wait_for(design_ready)
        self.assertEqual(sum(item["phase"] == "design" for item in current["items"]), 1)
        self.assertEqual(current["counts"].get("leased", 0), 0)
        self.assertEqual(workflow.reconcile_pending(), {"advanced": [], "errors": []})

    def test_expired_unknown_operation_creates_unclaimed_investigation_without_replay(self):
        identity = self.create()
        task = self.claim(identity)
        binding = {"workflow_id": identity, "item_id": task["id"], "attempt_id": task["attempt_id"], "lease_token": task["lease_token"]}
        intent = workflow.operation_intent("click", {"target": "submit"}, binding=binding, idempotency_key="only-click")
        workflow.operation_receipt(intent, {"error": "连接中断"}, status="unknown")
        self.assertTrue(workflow.needs_monitoring())
        self.assertFalse(workflow.needs_reconcile())
        db.execute("UPDATE workflow_attempts SET lease_until = '2000-01-01' WHERE id = ?", (task["attempt_id"],))
        self.start_worker()
        def investigation_ready():
            current = workflow.status(identity)
            return current if any(item["phase"] == "investigate" for item in current["items"]) else None
        current = self.wait_for(investigation_ready)
        investigations = [item for item in current["items"] if item["phase"] == "investigate"]
        self.assertEqual(len(investigations), 1)
        self.assertEqual(investigations[0]["state"], "ready")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM workflow_operations WHERE kind = 'click'")["n"], 1)
        self.assertEqual(db.one("SELECT state FROM workflow_operations WHERE id = ?", (intent["operation_id"],))["state"], "unknown")
        self.assertFalse(workflow.needs_monitoring())

    def test_cancelled_or_paused_workflow_does_not_publish_finished_candidate(self):
        for lifecycle in ("cancelled", "paused"):
            with self.subTest(lifecycle=lifecycle):
                identity, _case, run_id = self.queue_candidate("medium")
                runner.execute(run_id)
                if lifecycle == "cancelled":
                    workflow.cancel(identity, idempotency_key=self.key())
                else:
                    db.execute("UPDATE workflow_runtime SET lifecycle = 'paused' WHERE id = ?", (identity,))
                before = workflow.status(identity)
                self.assertEqual(workflow.reconcile_pending(), {"advanced": [], "errors": []})
                self.assertEqual(workflow.status(identity)["revision"], before["revision"])
                self.assertEqual(projects.cases(self.project), [])
                self.assertFalse(workflow.needs_reconcile())

    def test_queue_idle_does_not_ignore_uncollected_terminal_workflow(self):
        _identity, _case, run_id = self.queue_candidate("medium")
        runner.execute(run_id)
        self.assertTrue(jobs._pending())
        jobs.reconcile_workflows()
        self.assertFalse(jobs._pending())

    def test_cancelled_run_with_all_rows_does_not_publish_or_become_satisfied(self):
        identity, _case, run_id = self.queue_candidate("medium")
        runner.execute(run_id)
        db.execute("UPDATE runs SET status = 'Cancelled' WHERE id = ?", (run_id,))
        workflow.reconcile_pending()
        current = workflow.status(identity)
        self.assertNotEqual(current["conclusion"], "satisfied")
        self.assertEqual(current["cycle_state"], "recovering")
        self.assertEqual(projects.cases(self.project), [])
        self.assertEqual(db.one("SELECT status FROM results WHERE run_id = ?", (run_id,))["status"], "Pass")

    def test_project_report_is_rendered_in_background_and_does_not_keep_monitoring(self):
        from autoskill import data_refs, paths, task_reports

        identity = self.create()
        workflow.cancel(identity, idempotency_key=self.key())
        prepared = task_reports.prepare_workflow(identity)
        report_id = prepared["report_id"]
        folder = projects.path(self.project) / "reports" / report_id
        self.assertEqual(prepared["status"], "generating")
        self.assertEqual(task_reports._folder(report_id), folder)
        self.assertFalse((paths.local() / "task-reports" / report_id).exists())
        self.assertEqual(workflow._unprepared_reports(), [])
        self.assertEqual(workflow.pending_reports(), [report_id])
        self.assertTrue(workflow.needs_monitoring())
        frozen = (folder / "snapshot.json").read_bytes()
        revision = workflow.status(identity)["revision"]

        worker = self.start_worker()
        ready = self.wait_for(lambda: value if (value := task_reports.peek(report_id))["status"] == "ready" else None, timeout=5)
        self.wait_for(lambda: not jobs._pending(), timeout=5)
        worker.close()
        for artifact in ("excel", "markdown", "bugs"):
            self.assertEqual(data_refs.resolve(ready[artifact]).parent, folder)
            self.assertTrue(data_refs.resolve(ready[artifact]).is_file())
        self.assertTrue(all(ready["validation"][name + "_readable"] for name in ("workbook", "markdown", "bugs")))
        self.assertEqual((folder / "snapshot.json").read_bytes(), frozen)
        self.assertEqual(workflow.status(identity)["revision"], revision)
        self.assertEqual(workflow.pending_reports(), [])
        self.assertFalse(workflow.needs_monitoring())
        self.assertFalse(jobs._pending())
        with patch.object(task_reports, "prepare_workflow", side_effect=AssertionError("A ready report must not be prepared again")), \
                patch.object(jobs.subprocess, "Popen") as spawn:
            for _ in range(3):
                self.assertEqual(jobs.reconcile_workflows(), {"advanced": [], "errors": []})
            spawn.assert_not_called()

    def test_legacy_and_previous_report_generations_remain_in_the_background_queue(self):
        from autoskill import paths, task_reports

        identity = self.create()
        initial = workflow.status(identity)
        previous_id = task_reports.workflow_report_id(identity, initial["cycle_id"])
        legacy = paths.local() / "task-reports" / previous_id
        legacy.mkdir(parents=True)
        workflow.cancel(identity, idempotency_key=self.key())
        self.assertEqual(task_reports._folder(previous_id), legacy)
        previous_snapshot = (legacy / "snapshot.json").read_bytes()
        workflow.resume(identity, idempotency_key=self.key())
        cancelled = workflow.cancel(identity, idempotency_key=self.key())
        current_id = cancelled["test_report"]["report_id"]
        self.assertNotEqual(current_id, previous_id)
        current_folder = projects.path(self.project) / "reports" / current_id
        self.assertEqual(task_reports._folder(current_id), current_folder)
        shadow = paths.local() / "task-reports" / current_id / "receipt.json"
        execution_inputs.write_json(shadow, task_reports._saved(current_id))
        self.assertEqual(workflow.pending_reports(), sorted([previous_id, current_id]))
        self.assertEqual(workflow._unprepared_reports(), [])

        worker = self.start_worker()
        self.wait_for(lambda: all(task_reports.peek(report_id)["status"] == "ready" for report_id in (previous_id, current_id)), timeout=10)
        self.wait_for(lambda: not jobs._pending(), timeout=5)
        worker.close()
        self.assertEqual((legacy / "snapshot.json").read_bytes(), previous_snapshot)
        self.assertEqual(json.loads(shadow.read_bytes())["status"], "generating")
        self.assertEqual(workflow.pending_reports(), [])
        self.assertEqual(workflow._unprepared_reports(), [])
        self.assertFalse(workflow.needs_monitoring())
        self.assertFalse(jobs._pending())

    def test_pending_report_scan_ignores_invalid_location_indexes_and_receipts(self):
        from autoskill import data_refs, paths, task_reports

        identity = self.create()
        report_id = workflow.cancel(identity, idempotency_key=self.key())["test_report"]["report_id"]
        location_indexes = paths.local() / "task-report-locations"
        misplaced_id = "workflow_" + "f" * 64
        misplaced = projects.path(self.project) / "unbound-reports" / misplaced_id
        execution_inputs.write_json(misplaced / "receipt.json", {"status": "generating", "report_id": misplaced_id})
        execution_inputs.write_json(location_indexes / (misplaced_id + ".json"), {"project_id": self.project, "folder": data_refs.store(misplaced)})
        execution_inputs.write_json(location_indexes / ("workflow_" + "e" * 64 + ".json"), [])
        execution_inputs.write_json(location_indexes / "workflow_invalid.json", {"project_id": self.project, "folder": data_refs.store(misplaced)})
        execution_inputs.write_json(paths.local() / "task-reports" / ("workflow_" + "d" * 64) / "receipt.json", [])
        self.assertEqual(workflow.pending_reports(), [report_id])
        self.assertEqual(task_reports.peek(report_id)["status"], "generating")

    def test_failed_report_child_is_durable_failure_and_never_auto_respawned(self):
        from autoskill import paths, task_reports

        identity = self.create()
        cancelled = workflow.cancel(identity, idempotency_key=self.key())
        report_id = cancelled["test_report"]["report_id"]
        self.assertEqual(task_reports.peek(report_id)["status"], "generating")
        child = Mock()
        child.poll.return_value = 9
        jobs._report_children[str(paths.workspace())] = (child, report_id)
        with patch.object(jobs.subprocess, "Popen") as spawn:
            self.assertFalse(jobs._reports_running())
            failed_bytes = (task_reports._folder(report_id) / "receipt.json").read_bytes()
            for _ in range(3):
                jobs.reconcile_workflows()
            spawn.assert_not_called()
        self.assertEqual(task_reports.peek(report_id)["status"], "failed")
        self.assertEqual((task_reports._folder(report_id) / "receipt.json").read_bytes(), failed_bytes)
        self.assertEqual(workflow.pending_reports(), [])
        self.assertFalse(workflow.needs_monitoring())
        self.assertEqual(task_reports.retry(report_id)["status"], "ready")
        self.assertFalse(workflow.needs_monitoring())

    def test_closing_worker_that_was_never_started_is_safe(self):
        jobs.Worker().close()

    def test_cancel_resume_then_real_execution_gets_new_report_without_rewriting_cancelled_delivery(self):
        from autoskill import data_refs, task_reports

        identity = self.create(sources=[{"id": "s1", "text": "接口返回 medium；另用 high 独立预期验证真实失败，不得删除失败。"}])
        cancelled = workflow.cancel(identity, idempotency_key=self.key())
        first_id = cancelled["test_report"]["report_id"]
        first = task_reports.finish_workflow_report(first_id)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["cases"], 0)
        first_folder = data_refs.resolve(first["excel"]).parent
        original = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in first_folder.iterdir() if file.is_file()}
        resume_key = self.key()
        resumed = workflow.resume(identity, idempotency_key=resume_key)
        self.assertEqual(resumed["workflow"]["cycle_id"], cancelled["workflow"]["cycle_id"])
        self.assertEqual(resumed["workflow"]["report_generation"], 1)
        self.assertEqual(workflow.resume(identity, idempotency_key=resume_key), resumed)
        workflow.resume(identity, idempotency_key=self.key())
        self.assertEqual(workflow.status(identity)["report_generation"], 1)
        self.submit(identity, self.claim(identity), {"requirements": [
            {"id": "r1", "text": "接口应为 medium", "source_ids": ["s1"]},
            {"id": "r2", "text": "独立 high 预期应保留真实失败", "source_ids": ["s1"]}]})
        self.submit(identity, self.claim(identity), {"scenarios": [
            {"id": "sc1", "title": "验证 medium", "requirement_ids": ["r1"], "oracle_ids": ["o1"]},
            {"id": "sc2", "title": "保留 high 失败", "requirement_ids": ["r2"], "oracle_ids": ["o2"]}],
            "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"], "checks": {"status": 200, "json_equals": {"/priority": "medium"}}},
                        {"id": "o2", "kind": "api", "requirement_ids": ["r2"], "checks": {"status": 200, "json_equals": {"/priority": "high"}}}]})
        first_case = builder.api_case("TC-API-RESUME-1", "Medium passes", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "medium"}})
        second_case = builder.api_case("TC-API-RESUME-2", "High fails", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "high"}})
        first_case["requirement_ids"] = ["r1"]
        second_case["requirement_ids"] = ["r2"]
        self.submit(identity, self.claim(identity), {"cases": [first_case, second_case],
                                                  "bindings": {"sc1": [first_case["id"]], "sc2": [second_case["id"]]}})
        self.assertIsNone(self.claim(identity))
        execution = next(item for item in workflow.status(identity)["items"] if item["phase"] == "execute")
        actual = runner.execute(execution["run_id"])
        self.assertEqual((actual["status"], actual["passed"], actual["failed"]), ("Fail", 1, 1))
        workflow.reconcile_pending()
        self.submit(identity, self.claim(identity), {"disposition": "business_defect", "reason": "high 与实际 medium 不同", "evidence_refs": [execution["run_id"]]})
        self.submit(identity, self.claim(identity), {"entries": [{"id": "resume-k", "text": "保留失败结果", "evidence_refs": [execution["run_id"]]}]})
        final = workflow.status(identity)
        self.assertTrue(final["complete"])
        self.assertEqual(final["conclusion"], "violated")
        self.assertEqual(final["report_generation"], 1)
        final_id = final["test_report"]["report_id"]
        self.assertNotEqual(final_id, first_id)
        final_report = task_reports.finish_workflow_report(final_id)
        self.assertEqual((final_report["status"], final_report["cases"], final_report["execution_rows"]), ("ready", 2, 2))
        final_folder = data_refs.resolve(final_report["excel"]).parent
        frozen = json.loads((final_folder / "snapshot.json").read_bytes())
        self.assertEqual(frozen["report_generation"], 1)
        self.assertEqual([row["result"]["status"] for row in frozen["cases"]], ["Pass", "Fail"])
        self.assertTrue(all(row["report_generation"] == 1 and not row["historical_report_generation"] for row in frozen["cases"]))
        for result in db.rows("SELECT * FROM results WHERE run_id = ?", (execution["run_id"],)):
            self.assertTrue(execution_inputs.result_current(self.project, projects.case(self.project, result["case_id"]), result))
        self.assertEqual(task_reports.workflow_report_id(identity, final["cycle_id"], generation=0), first_id)
        self.assertEqual(task_reports.workflow_report_id(identity, final["cycle_id"]), final_id)
        self.assertEqual({file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in first_folder.iterdir() if file.is_file()}, original)
        saved = task_reports._saved(first_id)
        old_snapshot = task_reports._snapshot(task_reports._base("workflow", identity, first_id), saved)
        self.assertEqual((old_snapshot["report_generation"], len(old_snapshot["cases"])), (0, 0))
        with self.assertRaises(ValueError):
            task_reports._snapshot(task_reports._base("workflow", identity, final_id), saved)

    def test_resume_freezes_cancellation_boundary_even_while_old_run_is_queued(self):
        from autoskill import task_reports

        identity, _case, run_id = self.queue_candidate("medium")
        cancelled = workflow.cancel(identity, idempotency_key=self.key())
        self.assertEqual(cancelled["test_report"]["status"], "not_generated")
        original_id = cancelled["test_report"]["report_id"]
        resumed = workflow.resume(identity, idempotency_key=self.key())
        self.assertEqual(resumed["workflow"]["report_generation"], 1)
        original_snapshot = json.loads((task_reports._folder(original_id) / "snapshot.json").read_bytes())
        self.assertTrue(original_snapshot["metadata"]["cancelled_boundary"])
        self.assertEqual(original_snapshot["run_ids"], [run_id])
        self.assertIsNone(original_snapshot["cases"][0]["result"])
        self.assertFalse(original_snapshot["cases"][0]["evidence_valid"])
        self.assertNotEqual(task_reports.workflow_report_id(identity, original_snapshot["cycle_id"]), original_id)

    def test_explicit_workflow_requirement_is_published_with_its_case_and_frozen_source(self):
        identity = self.create()
        task = self.to_materialize(identity, "medium")
        case = builder.api_case("TC-API-EXPLICIT", "Explicit requirement", "GET", "/priority",
                                {"status": 200, "json_equals": {"/priority": "medium"}})
        case["requirement_ids"] = ["r1"]
        self.submit(identity, task, {"cases": [case], "bindings": {"sc1": [case["id"]]}})
        self.assertIsNone(self.claim(identity))
        execution = next(item for item in workflow.status(identity)["items"] if item["phase"] == "execute")
        source_file = projects.path(self.project) / "analysis/requirements.json"
        frozen_source = source_file.read_bytes()
        source_mtime = source_file.stat().st_mtime_ns
        manifest = execution_inputs.load(db.one("SELECT * FROM runs WHERE id = ?", (execution["run_id"],)))
        self.assertEqual(manifest["sources"]["files"]["analysis/requirements.json"]["sha256"], hashlib.sha256(frozen_source).hexdigest())
        self.assertEqual(projects.cases(self.project), [])
        self.assertEqual(runner.execute(execution["run_id"])["status"], "Pass")
        self.assertEqual(workflow.reconcile_pending()["errors"], [])
        final = workflow.status(identity)
        self.assertEqual(final["conclusion"], "satisfied")
        self.assertEqual(projects.case(self.project, case["id"])["requirement_ids"], ["r1"])
        requirements = json.loads((projects.path(self.project) / "analysis/requirements.json").read_bytes())
        record = next(record for record in requirements if record["id"] == "r1")
        self.assertEqual(record["text"], "优先级为 medium")
        self.assertEqual(record["source_ids"], ["s1"])
        self.assertEqual(record["workflow_ref"]["workflow_id"], identity)
        self.assertEqual(record["workflow_ref"]["definition_hash"], final["definition_hash"])
        self.assertEqual(workflow.read(identity, kind="oracles")["items"][0]["checks"]["json_equals"]["/priority"], "medium")
        self.assertEqual(source_file.read_bytes(), frozen_source)
        self.assertEqual(source_file.stat().st_mtime_ns, source_mtime)
        result = db.one("SELECT * FROM results WHERE run_id = ?", (execution["run_id"],))
        self.assertTrue(execution_inputs.result_current(self.project, projects.case(self.project, case["id"]), result))
        self.assertTrue(task_state.current_result(self.project, projects.case(self.project, case["id"]), result)["current"])
        self.submit(identity, self.claim(identity), {"entries": [{"id": "proof-current", "text": "当前执行与冻结来源一致"}]})
        self.assertTrue(execution_inputs.result_current(self.project, projects.case(self.project, case["id"]), result))
        projects.sync_requirement_records(self.project, [record])
        self.assertEqual(source_file.read_bytes(), frozen_source)
        projects.sync_requirement_records(self.project, [{**record, "title": "有效来源已被实际修改"}])
        self.assertFalse(execution_inputs.result_current(self.project, projects.case(self.project, case["id"]), result))

    def test_unknown_requirement_is_rejected_before_execution(self):
        identity = self.create()
        task = self.to_materialize(identity, "medium")
        case = builder.api_case("TC-API-UNKNOWN", "Unknown requirement", "GET", "/priority",
                                {"status": 200, "json_equals": {"/priority": "medium"}})
        case["requirement_ids"] = ["misspelled-requirement"]
        with self.assertRaises(workflow.WorkflowError):
            self.submit(identity, task, {"cases": [case], "bindings": {"sc1": [case["id"]]}})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs WHERE session_id = ?", (identity,))["n"], 0)

    def test_publication_never_restores_a_requirement_source_changed_after_execution(self):
        identity = self.create()
        task = self.to_materialize(identity, "medium")
        case = builder.api_case("TC-API-SOURCE", "Source stays changed", "GET", "/priority",
                                {"status": 200, "json_equals": {"/priority": "medium"}})
        case["requirement_ids"] = ["r1"]
        self.submit(identity, task, {"cases": [case], "bindings": {"sc1": [case["id"]]}})
        self.assertIsNone(self.claim(identity))
        execution = next(item for item in workflow.status(identity)["items"] if item["phase"] == "execute")
        self.assertEqual(runner.execute(execution["run_id"])["status"], "Pass")
        source = projects.path(self.project) / "analysis/requirements.json"
        record = json.loads(source.read_bytes())[0]
        projects.sync_requirement_records(self.project, [{**record, "text": "执行后来源已变化", "title": "来源已变化"}])
        changed = source.read_bytes()
        self.assertEqual(workflow.reconcile_pending()["errors"], [])
        self.assertEqual(source.read_bytes(), changed)
        self.assertEqual(projects.cases(self.project), [])
        diagnosis = self.claim(identity)
        self.assertEqual((diagnosis["phase"], diagnosis["context"]["publication"]), ("diagnose", "conflict"))
        result = db.one("SELECT * FROM results WHERE run_id = ?", (execution["run_id"],))
        self.assertTrue(execution_inputs.result_evidence(db.one("SELECT * FROM runs WHERE id = ?", (execution["run_id"],)), result, case))

    def test_repetition_does_not_overwrite_a_source_change_to_match_earlier_validation(self):
        identity, _case, run_id = self.queue_candidate("medium", repetitions=2)
        runner.execute(run_id)
        source = projects.path(self.project) / "analysis/requirements.json"
        record = json.loads(source.read_bytes())[0]
        projects.sync_requirement_records(self.project, [{**record, "title": "两次执行之间来源改变"}])
        changed = source.read_bytes()
        self.assertEqual(workflow.reconcile_pending()["errors"], [])
        following = workflow.reconcile_pending()
        self.assertTrue(following["errors"])
        self.assertEqual(source.read_bytes(), changed)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs WHERE session_id = ?", (identity,))["n"], 1)
        self.assertEqual(projects.cases(self.project), [])


if __name__ == "__main__":
    unittest.main()
