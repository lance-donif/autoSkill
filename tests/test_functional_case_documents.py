"""Saved UI/API designs drive every human workbook while technical inputs and failures remain traceable."""
import copy
import hashlib
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

from autoskill import builder, case_document, case_folders, execution_inputs, plan, projects, reporting, task_reports
from autoskill.recorder import session


def design(kind):
    if kind == "ui":
        return {"description": "验证审核员将指定草稿订单提交审核。", "preconditions": "审核员已登录，订单“DD-2026”处于草稿状态。",
                "steps": ["进入“订单管理-草稿订单”页面。", "打开订单“DD-2026”，点击【提交审核】。"],
                "expected": "2. 订单“DD-2026”的业务状态变为“待审核”。", "business_path": ["合成订单系统", "订单管理", "提交审核"],
                "prefix": "TJSH", "source": {"id": "REQ-SYNTHETIC-1", "text": "审核员可将草稿订单提交到待审核状态。"}}
    return {"description": "验证审核员按客户查询待审核订单。", "preconditions": "调用账号具有审核权限，客户“测试客户甲”存在待审核订单“DD-2026”。",
            "steps": ["以审核员账号，按客户“测试客户甲”和状态“待审核”查询订单。", "核对返回的订单编号及业务状态。"],
            "expected": "2. 返回订单“DD-2026”，业务状态为“待审核”。", "business_path": ["合成订单系统", "订单管理", "待审核查询"],
            "prefix": "DSHCX", "source": {"id": "REQ-SYNTHETIC-2", "text": "有审核权限的账号可按客户和待审核状态查询订单。"}}


def functional_rows(book):
    return {row[0]: row for row in book["功能测试用例"].iter_rows(min_row=2, values_only=True)
            if isinstance(row[0], str) and row[0].startswith("FT-")}


class FunctionalCaseDocumentsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data"), "AUTOSKILL_BRIDGE": "off",
                                              "AUTOSKILL_CASE_SKILL": str(self.root / "plan"), "AUTOSKILL_SESSION": "", "AUTOSKILL_WORKFLOW_BINDING": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("合成订单系统", "https://synthetic.example.invalid", project_id="functional")["id"]

    def cases(self):
        automation = {"start_url": "/__technical/ui", "actions": [{"action": "click", "selector": "#__technical_submit"}],
                      "assertions": [{"assertion": "text", "selector": "#__technical_state", "expected": "待审核"}]}
        ui = builder.ui_case("TC-UI-001", "提交草稿订单审核", automation, test_case_document=design("ui"))
        api = builder.api_case("TC-API-001", "查询指定客户的待审核订单", "POST", "/__technical/query",
                               {"status": 200, "json_equals": {"/order_id": "DD-2026", "/state": "待审核"}},
                               json={"customer": "测试客户甲", "state": "待审核"}, test_case_document=design("api"))
        return [ui, api]

    def assert_functional_content(self, file):
        book = load_workbook(file, data_only=False)
        self.addCleanup(book.close)
        self.assertEqual(book.sheetnames[0], "功能测试用例")
        self.assertEqual(book.worksheets[0].max_column, 14)
        rows = functional_rows(book)
        mappings = {row[1]: row for row in book["编号映射"].iter_rows(min_row=2, values_only=True)}
        for identity, kind in (("TC-UI-001", "ui"), ("TC-API-001", "api")):
            if identity not in mappings:
                continue
            fields = design(kind)
            row = rows[mappings[identity][0]]
            self.assertEqual(row[0], "FT-" + fields["prefix"] + "-001")
            self.assertEqual(row[1], "-".join(fields["business_path"]))
            self.assertEqual(row[2], fields["business_path"][-1])
            self.assertEqual(row[3], fields["description"])
            self.assertEqual(row[4], fields["preconditions"])
            self.assertEqual(row[5], "\n".join(f"{n}. {step}" for n, step in enumerate(fields["steps"], 1)))
            self.assertEqual(row[6], fields["expected"])
            self.assertNotIn("__technical", "\n".join(str(value) for value in row[:8]))
        original = "\n".join(str(row[1]) for row in book["来源与冻结定义"].iter_rows(min_row=2, values_only=True))
        self.assertIn("__technical", original)
        return book, rows, mappings

    def test_ui_api_save_and_project_folder_task_exports_share_source_backed_content(self):
        cases = self.cases()
        projects.save_cases(self.pid, cases)
        for saved in projects.cases(self.pid):
            supplied = design(saved["kind"])
            self.assertEqual(saved["steps"], supplied["steps"])
            self.assertEqual(saved["expected"], supplied["expected"])
            self.assertEqual(saved["extensions"]["test_case_document"]["source"], supplied["source"])
        before = {case["id"]: execution_inputs.content_hash(case) for case in projects.cases(self.pid)}
        self.assert_functional_content(reporting.export_cases(self.pid, list(before))["excel"])
        self.assert_functional_content(reporting.build(self.pid)["excel"])
        tree = case_folders.list_folders(self.pid)
        tree = case_folders.create(self.pid, "本轮回归资料", expected_revision=tree["revision"])
        target = next(folder["id"] for folder in tree["folders"] if folder["name"] == "本轮回归资料")
        case_folders.move_cases(self.pid, list(before), target, expected_revision=tree["revision"])
        selected = case_folders.selection(self.pid, target)
        book, _, mappings = self.assert_functional_content(reporting.export_cases(self.pid, selected["case_ids"], organization=selected)["excel"])
        self.assertTrue(all(json.loads(row[9]) == ["合成订单系统", "本轮回归资料"] for row in mappings.values()))
        self.assertEqual(before, {case["id"]: execution_inputs.content_hash(case) for case in projects.cases(self.pid)})
        self.assertEqual({row[1] for row in book["编号映射"].iter_rows(min_row=2, values_only=True)}, set(before))

    def test_legacy_requests_need_functional_design_without_erasing_a_failure(self):
        case = builder.api_case("TC-API-001", "查询 /__technical/query", "GET", "/__technical/query", {"status": 200})
        case["title"] = "查询 /__technical/query"
        case["steps"] = ["GET /__technical/query"]
        case["expected"] = "HTTP 200"
        original = copy.deepcopy(case)
        snapshot = {"project_id": self.pid, "project": {"name": "合成订单系统"}, "kind": "run", "task_id": "synthetic-run",
                    "title": "合成失败证据", "status": "Fail", "run_ids": ["synthetic-run"], "sources": {}, "metadata": {}, "notes": [],
                    "cases": [{"definition": case, "result": {"status": "Fail", "detail": {"business_status": "Fail"}, "reason": "独立断言失败"},
                               "evidence_valid": False, "run_id": "synthetic-run"}], "results": []}
        file = self.root / "legacy.xlsx"
        reporting.write_task(snapshot, file)
        book = load_workbook(file)
        self.addCleanup(book.close)
        row = next(iter(functional_rows(book).values()))
        self.assertIn("待确认", row[3])
        self.assertIn(case_document.MISSING_STEPS, row[5])
        self.assertIn(case_document.MISSING_EXPECTED, row[6])
        self.assertNotIn("GET /", row[5])
        self.assertNotIn("HTTP 200", row[6])
        self.assertEqual(row[9], "失败")
        for field in (row[4], row[5], row[6]):
            self.assertIn("【待确认", field)
        self.assertIn("必要的功能前置条件", row[12])
        self.assertIn("功能测试步骤尚未提供", row[12])
        self.assertIn("有来源支持的预期结果", row[12])
        self.assertEqual(original, case)
        self.assertIn("GET /__technical/query", "\n".join(str(row[1]) for row in book["来源与冻结定义"].iter_rows(min_row=2, values_only=True)))

    def test_document_overrides_legacy_display_fields_without_mutating_frozen_definition(self):
        case = self.cases()[0]
        case["steps"] = ["打开 /__technical/ui", "点击 选择器 #__technical_submit"]
        case["expected"] = "文本为“待审核”"
        original = copy.deepcopy(case)
        self.assertEqual(case_document.functional_steps(case), design("ui")["steps"])
        self.assertEqual(case_document.functional_expected(case), design("ui")["expected"])
        self.assertEqual(original, case)

    def test_recording_with_plan_freezes_actual_functional_design_before_its_report(self):
        row = {"id": "DESIGN-001", "module": "订单管理", "title": "验证草稿订单提交审核", "preconditions": design("ui")["preconditions"],
               "steps": design("ui")["steps"], "expected": design("ui")["expected"]}
        source = self.root / "design.json"
        source.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
        plan.write(self.pid, str(source))
        rec = {"project_id": self.pid, "start_url": "/__technical/ui", "steps": [
            {"action": "click", "selector": "#__technical_submit"},
            {"assertion": "text", "selector": "#__technical_state", "expected": "待审核"}],
            "secrets": [], "used_login": False, "as_login": False}
        session._save(rec)
        with patch.object(session.agent, "call", return_value=None), patch("autoskill.server.jobs.ensure_worker"):
            result = session.stop("提交订单审核", plan_case="DESIGN-001")
        saved = next(case for case in result["candidates"] if case["id"] == result["ui_case"]["id"])
        document = saved["extensions"]["test_case_document"]
        self.assertEqual(saved["steps"], [f"{n}. {step}" for n, step in enumerate(row["steps"], 1)])
        self.assertEqual(saved["expected"], row["expected"])
        self.assertEqual(document["source"]["case_id"], "DESIGN-001")
        self.assertEqual(len(document["source"]["design_sha256"]), hashlib.sha256().digest_size * 2)
        unhashed = copy.deepcopy(document)
        digest = unhashed["source"].pop("design_sha256")
        self.assertEqual(execution_inputs.content_hash(unhashed), digest)
        receipt = task_reports.get(result["test_report"]["report_id"])
        book = load_workbook(receipt["excel"])
        self.addCleanup(book.close)
        exported = next(iter(functional_rows(book).values()))
        self.assertIn("点击【提交审核】", exported[5])
        self.assertNotIn("__technical", exported[5])
        self.assertEqual(exported[6], row["expected"])
        self.assertEqual(exported[9], "业务未验证")

    def test_conversation_questions_and_independent_expected_values_are_known_functional_facts(self):
        case = {"kind": "native", "turns": [{"question": "2025年销售额是多少？", "expected": 700},
                                              {"question": "北京呢？", "reference": {"operation": "sum", "field": "sales"}}],
                "dataset": [{"sales": 100}, {"sales": 100}]}
        self.assertEqual(case_document.functional_steps(case), ["向助手提问：“2025年销售额是多少？”。", "向助手提问：“北京呢？”。"])
        self.assertEqual(case_document.functional_expected(case), "1. 本轮回答应为：700\n2. 本轮回答应为：200")

    def test_builder_rejects_a_raw_request_masquerading_as_functional_steps(self):
        with self.assertRaisesRegex(ValueError, "具体业务操作"):
            builder.api_case("TC-API-001", "查询订单", "GET", "/orders", {"status": 200},
                             test_case_document={"steps": ["GET /orders"]})
        case = builder.ui_case("TC-UI-001", "验证表单", {"start_url": "/form", "actions": [], "assertions": []})
        self.assertEqual(case["steps"], [case_document.MISSING_STEPS])
        self.assertIn("待确认", case["expected"])


if __name__ == "__main__":
    unittest.main()
