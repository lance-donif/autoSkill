"""Case sheet verdicts follow revisions, and generated Excel content stays literal text."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, plan, projects, reporting, runner
from autoskill.replay import api


class PlanReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name,
                                          "AUTOSKILL_CASE_SKILL": str(self.folder / "case-skill"), "AUTOSKILL_HEADED": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pid = projects.create("=1+1", "http://127.0.0.1:9", project_id="review")["id"]
        self.case_id = "TC-API-001"
        projects.save_cases(self.pid, [builder.api_case(self.case_id, "=2+2", "GET", "/health",
                                                       {"status": 200, "json_equals": {"/ok": True}})])

    def write_plan(self, item=None):
        source = self.folder / "designed.json"
        source.write_text(json.dumps([item or {"id": "TC-001", "title": "Health check"}]), encoding="utf-8")
        return plan.write(self.pid, str(source))

    def record_result(self, status, reason="", project_id=None):
        pid = project_id or self.pid
        case = projects.case(pid, self.case_id)
        run = runner.create(pid, [self.case_id])
        expected = case["assertions"]["status"]
        response_status = expected if status == "Pass" else 200 if expected != 200 else 500
        transport = httpx.MockTransport(lambda _request: httpx.Response(response_status, json={"ok": True}))
        original = api.run_case
        def evaluate(*args):
            outcome = original(*args)
            if reason:
                outcome["reason"] = reason
            return outcome
        with patch.object(api, "new_client", side_effect=lambda *args: httpx.Client(transport=transport)), patch.object(api, "run_case", side_effect=evaluate):
            final = runner.execute(run["id"])
        self.assertEqual(final["status"], status)

    def test_sync_invalidates_old_pass_and_accepts_a_result_for_the_new_revision(self):
        self.write_plan()
        plan.link("TC-001", self.pid, self.case_id, 1)
        self.record_result("Pass")
        self.assertEqual(plan.sync()["summary"]["通过"], 1)

        changed = projects.case(self.pid, self.case_id)
        changed["assertions"]["status"] = 201
        projects.save_cases(self.pid, [changed])
        synced = plan.sync()
        row = plan.show()["cases"][0]
        self.assertEqual((synced["summary"]["通过"], row["status"]), (0, "未执行"))
        self.assertIn("重新回放", row["note"])
        self.assertEqual(reporting.build(self.pid)["not_run"], 1)

        self.record_result("Fail", "Expected HTTP 201, received 200")
        self.assertEqual(plan.sync()["summary"]["失败"], 1)
        self.assertEqual(plan.show()["cases"][0]["note"], "Expected HTTP 201, received 200")

    def test_generated_case_fields_and_project_name_are_literal_excel_text(self):
        fields = dict.fromkeys(("id", "module", "title", "type", "priority", "preconditions", "steps", "expected"), "=1+1")
        made = self.write_plan(fields)
        book = load_workbook(made["excel"])
        self.addCleanup(book.close)
        for key, label, _ in plan.COLUMNS:
            if key in fields:
                column = next(cell.column for cell in book["用例"][1] if cell.value == label)
                cell = book["用例"].cell(2, column)
                self.assertEqual((cell.value, cell.data_type), (fields[key], "s"), key)
        self.assertEqual((book["信息"]["B2"].value, book["信息"]["B2"].data_type), ("=1+1", "s"))

    def test_sync_preserves_design_rows_and_updates_other_projects_after_a_project_is_removed(self):
        other = projects.create("Remaining project", "http://127.0.0.1:10", project_id="remaining")["id"]
        projects.save_cases(other, [builder.api_case(self.case_id, "Remaining case", "GET", "/health",
                                                   {"status": 200, "json_equals": {"/ok": True}})])
        source = self.folder / "designed.json"
        source.write_text(json.dumps([{"id": "TC-001", "title": "Removed project design"},
                                      {"id": "TC-002", "title": "Remaining project design"}]), encoding="utf-8")
        plan.write(self.pid, str(source))
        plan.link("TC-001", self.pid, self.case_id, 1)
        plan.link("TC-002", other, self.case_id, 1)
        self.record_result("Pass")
        self.record_result("Pass", project_id=other)
        self.assertEqual(plan.sync()["summary"]["通过"], 2)

        shutil.rmtree(projects.path(self.pid))
        self.assertEqual(plan.sync(quiet=True)["updated"], 2)
        rows = plan.show()["cases"]
        self.assertEqual([(row["id"], row["title"], row["status"]) for row in rows],
                         [("TC-001", "Removed project design", "未执行"), ("TC-002", "Remaining project design", "通过")])
        self.assertEqual(rows[0]["replay"], self.pid + "/" + self.case_id)

    def test_manual_and_replay_notes_stay_literal_without_rewriting_existing_formulas(self):
        made = self.write_plan()
        plan.mark("TC-001", "阻塞", "=3+3")
        book = load_workbook(made["excel"])
        self.assertEqual((book["用例"]["J2"].value, book["用例"]["J2"].data_type), ("=3+3", "s"))
        book.close()

        plan.link("TC-001", self.pid, self.case_id, 1)
        self.record_result("Fail", "=4+4")
        plan.sync()
        book = load_workbook(made["excel"])
        self.assertEqual((book["用例"]["J2"].value, book["用例"]["J2"].data_type), ("=4+4", "s"))
        book.close()

        custom = Workbook()
        custom.active.append(["用例编号", "用例标题", "辅助计算"])
        custom.active.append(["TC-X", "Custom case", "=1+1"])
        custom.save(made["excel"])
        custom.close()
        plan.mark("TC-X", "通过", "=5+5")
        book = load_workbook(made["excel"])
        self.addCleanup(book.close)
        self.assertEqual((book.active["C2"].value, book.active["C2"].data_type), ("=1+1", "f"))
        self.assertEqual((book.active["E2"].value, book.active["E2"].data_type), ("=5+5", "s"))

    def test_report_keeps_project_name_case_title_and_failure_reason_as_text(self):
        self.record_result("Fail", "=6+6")
        made = reporting.build(self.pid)
        book = load_workbook(made["excel"])
        self.addCleanup(book.close)
        for sheet, coordinate, value in (("概览", "B1", "=1+1"), ("用例与结果", "D2", "=2+2"), ("用例与结果", "J2", "=6+6")):
            cell = book[sheet][coordinate]
            self.assertEqual((cell.value, cell.data_type), (value, "s"))
        self.assertEqual(book["概览"]["B4"].data_type, "n")


if __name__ == "__main__":
    unittest.main()
