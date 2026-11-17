"""The public CLI preserves authored design sources through its compatible sheet and recording report."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, case_plans, execution_inputs, plan, projects, task_reports
from autoskill.recorder import session
from autoskill.server import jobs


class PlanFunctionalSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data"), "AUTOSKILL_CASE_SKILL": str(self.root / "plan"),
                                              "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": "", "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_BRIDGE": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("合成订单系统", "https://synthetic.example.invalid", project_id="plan-source")["id"]
        self.row = {"id": "DESIGN-001", "title": "提交指定草稿订单审核", "description": "验证审核员将指定草稿订单提交审核。", "module": "订单管理/审核",
                    "type": "页面", "priority": "P0", "preconditions": "审核员已登录，订单“DD-2026”处于草稿状态。",
                    "steps": ["1. 进入【草稿订单】。", "2. 打开订单“DD-2026”，点击【提交审核】。"],
                    "expected": "2. 订单“DD-2026”的业务状态变为“待审核”，不得直接变成“已通过”。",
                    "source": {"kind": "synthetic_requirement", "id": "REQ-SYN-1", "text": "审核员可提交草稿订单，提交后为待审核。", "references": ["原需求第2项"]},
                    "business_path": ["合成订单系统", "订单管理", "提交审核"], "prefix": "TJSH", "author": "需求编写人员", "remarks": "原文否定判据必须保留。"}

    def write(self, row=None):
        source = self.root / "input.json"
        source.write_text(json.dumps([row or self.row], ensure_ascii=False), encoding="utf-8")
        return plan.write(self.pid, str(source))

    def test_rich_cli_source_roundtrip_is_bound_to_its_business_row_and_online_overlay(self):
        self.write()
        shown = plan.show()["cases"][0]
        for key in plan.FUNCTIONAL_FIELDS:
            if key in self.row:
                self.assertEqual(shown[key], self.row[key])
        self.assertEqual(shown["steps"], "\n".join(self.row["steps"]))
        plan.mark("DESIGN-001", "阻塞", "环境不可用")
        self.assertEqual(plan.show()["cases"][0]["source"], self.row["source"])
        book, sheet, columns = plan._open()
        sheet.cell(2, columns["title"], "另一份替换设计")
        plan._save(book)
        book.close()
        self.assertNotIn("source", plan.show()["cases"][0])
        initial = case_plans.get(self.pid)
        online = case_plans.save(self.pid, {**self.row, "steps": "\n".join(self.row["steps"])}, case_id="DESIGN-001", expected_revision=initial["revision"])
        replacement = {**self.row, "source": {**self.row["source"], "version": 2}, "author": "新版来源编写人员"}
        with plan.use_folder(online["folder"]):
            self.write(replacement)
            self.assertEqual(plan.show()["cases"][0]["source"], replacement["source"])
        updated = case_plans.get(self.pid)
        self.assertEqual(updated["rows"][0]["source"], replacement["source"])
        saved = case_plans.save(self.pid, {"author": "在线编辑人员"}, case_id="DESIGN-001", expected_revision=updated["revision"])
        self.assertEqual(saved["rows"][0]["source"], replacement["source"])

    def test_array_steps_and_expectations_deliver_a_real_workbook_and_copyable_execution_source(self):
        row = {**self.row, "expected": ["1. 草稿列表显示指定订单。", self.row["expected"]]}
        written = self.write(row)
        self.assertEqual(written["test_report"]["status"], "ready", written)
        book = load_workbook(written["delivery_excel"])
        try:
            sheet = book["功能测试用例"]
            self.assertEqual(sheet.max_column, 14)
            cells = next(values for values in sheet.values if values[9] == "未执行")
            self.assertEqual(cells[5:7], ("\n".join(row["steps"]), "\n".join(row["expected"])))
        finally:
            book.close()
        shown = plan.show()["cases"][0]
        source = shown["execution_source"]
        self.assertEqual(source["task_id"], written["task_id"])
        self.assertEqual(source["case_id"], row["id"])
        self.assertEqual(source["file"], written["excel"])
        self.assertEqual(source["upstream"], row["source"])
        candidate = builder.api_case("TC-API-SOURCE", "Source-backed API", "GET", "/order", {"status": 200, "json_equals": {"/ok": True}})
        candidate["extensions"]["test_case_document"] = {"source": source}
        self.assertNotIn("error", plan.bind_execution(row["id"], self.pid, candidate, None))
        replacement = self.write(row)
        self.assertNotEqual(replacement["task_id"], source["task_id"])
        self.assertIn("error", plan.bind_execution(row["id"], self.pid, candidate, None))

    def test_cli_record_plan_freezes_full_source_design_before_later_sheet_edits(self):
        self.write({**self.row, "priority": "高"})
        recording = {"project_id": self.pid, "start_url": "/__technical/draft", "steps": [
            {"action": "click", "selector": "#__technical_submit"},
            {"assertion": "text", "selector": "#__technical_state", "expected": "待审核", "oracle": {"source": "explicit"}}],
            "secrets": [], "used_login": False, "as_login": False}
        session._save(recording)
        with patch.object(session.agent, "call", return_value=None), \
                patch.object(session.capture, "stop", return_value={"source": "cdp_network", "entries": [], "available": False}), \
                patch.object(jobs, "ensure_worker", return_value={}):
            result = session.stop("提交指定草稿订单审核", plan_case="DESIGN-001")
        saved = next(case for case in result["candidates"] if case["id"] == result["ui_case"]["id"])
        self.assertTrue(result["run_id"], result)
        self.assertEqual(saved["priority"], "P0")
        self.assertEqual(projects.cases(self.pid), [])
        document = saved["extensions"]["test_case_document"]
        self.assertEqual(document["priority"], "高")
        for key in ("description", "business_path", "prefix", "author", "remarks"):
            self.assertEqual(document[key], self.row[key])
        self.assertEqual(document["source"]["upstream"], self.row["source"])
        self.assertEqual(saved["steps"], self.row["steps"])
        self.assertEqual(saved["expected"], self.row["expected"])
        unhashed = copy.deepcopy(document)
        declared_hash = unhashed["source"].pop("design_sha256")
        self.assertEqual(execution_inputs.content_hash(unhashed), declared_hash)
        report_file = task_reports._folder(result["test_report"]["report_id"]) / "snapshot.json"
        frozen_bytes = report_file.read_bytes()
        frozen = json.loads(frozen_bytes)
        self.assertEqual(frozen["cases"][0]["definition"]["extensions"]["test_case_document"], document)
        self.write({**self.row, "source": {"version": 2}, "expected": "2. 来源已修订，等待新独立检查。"})
        self.assertEqual(report_file.read_bytes(), frozen_bytes)


if __name__ == "__main__":
    unittest.main()
