"""State projections, lifecycle filters and read-only polling share one scope."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from fastapi.testclient import TestClient

from autoskill import builder, data_refs, db, load_testing, projects, report_locations, runner, task_state, workflow_state
from autoskill.server import metrics
from autoskill.server.app import create_app


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        time.sleep(0.02)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StateApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.pid = projects.create("状态测试", f"http://127.0.0.1:{self.site.server_port}", project_id="state-test")["id"]
        self.client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        self.addCleanup(self.client.close)

    def queued(self, identity):
        db.insert("runs", {"id": identity, "project_id": self.pid, "engine": "playwright", "status": "Queued", "total": 1, "created_at": db.now()})

    def measured(self):
        cases = [builder.api_case("TC-API-001", "业务通过", "GET", "/", {"status": 200, "json_equals": {"/ok": True}, "max_ms": 10000}),
                 builder.api_case("TC-API-002", "业务失败", "GET", "/", {"status": 200, "json_equals": {"/ok": False}, "max_ms": 10000}),
                 builder.api_case("TC-API-003", "性能失败", "GET", "/", {"status": 200, "json_equals": {"/ok": True}, "max_ms": 1})]
        projects.save_cases(self.pid, cases)
        run = runner.create(self.pid)
        return runner.execute(run["id"])

    def test_workspace_revision_tracks_commits_not_noop_or_rollback(self):
        self.queued("rev")
        first = self.client.get("/api/state-revision").json()["revision"]
        self.assertEqual(first, self.client.get("/api/state-revision").json()["revision"])
        db.update("runs", "rev", {"status": "Queued"})
        self.assertEqual(first, self.client.get("/api/state-revision").json()["revision"])
        with self.assertRaises(RuntimeError), db.connect() as connection:
            connection.execute("UPDATE runs SET status='Running' WHERE id='rev'")
            raise RuntimeError("rollback")
        self.assertEqual(first, self.client.get("/api/state-revision").json()["revision"])
        db.update("runs", "rev", {"status": "Running"})
        self.assertNotEqual(first, self.client.get("/api/state-revision").json()["revision"])

    def test_revision_trigger_is_refreshed_when_a_persistent_column_is_added(self):
        self.queued("evolving")
        with db.connect() as connection:
            connection.execute("ALTER TABLE runs ADD COLUMN future_value TEXT NOT NULL DEFAULT ''")
        db.init()
        revision = db.one("SELECT revision FROM state_revision WHERE id=1")["revision"]
        db.update("runs", "evolving", {"future_value": ""})
        self.assertEqual(db.one("SELECT revision FROM state_revision WHERE id=1")["revision"], revision)
        db.update("runs", "evolving", {"future_value": "changed"})
        self.assertGreater(db.one("SELECT revision FROM state_revision WHERE id=1")["revision"], revision)

    def test_current_cycle_is_shared_by_lists_and_counts_and_history_is_explicit(self):
        workflow_state.init()
        for identity in ("old", "current", "plain"):
            self.queued(identity)
        db.insert("workflow_runtime", {"id": "flow", "project_id": self.pid, "title": "周期", "cycle_id": "new", "created_at": db.now(), "updated_at": db.now()})
        for sequence, (identity, cycle) in enumerate((("old", "old"), ("current", "new")), 1):
            db.insert("workflow_items", {"id": "item-" + identity, "workflow_id": "flow", "sequence": sequence, "cycle_id": cycle,
                                         "phase": "execute", "state": "waiting_execution", "run_id": identity, "created_at": db.now()})
        current = self.client.get("/api/runs").json()
        self.assertEqual({row["id"] for row in current["items"]}, {"current", "plain"})
        self.assertEqual(self.client.get("/api/summary").json()["runs"]["total"], current["total"])
        all_runs = self.client.get("/api/runs?cycle=all").json()
        self.assertEqual(all_runs["total"], 3)
        self.assertEqual(self.client.get("/api/summary?cycle=all").json()["runs"]["total"], 3)
        self.assertEqual(self.client.get("/api/runs?cycle=invalid").status_code, 400)
        for identity in ("current", "old"):
            db.insert("results", {"run_id": identity, "project_id": self.pid, "case_id": "TC-API-001", "case_title": "周期结果",
                                  "kind": "api", "engine": "http", "status": "Pass", "started_at": db.now()})
        latest = metrics.results(metrics.Scope(), current_only=True)
        self.assertEqual((latest["total"], latest["items"][0]["run_id"]), (1, "current"))

    def test_result_axes_filter_before_pagination_and_summary_uses_same_selection(self):
        run = self.measured()
        first = self.client.get("/api/results?business=pass&limit=1").json()
        second = self.client.get("/api/results?business=pass&limit=1&offset=1").json()
        self.assertEqual((first["total"], second["total"]), (2, 2))
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertTrue(all(row["state"]["checks"]["business"] == "pass" for row in first["items"] + second["items"]))
        counts = self.client.get("/api/summary?business=pass").json()["results"]
        self.assertEqual((counts["executions"], counts["passed"], counts["failed"]), (2, 1, 1))
        detail = self.client.get("/api/runs/" + run["id"]).json()
        self.assertEqual(detail["state"]["lifecycle"], "completed")
        slow = next(row for row in detail["results"] if row["case_id"] == "TC-API-003")
        self.assertEqual((slow["state"]["checks"]["business"], slow["state"]["checks"]["performance"]), ("pass", "fail"))
        self.assertEqual(slow["state"]["outcome"], "fail")

    def test_history_page_counts_offscreen_outcomes_without_opening_their_reports(self):
        self.measured()
        self.queued("active-extra")
        db.update("runs", "active-extra", {"status": "Running"})
        for entity, total, failed in (("results", 3, 2), ("runs", 2, 1)):
            with self.subTest(entity=entity), patch.object(task_state, "report", wraps=task_state.report) as reports:
                page = self.client.get(f"/api/{entity}?limit=1&offset=1").json()
                self.assertEqual((page["total"], len(page["items"]), page["state_counts"]["fail"]), (total, 1, failed))
                self.assertEqual(sum(page["state_counts"].values()), total)
                self.assertEqual(reports.call_count, 1)
        running = self.client.get("/api/runs?status=Running").json()
        self.assertEqual([row["id"] for row in running["items"]], ["active-extra"])

    def test_history_verdict_filters_use_the_run_or_the_individual_result_before_pagination(self):
        mixed = self.measured()["id"]
        passed = runner.execute(runner.create(self.pid, ["TC-API-001"])["id"])["id"]
        for identity, status in (("waiting", "Running"), ("stopped", "Cancelled"), ("blocked", "Blocked")):
            self.queued(identity)
            db.update("runs", identity, {"status": status})
        runs = self.client.get("/api/runs?status=NotPassed").json()
        self.assertEqual({row["id"] for row in runs["items"]}, {mixed})
        self.assertEqual({row["history_status"] for row in runs["items"]}, {"Fail"})
        self.assertEqual({row["state"]["outcome"] for row in runs["items"]}, {"fail"})
        self.assertEqual([row["id"] for row in self.client.get("/api/runs?status=Pass").json()["items"]], [passed])
        self.assertEqual(self.client.get("/api/runs?status=Running").json()["items"][0]["history_status"], "NotRun")
        idle = self.client.get("/api/runs?status=NotRun").json()["items"]
        self.assertEqual({row["id"] for row in idle}, {"waiting", "stopped", "blocked"})
        results = self.client.get("/api/results?status=Pass").json()["items"]
        self.assertEqual({row["run_id"] for row in results}, {mixed, passed})
        self.assertTrue(all(row["history_status"] == "Pass" for row in results))
        page = self.client.get("/api/results?status=NotPassed&limit=1&offset=1").json()
        self.assertEqual((page["total"], len(page["items"]), page["items"][0]["run_id"]), (2, 1, mixed))

    def test_get_run_never_generates_missing_report_or_retries_failed_report(self):
        run = self.measured()
        receipt = report_locations.folder("run_" + run["id"]) / "receipt.json"
        saved = json.loads(receipt.read_text())
        receipt.unlink()
        with patch("autoskill.task_reports.for_run", side_effect=AssertionError("GET attempted export")):
            response = self.client.get("/api/runs/" + run["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["test_report"]["status"], "not_generated")
        self.assertEqual(response.json()["state"]["report"], "not_generated")
        self.assertFalse(receipt.exists())
        failed = {key: saved[key] for key in ("report_id", "kind", "task_id", "generation", "source_hash")}
        failed.update(status="failed", error="导出失败", retryable=True)
        receipt.write_text(json.dumps(failed))
        with patch("autoskill.task_reports.for_run", side_effect=AssertionError("GET attempted retry")):
            response = self.client.get("/api/runs/" + run["id"])
        self.assertEqual(response.json()["test_report"]["status"], "failed")
        counts = db.one("SELECT (SELECT COUNT(*) FROM runs) runs,(SELECT COUNT(*) FROM results) results")
        retried = self.client.post("/api/task-reports/run_" + run["id"] + "/retry")
        self.assertEqual((retried.status_code, retried.json()["status"]), (200, "ready"))
        self.assertEqual(db.one("SELECT (SELECT COUNT(*) FROM runs) runs,(SELECT COUNT(*) FROM results) results"), counts)

    def test_phase_filter_includes_empty_active_runs_without_inventing_business_success(self):
        self.queued("queued")
        self.queued("active")
        db.update("runs", "active", {"status": "Running"})
        page = self.client.get("/api/runs?phase=running&limit=1").json()
        self.assertEqual((page["total"], page["items"][0]["id"]), (1, "active"))
        self.assertEqual(page["items"][0]["state"]["checks"]["business"], "unverified")
        self.assertEqual(page["items"][0]["state"]["report"], "pending")
        self.assertEqual(metrics.run_counts(metrics.Scope.parse(None, None, None, phase="running"))["total"], 1)

    def test_raw_pass_without_business_evidence_never_enters_pass_metrics(self):
        self.queued("unverified")
        db.update("runs", "unverified", {"status": "Pass", "done": 1, "passed": 1})
        db.insert("results", {"run_id": "unverified", "project_id": self.pid, "case_id": "TC-API-001", "case_title": "未验证",
                              "kind": "api", "engine": "http", "status": "Pass", "started_at": db.now(), "detail": {"business_status": "Unverified"}})
        summary = self.client.get("/api/summary").json()
        self.assertEqual((summary["results"]["passed"], summary["results"]["failed"]), (0, 1))
        self.assertEqual(summary["results"]["pass_rate"], 0)
        self.assertEqual(self.client.get("/api/results?status=Pass").json()["total"], 0)
        self.assertEqual(self.client.get("/api/results?status=Fail").json()["total"], 1)
        self.assertEqual(self.client.get("/api/results?status=NotPassed").json()["items"][0]["state"]["outcome"], "fail")
        row = self.client.get("/api/runs").json()["items"][0]
        self.assertEqual((row["passed"], row["failed"], row["recorded_counts"]["passed"]), (0, 1, 1))

    def test_changed_definition_invalidates_current_case_without_rewriting_run_history(self):
        run = self.measured()
        before = next(row for row in metrics.cases(metrics.Scope())["items"] if row["case_id"] == "TC-API-001")
        self.assertEqual(before["state"]["outcome"], "pass")
        self.assertEqual((before["current"], before["current_status"], before["last_status"]), (True, "Pass", "Pass"))
        case = projects.case(self.pid, "TC-API-001")
        case["assertions"]["json_equals"] = {"/ok": False}
        projects.save_cases(self.pid, [case])
        after = next(row for row in metrics.cases(metrics.Scope())["items"] if row["case_id"] == "TC-API-001")
        self.assertEqual(after["state"]["outcome"], "not_run")
        self.assertFalse(after["state"]["current"])
        self.assertEqual(after["state"]["historical_outcome"], "pass")
        self.assertEqual((after["current"], after["current_status"], after["last_status"]), (False, None, "Pass"))
        drawer = self.client.get(f"/api/projects/{self.pid}/cases/TC-API-001").json()
        self.assertEqual(drawer["state"], after["state"])
        self.assertEqual((drawer["current"], drawer["current_status"], drawer["last_status"]), (False, None, "Pass"))
        history = self.client.get("/api/runs/" + run["id"]).json()
        result = next(row for row in history["results"] if row["case_id"] == "TC-API-001")
        self.assertEqual(result["state"]["outcome"], "pass")

    def test_load_runs_preserve_verified_business_and_performance_and_stay_out_of_functional_counts(self):
        cases = [builder.api_case("TC-LOAD-001", "负载业务通过", "GET", "/", {"status": 200, "json_equals": {"/ok": True}, "max_ms": 10000}),
                 builder.api_case("TC-LOAD-002", "负载性能失败", "GET", "/", {"status": 200, "json_equals": {"/ok": True}, "max_ms": 1})]
        projects.save_cases(self.pid, cases)
        for case, outcome, performance in zip(cases, ("pass", "fail"), ("pass", "fail")):
            with self.subTest(case=case["id"]):
                run = load_testing.enqueue(self.pid, [case["id"]], concurrency=1, iterations=2, headed=False)
                runner.execute(run["id"])
                detail = self.client.get("/api/runs/" + run["id"]).json()
                self.assertEqual(detail["state"]["outcome"], outcome)
                self.assertEqual(detail["state"]["checks"], {"business": "pass", "performance": performance, "quality": "not_applicable"})
                self.assertEqual(detail["state"]["evidence"], "valid")
                self.assertEqual(len(detail["results"]), 2)
                self.assertTrue(all(result["state"]["evidence"] == "valid" for result in detail["results"]))
                native_detail = self.client.get("/api/load-tests/" + run["id"]).json()
                self.assertEqual(native_detail["state_counts"], detail["state_counts"])
                self.assertEqual((native_detail["passed"], native_detail["failed"]), (2 if outcome == "pass" else 0, 2 if outcome == "fail" else 0))
                self.assertEqual(native_detail["recorded_counts"], detail["recorded_counts"])
        packet = self.client.get("/api/dashboard").json()
        self.assertEqual((packet["summary"]["runs"]["total"], packet["summary"]["results"]["executions"], packet["recent"]["total"], packet["failures"]["total"]), (0, 0, 0, 0))
        self.assertEqual(self.client.get("/api/runs").json()["total"], 2)
        self.assertEqual(self.client.get("/api/runs?exclude_load=true").json()["total"], 0)
        self.assertEqual(self.client.get("/api/results?current=true").json()["total"], 0)
        self.assertTrue(all(not item["current"] and item["last_status"] is None for item in self.client.get("/api/cases").json()["items"]))

    def test_load_counts_invalidate_damaged_evidence_without_losing_recorded_pass(self):
        case = builder.api_case("TC-LOAD-001", "负载证据", "GET", "/", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [case])
        run = load_testing.enqueue(self.pid, [case["id"]], concurrency=1, iterations=1, headed=False)
        runner.execute(run["id"])
        first = self.client.get("/api/load-tests/" + run["id"]).json()
        self.assertEqual((first["passed"], first["recorded_counts"]["passed"]), (1, 1))
        row = db.one("SELECT * FROM results WHERE run_id=? AND prerequisite=0", (run["id"],))
        receipt = data_refs.resolve(json.loads(row["detail"])["attempts"][0]["path"])
        receipt.write_bytes(b"damaged execution evidence")
        before = db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        for projected in (self.client.get("/api/load-tests/" + run["id"]).json(), self.client.get("/api/load-tests").json()["items"][0]):
            self.assertEqual((projected["passed"], projected["failed"], projected["recorded_counts"]["passed"]), (0, 1, 1))
            self.assertEqual((projected["state"]["outcome"], projected["state"]["evidence"]), ("fail", "invalid"))
        self.assertEqual(db.one("SELECT * FROM runs WHERE id=?", (run["id"],)), before)
        self.assertEqual(receipt.read_bytes(), b"damaged execution evidence")
        self.assertEqual(first["passed"], 1)

    def test_unstarted_targets_are_counted_for_pending_and_finished_runs(self):
        self.queued("incomplete")
        db.update("runs", "incomplete", {"total": 3})
        for status in ("Queued", "Cancelled", "Pass"):
            with self.subTest(status=status):
                db.update("runs", "incomplete", {"status": status})
                item = self.client.get("/api/runs/incomplete").json()
                self.assertEqual((item["not_run"], item["state_counts"]["not_run"], item["passed"]), (3, 3, 0))
                self.assertEqual(item["recorded_counts"], {"passed": 0, "failed": 0, "blocked": 0})

    def test_named_unstarted_targets_match_history_filter_and_run_detail_without_persisting_results(self):
        self.queued("mixed")
        ids = ["PRESTART", "INTERRUPTED", "MISSING"]
        db.update("runs", "mixed", {"status": "Cancelled", "total": 3, "done": 2, "case_ids": ids})
        for case_id, started in zip(ids, (False, True)):
            db.insert("results", {"run_id": "mixed", "project_id": self.pid, "case_id": case_id, "case_title": case_id,
                                  "kind": "api", "engine": "http", "status": "Cancelled", "started_at": db.now(),
                                  "detail": {"target_started": started}})
        detail = self.client.get("/api/runs/mixed").json()
        self.assertEqual((detail["not_run"], detail["failed"], len(detail["results"])), (2, 1, 3))
        planned = next(row for row in detail["results"] if row["case_id"] == "MISSING")
        self.assertIsNone(planned["started_at"])
        for status, outcomes in (("NotRun", {"MISSING", "PRESTART"}), ("Fail", {"INTERRUPTED"})):
            history = self.client.get("/api/results?status=" + status).json()
            self.assertEqual({row["case_id"] for row in history["items"]}, outcomes)
            self.assertEqual({row["history_status"] for row in history["items"]}, {status})
            if status == "NotRun":
                self.assertIn(planned["id"], {row["id"] for row in history["items"]})
        summary = self.client.get("/api/dashboard").json()
        totals = summary["summary"]["results"]
        self.assertEqual((totals["not_run"], totals["failed"], summary["failures"]["total"]), (2, 1, 1))
        self.assertEqual((totals["reliability"]["denominator"], totals["reliability"]["not_run"]), (3, 2))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM results")["n"], 2)

    def test_unstarted_targets_follow_the_run_creation_window_and_yield_to_recorded_results(self):
        self.queued("scheduled")
        db.update("runs", "scheduled", {"case_ids": ["A", "B", "C"], "total": 3})
        detail = self.client.get("/api/runs/scheduled").json()
        self.assertEqual((detail["not_run"], len(detail["results"])), (3, 3))
        initial = self.client.get("/api/results?status=NotRun&limit=1&offset=1").json()
        self.assertEqual((initial["total"], len(initial["items"])), (3, 1))
        old = self.client.get("/api/results?end=2000-01-01").json()
        self.assertEqual(old["total"], 0)
        db.insert("results", {"run_id": "scheduled", "project_id": self.pid, "case_id": "A", "case_title": "A", "kind": "api",
                              "engine": "http", "status": "Fail", "started_at": db.now(), "detail": {}})
        history = self.client.get("/api/results").json()
        self.assertEqual((history["total"], history["state_counts"]), (3, {"pass": 0, "fail": 1, "not_run": 2}))
        page = self.client.get("/api/results?status=NotRun").json()
        self.assertEqual({row["case_id"] for row in page["items"]}, {"B", "C"})
        self.assertEqual({row["id"] for row in page["items"]}, {row["id"] for row in detail["results"] if row["case_id"] in ("B", "C")})

    def test_response_snapshot_does_not_cache_a_later_evidence_change(self):
        run = self.measured()
        scope = metrics.Scope()
        first = metrics.summary(scope)
        self.assertEqual(first["results"]["passed"], 1)
        row = db.one("SELECT * FROM results WHERE run_id=? AND case_id='TC-API-001'", (run["id"],))
        receipt = data_refs.resolve(json.loads(row["detail"])["attempts"][0]["path"])
        receipt.write_bytes(b"damaged execution evidence")
        later = metrics.summary(scope)
        self.assertEqual((later["results"]["passed"], later["results"]["not_run"], later["results"]["failed"]), (0, 0, 3))
        self.assertEqual(sum(point["passed"] for point in later["trend"]["points"]), 0)
        self.assertEqual(first["results"]["passed"], 1)

    def test_dashboard_bundle_matches_individual_views_without_changing_scope(self):
        self.measured()
        packet = self.client.get("/api/dashboard?limit=1").json()
        self.assertEqual(packet["summary"], self.client.get("/api/summary").json())
        self.assertEqual(packet["recent"], self.client.get("/api/runs?limit=1").json())
        self.assertEqual((packet["failures"]["total"], len(packet["failures"]["items"])), (2, 1))
        self.assertEqual(packet["failures"]["items"][0]["state"]["outcome"], "fail")
        self.assertNotIn("detail", packet["failures"]["items"][0])
        current = self.client.get("/api/dashboard?current=true&limit=6").json()
        self.assertEqual(current["failures"]["total"], current["summary"]["results"]["still_failing"])
        excluded = self.client.get("/api/dashboard?projects=another-project").json()
        self.assertEqual((excluded["summary"]["runs"]["total"], excluded["recent"]["total"], excluded["failures"]["total"]), (0, 0, 0))

    def test_dashboard_bundle_does_not_mix_a_concurrent_run_commit(self):
        self.measured()
        original = metrics.summary

        def commit_after_summary(*args, **kwargs):
            value = original(*args, **kwargs)
            with ThreadPoolExecutor(1) as pool:
                pool.submit(self.queued, "concurrent").result(timeout=5)
            return value

        with patch.object(metrics, "summary", side_effect=commit_after_summary):
            packet = self.client.get("/api/dashboard").json()
        self.assertEqual((packet["summary"]["runs"]["total"], packet["recent"]["total"]), (1, 1))
        self.assertNotIn("concurrent", {row["id"] for row in packet["recent"]["items"]})
        later = self.client.get("/api/dashboard").json()
        self.assertEqual((later["summary"]["runs"]["total"], later["recent"]["total"]), (2, 2))
