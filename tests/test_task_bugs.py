"""Issue delivery preserves failed or unknown facts and retries only frozen report generation."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

import test_task_markdown as markdown_fixtures
import test_task_reports as fixtures

from autoskill import data_refs, db, deletion, execution_inputs, projects, runner, task_bugs, task_report_data, task_reports
from autoskill.server.app import create_app


class TaskBugContentTests(unittest.TestCase):
    def test_categories_preserve_first_failure_and_do_not_confuse_environment_with_business(self):
        value = markdown_fixtures.snapshot()
        observe = markdown_fixtures.observation
        value["cases"] = [observe("business", "Fail", reason="Wrong priority"), observe("business", "Pass"),
                          observe("network", "Blocked", reason="Connection refused"), observe("unknown", "Fail", valid=False),
                          observe("recording", None, recorded={"status": "Fail", "actual": "Observed label"}),
                          observe("history", None), observe("pending", None)]
        value["cases"][-2]["historical_result"] = {"status": "Fail", "reason": "Original business failure"}
        text, summary = task_bugs.render(value, excel_link="cases.xlsx")
        self.assertEqual(summary["issues"], 5)
        self.assertEqual(summary["counts"], {"business": 1, "execution": 1, "unverified": 1, "recording": 1, "historical": 1})
        for expected in ("Wrong priority", "Connection refused", "Observed label", "Original business failure", "后续复测：通过", "此前失败仍保留"):
            self.assertIn(expected, text)
        self.assertIn("未执行", text)
        self.assertEqual(text.count("## BUG-"), 5)

    def test_multiturn_checks_stay_in_one_case_and_keep_zero_expected(self):
        value = markdown_fixtures.snapshot()
        item = markdown_fixtures.observation("dialogue", "Fail")
        item["result"]["detail"]["turns"] = [{"assertions": [{"name": "amount", "passed": False, "expected": 0, "actual": 9}]},
                                            {"assertions": [{"name": "amount", "passed": True, "expected": 0, "actual": 0}]}]
        item["result"]["detail"]["screenshots"] = "turn-1.png"
        item["result"]["detail"]["attempts"] = 1
        value["cases"] = [item]
        text, summary = task_bugs.render(value, excel_link="cases.xlsx")
        self.assertEqual(summary["issues"], 1)
        self.assertIn('"预期": 0', text)
        self.assertIn('"实际": 9', text)
        self.assertIn("turn-1.png", text)
        self.assertNotIn("t、u、r、n", text)
        item["definition"]["expected"] = False
        item["result"]["detail"]["turns"] = []
        self.assertIn("预期：False", task_bugs.render(value, excel_link="cases.xlsx")[0])

    def test_no_results_do_not_claim_bug_free_and_blocking_reason_is_visible(self):
        value = markdown_fixtures.snapshot()
        value.update(status="Blocked", cases=[markdown_fixtures.observation("unexecuted", None)])
        value["metadata"]["reason"] = "Missing actual engine"
        value["cases"][0]["note"] = "Missing actual engine"
        text, summary = task_bugs.render(value, excel_link="cases.xlsx")
        self.assertEqual(summary["counts"]["execution"], 1)
        self.assertEqual(summary["counts"]["business"], 0)
        self.assertIn("Missing actual engine", text)
        self.assertIn("未执行", text)
        value["status"] = "Cancelled"
        value["project"]["name"] = '<script>bad()</script> [fake](javascript:bad)'
        text, summary = task_bugs.render(value, excel_link="cases.xlsx")
        self.assertEqual(summary["issues"], 0)
        self.assertIn("不等于全部需求均已验证", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("[fake]", text)


class TaskBugDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.TaskReportTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.TaskReportTests.setUp(self)

    def execute(self, case_id="TC-API-001"):
        return runner.execute(runner.create(self.pid, [case_id])["id"])

    def test_real_http_failure_download_and_new_pass_keep_original_bug_file(self):
        run = self.execute()
        report = run["test_report"]
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["bugs_counts"]["business"], 1)
        original = Path(report["bugs"]).read_bytes()
        self.assertEqual((len(original), hashlib.sha256(original).hexdigest()), (report["bugs_size"], report["bugs_sha256"]))
        self.assertTrue(report["validation"]["bugs_readable"])
        for expected in ("TC-API-001", "high", "medium"):
            self.assertIn(expected, original.decode())
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            response = client.get(report["bugs_download_url"])
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(response.content, original)
        changed = copy.deepcopy(self.case)
        changed["assertions"]["json_equals"]["/priority"] = "medium"
        projects.save_cases(self.pid, [changed])
        passed = self.execute()["test_report"]
        self.assertEqual(passed["bugs_count"], 0, passed)
        self.assertEqual(Path(report["bugs"]).read_bytes(), original)

    def test_legacy_two_files_survive_repeated_export_failure_without_live_recapture(self):
        run = self.execute()
        report = run["test_report"]
        old = {name: (Path(report[name]).read_bytes(), Path(report[name]).stat().st_mtime_ns) for name in ("excel", "markdown")}
        saved = copy.deepcopy(report)
        Path(saved["bugs"]).unlink()
        for key in list(saved):
            if key.startswith("bugs"):
                saved.pop(key)
        saved["validation"].pop("bugs_readable", None)
        execution_inputs.write_json(task_reports._folder(saved["report_id"]) / "receipt.json", saved)
        changed = copy.deepcopy(self.case)
        changed["assertions"]["json_equals"]["/priority"] = "medium"
        projects.save_cases(self.pid, [changed])
        count = db.one("SELECT COUNT(*) n FROM runs")["n"]
        with patch.object(task_report_data, "run_snapshot", side_effect=AssertionError("No live recapture")), \
                patch.object(runner, "execute", side_effect=AssertionError("No test execution")):
            with patch.object(task_bugs, "write_task", side_effect=OSError("Synthetic export interruption")):
                first = task_reports.for_run(run["id"])
                second = task_reports.retry(saved["report_id"])
            self.assertEqual((first["status"], second["status"]), ("failed", "failed"))
            self.assertNotIn("_retained_artifacts", second)
            fixed = task_reports.retry(saved["report_id"])
        self.assertEqual(fixed["status"], "ready", fixed)
        self.assertEqual(fixed["bugs_counts"]["business"], 1)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], count)
        for name, expected in old.items():
            self.assertEqual((Path(fixed[name]).read_bytes(), Path(fixed[name]).stat().st_mtime_ns), expected)

    def test_tamper_blocks_three_downloads_and_retry_repairs_only_bug_markdown(self):
        run = self.execute()
        report = run["test_report"]
        excel, brief = Path(report["excel"]).read_bytes(), Path(report["markdown"]).read_bytes()
        original = Path(report["bugs"]).read_bytes()
        Path(report["bugs"]).write_bytes(b"changed")
        self.assertEqual(task_reports.get(report["report_id"])["status"], "failed")
        for kind in ("excel", "markdown", "bugs"):
            with self.assertRaises(ValueError):
                task_reports.download(report["report_id"], format=kind)
        with patch.object(runner, "execute", side_effect=AssertionError("No rerun")):
            fixed = task_reports.retry(report["report_id"])
        self.assertEqual(fixed["status"], "ready", fixed)
        self.assertEqual(Path(fixed["bugs"]).read_bytes(), original)
        self.assertEqual(Path(fixed["excel"]).read_bytes(), excel)
        self.assertEqual(Path(fixed["markdown"]).read_bytes(), brief)
        deletion.delete_runs([run["id"]])
        with self.assertRaises(ValueError):
            task_reports.download(report["report_id"], format="bugs")
        self.assertEqual(task_reports.retry(report["report_id"])["status"], "skipped")

    def test_invalid_new_file_cannot_make_a_ready_receipt_and_partial_excel_is_retained(self):
        def invalid_file(snapshot, destination, **kwargs):
            destination.write_bytes(b"\xff")
            return {"bugs_validation": {"bugs_readable": True}}

        with patch.object(task_bugs, "write_task", side_effect=invalid_file):
            run = self.execute()
        failed = run["test_report"]
        self.assertEqual((run["status"], failed["status"]), ("Fail", "failed"))
        folder = task_reports._folder(failed["report_id"])
        retained = json.loads((folder / "receipt.json").read_bytes())["_retained_artifacts"]
        excel = data_refs.resolve(retained["excel"])
        previous = (excel.read_bytes(), excel.stat().st_mtime_ns)
        fixed = task_reports.retry(failed["report_id"])
        self.assertEqual(fixed["status"], "ready", fixed)
        self.assertEqual((excel.read_bytes(), excel.stat().st_mtime_ns), previous)


if __name__ == "__main__":
    unittest.main()
