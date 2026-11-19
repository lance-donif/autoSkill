"""First and last verified attempts share a denominator with unexecuted target cases."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import db, execution_inputs, projects, task_state
from autoskill.server import metrics


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.pid = projects.create("指标", "http://127.0.0.1:9", project_id="rates")["id"]
        db.insert("runs", {"id": "measured", "project_id": self.pid, "engine": "playwright", "status": "Cancelled", "total": 6, "done": 4,
                           "input_manifest": "fixture", "created_at": db.now()})

    def result(self, case_id, status, detail, prerequisite=False):
        db.insert("results", {"run_id": "measured", "project_id": self.pid, "case_id": case_id, "case_title": case_id, "kind": "api",
                              "engine": "http", "status": status, "started_at": db.now(), "detail": detail, "prerequisite": int(prerequisite)})

    def attempt(self, name, outcome):
        path = Path(self.tmp.name) / f"{name}.json"
        receipt = {"outcome": outcome}
        execution_inputs.write_json(path, receipt)
        return {"path": str(path), "sha256": execution_inputs.content_hash(receipt), "status": outcome["status"]}

    def test_attempt_rates_include_assertion_free_execution_and_unstarted_targets(self):
        success = {"status": "Pass", "execution_only_pass": True, "target_started": True}
        failure = {"status": "Fail", "business_status": "Fail", "target_started": True}
        self.result("one", "Pass", success)
        self.result("two", "Fail", {**failure, "attempts": [self.attempt("first", failure), self.attempt("final", success)]})
        self.result("three", "Cancelled", {"target_started": True})
        self.result("four", "Blocked", {"target_started": False})
        self.result("login", "Pass", success, prerequisite=True)
        with patch.object(task_state, "_evidence", return_value=True):
            row = metrics.result_counts(metrics.Scope([self.pid]))["reliability"]
        self.assertEqual((row["denominator"], row["first_passed"], row["final_passed"]), (6, 1, 2))
        self.assertEqual((row["recovered_after_failure"], row["not_run"], row["unexecuted"]), (1, 3, 3))
        self.assertEqual((row["first_pass_rate"], row["final_pass_rate"]), (0.1667, 0.3333))

    def test_unknown_native_target_count_never_becomes_a_hundred_percent_claim(self):
        self.result("one", "Pass", {"business_status": "Pass"})
        db.insert("runs", {"id": "native", "project_id": self.pid, "engine": "jmeter", "family": "native", "status": "Queued", "total": 0, "created_at": db.now()})
        row = metrics.reliability(metrics.Scope([self.pid]))
        self.assertEqual(row["unknown_target_runs"], 1)
        self.assertIsNone(row["first_pass_rate"])
        self.assertIsNone(row["final_pass_rate"])

    def test_legacy_success_with_missing_evidence_is_a_failed_result(self):
        self.result("old", "Pass", {})
        view = metrics.result_counts(metrics.Scope([self.pid]))
        self.assertEqual((view["failed"], view["reliability"]["first_passed"]), (1, 0))
        excluded = metrics.reliability(metrics.Scope(["other"]))
        self.assertEqual(excluded["denominator"], 0)
        self.assertIsNone(excluded["first_pass_rate"])
