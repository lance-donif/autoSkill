"""Read actual task workbooks against the supplied case-document rules and frozen facts."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import reporting, task_bugs, task_report_data, task_workbook, task_workbook_data


def definition(identity="TC-UI-001"):
    return {"id": identity, "kind": "ui", "title": "验证搜索历史返回已保存记录", "module": "个人工作台", "priority": "P1",
            "preconditions": "已保存两条搜索历史；访问已授权的测试账号。", "steps": ["打开个人工作台", "点击搜索历史"],
            "expected": "2. 展示已保存的两条历史记录。", "extensions": {"test_case_document": {
                "business_path": ["1. 数智运营管理", "1.1 个人工作台", "1.1.1 智能助手", "1.1.1.1 搜索历史"], "prefix": "SSLS", "author": "测试编制人甲"}}}


def observation(case, status="Pass", run="run-a", *, valid=True):
    result = {"case_id": case["id"], "case_title": case["title"], "run_id": run, "status": status, "reason": "",
              "prerequisite": 0, "actual": "返回 2 条已保存记录" if status == "Pass" else "返回 0 条记录",
              "detail": {"business_status": status}, "engine": "playwright", "duration_ms": 23, "started_at": "2026-11-13T01:00:00Z"}
    return {"definition": case, "result": result, "run_id": run, "evidence_valid": valid, "environment": ["被测地址：http://synthetic.local"]}


def snapshot(items):
    return {"kind": "workflow", "task_id": "workflow-a", "cycle_id": "cycle-a", "project_id": "project-a", "project": {"name": "数智运营管理"},
            "title": "合成来源搜索历史验证", "status": "violated", "cases": items, "run_ids": list(dict.fromkeys(item.get("run_id", "") for item in items)),
            "results": [item["result"] for item in items if item.get("result")], "sources": {"source": "synthetic requirement"}, "notes": [], "metadata": {}}


class TaskWorkbookTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def render(self, data):
        file = self.folder / "report.xlsx"
        receipt = reporting.write_task(data, file)
        book = load_workbook(file)
        self.addCleanup(book.close)
        self.assertTrue(receipt["validation"]["structure"]["ok"], receipt)
        self.assertEqual(receipt["template_id"], "test-cases-14-column-v1")
        self.assertEqual(book.sheetnames[0], "功能测试用例")
        sheet = book.worksheets[0]
        grouped = {number for merged in sheet.merged_cells.ranges for number in range(merged.min_row, merged.max_row + 1)}
        rows = [row for number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2) if number not in grouped and row[0]]
        return book, rows, receipt

    def test_real_hierarchy_canonical_fields_and_failure_history(self):
        case = definition()
        first, final = observation(case, "Fail"), observation(copy.deepcopy(case), "Pass", "run-b")
        first["result"]["detail"]["attempts"] = [{"status": "Fail", "receipt": "/synthetic/attempt-1.json", "sha256": "frozen-proof",
                                                "screenshot": "/synthetic/failure.png"}]
        book, rows, receipt = self.render(snapshot([first, final]))
        self.assertEqual(receipt["cases"], 1)
        self.assertEqual(receipt["execution_rows"], 2)
        row = rows[0]
        self.assertEqual(row[0], "FT-SSLS-001")
        self.assertEqual(row[1], "数智运营管理-个人工作台-智能助手-搜索历史")
        self.assertEqual(row[2], "1.1.1.1 搜索历史")
        self.assertEqual(row[3], case["title"])
        self.assertEqual(row[4], case["preconditions"])
        self.assertIn("点击搜索历史", row[5])
        self.assertEqual(row[6], case["expected"])
        self.assertEqual(row[7], "测试编制人甲")
        self.assertIn("曾失败，后续通过", row[9])
        self.assertIn("返回 0 条记录", row[8])
        self.assertIn("返回 2 条已保存记录", row[8])
        self.assertIsNone(row[10])
        self.assertEqual(book["编号映射"].cell(2, 2).value, "TC-UI-001")
        self.assertIn("attempt-1.json", book["执行明细"].cell(2, 6).value)
        self.assertIsNone(book.worksheets[0].cell(6, 14).hyperlink)
        self.assertIn("/synthetic/failure.png", row[13])
        self.assertIn("未随 Excel 嵌入", row[13])
        self.assertEqual(next(iter(book.worksheets[0].values)), tuple(task_workbook.HEADERS))
        self.assertEqual({str(value) for value in book.worksheets[0].merged_cells.ranges}, {"A2:L2", "A3:L3", "A4:L4", "A5:L5"})
        self.assertEqual([book.worksheets[0].cell(row, 1).fill.fgColor.rgb[-6:] for row in range(2, 6)], task_workbook.COLORS)
        for sheet in book:
            for cells in sheet:
                for cell in cells:
                    if cell.value is not None:
                        self.assertEqual(cell.font.name, "宋体")
        self.assertEqual(task_report_data.task_summary(snapshot([first, final]))["counts"]["Fail"], 1)

    def test_frozen_design_arrays_and_multiline_steps_render_without_changing_the_snapshot(self):
        case = {**definition(), "kind": "scenario", "expected": ["1. 个人工作台可访问。", "2. 展示已保存的两条历史记录。"]}
        item = {"definition": case, "result": None, "run_id": None, "evidence_valid": False}
        for steps in (case["steps"], "\n".join(case["steps"])):
            with self.subTest(steps=type(steps).__name__):
                case["steps"] = steps
                data = snapshot([item])
                original = copy.deepcopy(data)
                book, rows, _ = self.render(data)
                self.assertEqual(rows[0][6], "\n".join(case["expected"]))
                self.assertIn("点击搜索历史", rows[0][5])
                self.assertIn("点击搜索历史", reporting._definition_fields(case)["steps"])
                self.assertEqual(data, original)
                book.close()

    def test_cancel_receipts_do_not_invent_failures_or_erase_an_earlier_failure(self):
        case = definition()
        cancelled = observation(case, "Cancelled", "run-cancelled")
        cancelled['result']['detail'].update(business_status='Unverified', execution_status='cancelled')
        cancelled['result'].update(category='cancelled', reason='运行已取消；未完成检查不判定失败')
        cancelled['recording_result'] = {'status': 'Pass'}
        unrun = {'definition': definition('TC-UI-NOT-STARTED'), 'result': None, 'run_id': 'run-cancelled', 'evidence_valid': False}
        data = snapshot([cancelled, unrun])
        _, rows, _ = self.render(data)
        self.assertEqual([row[9] for row in rows], ['已取消', '未执行'])
        self.assertEqual(task_bugs.render(data, excel_link='report.xlsx')[1]['counts']['business'], 0)
        prior = observation(case, 'Fail', 'run-earlier-failure')
        cancelled['recording_result'] = {'status': 'Fail', 'actual': '原录制观察仍保留'}
        data = snapshot([prior, cancelled, unrun])
        _, rows, _ = self.render(data)
        self.assertEqual(rows[0][9], '失败')
        self.assertIn('run-cancelled：已取消', rows[0][8])
        self.assertNotIn('run-cancelled：失败', rows[0][8])
        self.assertIn('原录制观察仍保留', rows[0][8])
        self.assertEqual(task_bugs.render(data, excel_link='report.xlsx')[1]['counts']['business'], 1)

    def test_recording_failure_does_not_replace_each_replay_verdict_in_the_workbook(self):
        case = definition()
        recorded = {'status': 'Fail', 'actual': '最初录制只显示 0 条', 'expected': '应显示 2 条'}
        initial = {'definition': case, 'result': None, 'run_id': None, 'evidence_valid': False, 'recording_result': recorded}
        failed = {**observation(case, 'Fail', 'run-first'), 'recording_result': recorded}
        passed = {**observation(case, 'Pass', 'run-retest'), 'recording_result': recorded}
        data = snapshot([initial, failed, passed])
        book, rows, _ = self.render(data)
        self.assertEqual(rows[0][9], '失败（曾失败，后续通过）')
        self.assertIn('各轮结论：失败 → 失败 → 通过', rows[0][12])
        self.assertIn('run-retest：通过', rows[0][8])
        cells = list(book['用例与结果'].values)
        columns = {name: index for index, name in enumerate(cells[0])}
        replay = next(row for row in cells[1:] if row[columns['运行标识']] == 'run-retest')
        self.assertEqual((replay[columns['本次结果']], replay[columns['业务判断']]), ('通过', 'Pass'))
        self.assertIn('返回 2 条', replay[columns['实际结果']])
        self.assertEqual(task_report_data.task_summary(data)['counts']['Fail'], 1)
        bug_text, _ = task_bugs.render(data, excel_link='report.xlsx')
        self.assertIn('失败 → 失败 → 通过', bug_text)

    def test_missing_facts_do_not_turn_observations_into_oracles(self):
        case = {"id": "native:case-a", "title": "未提供设计事实", "kind": "native", "assertions": [{"actual": "observed-answer"}]}
        item = observation(case, valid=False)
        data = snapshot([item, {"definition": {"id": "TC-unrun", "title": "尚未执行"}, "result": None,
                               "recording_result": {"status": "Fail", "actual": "recorded-failure", "expected": "declared"}}])
        _, rows, receipt = self.render(data)
        self.assertEqual(receipt["cases"], 2)
        self.assertTrue(all(row[0].startswith("FT-WQR") for row in rows))
        self.assertIn("【待确认：有来源支持的预期结果】", rows[0][6])
        self.assertNotIn("observed-answer", rows[0][6])
        self.assertIn("待确认", rows[0][7])
        self.assertEqual(rows[0][9], "业务未验证")
        self.assertEqual(rows[1][9], "失败")
        self.assertIn("recorded-failure", rows[1][8])
        summary = task_report_data.task_summary(data)
        self.assertEqual(summary["counts"]["Unverified"], 1)
        self.assertEqual(summary["counts"]["Fail"], 1)
        self.assertIn("录制时已观察到断言失败", summary["issues"][1]["reason"])

    def test_multiline_input_and_wait_keep_step_expectation_mapping(self):
        case = definition()
        case["steps"] = ["打开个人工作台", "等待 2 秒", "输入第一行\n  第二行保留两个空格", "点击提交审批"]
        case["expected"] = "3. 输入内容保留两行\n  第二行仍含空格\n4. 提交结果为待审批。"
        book, rows, _ = self.render(snapshot([observation(case)]))
        self.assertIn("1. 打开个人工作台（等待 2 秒）", rows[0][5])
        self.assertIn("3. 点击提交审批", rows[0][5])
        self.assertNotIn("点击【提交】审批", rows[0][5])
        self.assertIn("2. 输入内容保留两行", rows[0][6])
        self.assertIn("3. 提交结果为待审批", rows[0][6])
        originals = [row[1] for row in book["测试数据"].iter_rows(min_row=2, values_only=True)]
        self.assertIn("输入第一行\n  第二行保留两个空格", originals)
        self.assertIn("输入内容保留两行\n  第二行仍含空格", originals)
        self.assertIn('"3": 2', book["编号映射"].cell(2, 8).value)
        self.assertEqual(task_workbook_data._button_names("点击搜索；点击提交按钮。"), "点击【搜索】；点击【提交】。")

    def test_machine_oracle_stays_in_technical_evidence_when_functional_expected_prose_is_missing(self):
        case = definition()
        case.pop("expected")
        case["extensions"]["automation"] = {"start_url": "http://synthetic.local", "actions": [], "assertions": [{"assertion": "text", "expected": "已发布"}]}
        book, rows, _ = self.render(snapshot([observation(case, "Fail")]))
        self.assertIn("待确认：有来源支持的预期结果", rows[0][6])
        self.assertNotIn("返回 0 条记录", rows[0][6])
        self.assertNotIn("已发布", rows[0][6])
        self.assertEqual(rows[0][9], "失败")
        frozen = "\n".join(str(row[1]) for row in book["来源与冻结定义"].iter_rows(min_row=2, values_only=True))
        self.assertIn("已发布", frozen)

    def test_definition_changes_get_distinct_mapping_and_unknown_stays_unknown(self):
        old = definition()
        new = copy.deepcopy(old)
        new["expected"] = "2. 展示已保存的三条历史记录。"
        data = snapshot([observation(old, "Fail"), observation(new, "unexpected_status", "run-b")])
        book, rows, receipt = self.render(data)
        self.assertEqual(receipt["cases"], 2)
        self.assertEqual([row[0] for row in rows], ["FT-SSLS-001", "FT-SSLS-002"])
        self.assertEqual(rows[1][9], "未知")
        self.assertNotEqual(book["编号映射"].cell(2, 3).value, book["编号映射"].cell(3, 3).value)
        self.assertEqual(task_report_data.task_summary(data)["counts"]["Unknown"], 1)

    def test_thousand_cases_and_deep_real_hierarchy_are_not_restricted(self):
        items = []
        for number in range(1001):
            case = definition(f"TC-{number}")
            case["extensions"]["test_case_document"]["business_path"] = [f"实际业务层级{depth}" for depth in range(1, 10)]
            items.append({"definition": case, "result": None})
        book, rows, receipt = self.render(snapshot(items))
        self.assertEqual(len(rows), 1001)
        self.assertEqual(rows[-1][0], "FT-SSLS-1001")
        self.assertEqual(receipt["validation"]["structure"]["functionItemCount"], 1)
        self.assertEqual(book.worksheets[0].cell(10, 1).comment.text, "autoskill:business-level=9")
        self.assertEqual(book.worksheets[0].cell(10, 1).fill.fgColor.rgb[-6:], "BFBFBF")

    def test_empty_blocked_scope_has_no_fabricated_case(self):
        book, rows, receipt = self.render(snapshot([]))
        self.assertFalse(rows)
        self.assertEqual(receipt["cases"], 0)
        self.assertEqual(receipt["validation"]["structure"]["rootCount"], 1)
        self.assertIn("尚未形成用例", book.worksheets[0].cell(2, 13).value)

    def test_large_procedures_and_literal_formula_text_keep_full_data(self):
        case = definition()
        case["title"] = "=SUM(1,2)"
        case["steps"] = [f"输入第 {number} 项已声明测试数据并检查原始字段关系" for number in range(2000)]
        case["expected"] = "2000. 最后一项符合独立判据。"
        book, rows, _ = self.render(snapshot([observation(case)]))
        self.assertIn("完整操作链", rows[0][5])
        self.assertEqual(book.worksheets[0].cell(6, 4).data_type, "s")
        restored = "".join(str(row[1]) for row in book["测试数据"].iter_rows(min_row=2, values_only=True) if row[0].endswith("/完整步骤"))
        self.assertIn("2000. 输入第 1999 项", restored)
        self.assertTrue(all(row.height < 409 for row in book["测试数据"].row_dimensions.values()))

    def test_long_visible_procedure_and_screenshot_basename_are_explicit_references(self):
        case = definition()
        case["steps"] = [f"操作第 {number} 个已声明控件" for number in range(40)]
        case["expected"] = "40. 全部操作符合独立需求。"
        item = observation(case)
        item["evidence_directory"] = "/synthetic/workspace/.local/runs/run-a"
        item["result"]["detail"]["screenshots"] = ["attempt-failure.png"]
        book, rows, _ = self.render(snapshot([item]))
        self.assertIn("完整操作链", rows[0][5])
        self.assertLess(book.worksheets[0].row_dimensions[6].height, 409)
        restored = "".join(row[1] for row in book["测试数据"].iter_rows(min_row=2, values_only=True) if row[0].endswith("/完整步骤"))
        self.assertEqual(restored, "\n".join(f"{number}. {step}" for number, step in enumerate(case["steps"], 1)))
        self.assertTrue(all(row.height < 409 for row in book["测试数据"].row_dimensions.values()))
        self.assertIn("/synthetic/workspace/.local/runs/run-a/attempt-failure.png", rows[0][13])
        self.assertIsNone(book.worksheets[0].cell(6, 14).hyperlink)

    def test_real_group_name_starting_with_ft_is_not_a_case_identifier(self):
        case = definition()
        case["extensions"]["test_case_document"]["business_path"] = ["FT-业务系统", "原始功能"]
        _, rows, receipt = self.render(snapshot([observation(case)]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "FT-业务系统-原始功能")
        self.assertEqual(receipt["validation"]["structure"]["businessGroupCount"], 2)


if __name__ == "__main__":
    unittest.main()
