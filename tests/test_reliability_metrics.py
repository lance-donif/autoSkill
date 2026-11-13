"""First and final business metrics retain blocked, cancelled, and unverified target executions."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import db, projects
from autoskill.server import metrics


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name})
        self.env.start()
        self.pid = projects.create("指标", "http://127.0.0.1:9", project_id="rates")["id"]
        db.insert("runs", {"id": "measured", "project_id": self.pid, "engine": "playwright", "status": "Cancelled", "total": 6, "done": 4, "created_at": db.now()})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def result(self, case_id, status, detail, prerequisite=False):
        db.insert("results", {"run_id": "measured", "project_id": self.pid, "case_id": case_id, "case_title": case_id, "kind": "api",
                              "engine": "http", "status": status, "started_at": db.now(), "detail": detail, "prerequisite": int(prerequisite)})

    def test_recovery_has_one_fixed_denominator_and_keeps_unverified_and_cancelled(self):
        self.result("one", "Pass", {"business_status": "Pass", "attempts": [{"business_status": "Pass"}]})
        self.result("two", "Fail", {"business_status": "Fail", "attempts": [{"business_status": "Fail"}, {"business_status": "Pass"}]})
        self.result("three", "Pass", {"business_status": "Unverified"})
        self.result("four", "Blocked", {"business_status": "Unverified"})
        self.result("login", "Pass", {"business_status": "Pass"}, prerequisite=True)
        row = metrics.result_counts(metrics.Scope([self.pid]))["reliability"]
        self.assertEqual((row["denominator"], row["first_passed"], row["final_passed"]), (6, 1, 2))
        self.assertEqual((row["recovered_after_failure"], row["unverified"], row["blocked"]), (1, 2, 1))
        self.assertEqual((row["cancelled"], row["unexecuted"]), (2, 2))
        self.assertEqual((row["first_pass_rate"], row["final_pass_rate"]), (0.1667, 0.3333))

    def test_unknown_native_target_count_never_becomes_a_hundred_percent_claim(self):
        self.result("one", "Pass", {"business_status": "Pass"})
        db.insert("runs", {"id": "native", "project_id": self.pid, "engine": "jmeter", "family": "native", "status": "Queued", "total": 0, "created_at": db.now()})
        row = metrics.reliability(metrics.Scope([self.pid]))
        self.assertEqual(row["unknown_target_runs"], 1)
        self.assertIsNone(row["first_pass_rate"])
        self.assertIsNone(row["final_pass_rate"])

    def test_legacy_success_has_no_invented_business_verdict_and_scope_is_applied(self):
        self.result("old", "Pass", {})
        row = metrics.reliability(metrics.Scope([self.pid]))
        self.assertEqual((row["first_passed"], row["unverified"]), (0, 1))
        excluded = metrics.reliability(metrics.Scope(["other"]))
        self.assertEqual(excluded["denominator"], 0)
        self.assertIsNone(excluded["first_pass_rate"])
