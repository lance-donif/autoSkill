"""Validate the XLSX bytes downloaded from the static Pages case library."""
import hashlib
import json
import sys
import unittest
from collections import Counter
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.cell import range_to_tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dashboard_stability as preview

from autoskill import task_workbook

VERDICTS = {"Pass": "通过", "Fail": "失败", "NotRun": "未执行"}


class PreviewCaseWorkbookTests(unittest.TestCase):
    setUpClass = classmethod(preview.DashboardStabilityTests.setUpClass.__func__)
    setUp = preview.fixtures.DashboardReviewTests.setUp
    handle_request = preview.DashboardStabilityTests.handle_request
    open_static_preview = preview.DashboardStabilityTests.open_static_preview

    def open_project(self, project_id):
        sample = self.open_static_preview("#/cases?projects=" + project_id + "&range=all")
        self.expected = {case["case_id"]: case["current_status"] or "NotRun"
                         for case in sample["/cases"]["items"] if case["project_id"] == project_id}
        return sample

    def assert_overview(self, book, expected):
        self.assertEqual(book.sheetnames[:2], ["概览", "功能测试用例"])
        overview = book["概览"]
        self.assertEqual(book.active, overview)
        counts = Counter(VERDICTS[status] for status in expected.values())
        required = {label: counts[label] for label in VERDICTS.values()}
        if not expected and not overview._charts:
            values = {row[0]: row[1] for row in overview.values if row[0] in VERDICTS.values()}
            self.assertEqual(values, required)
            return

        def cell_values(formula):
            sheet, bounds = range_to_tuple(formula)
            self.assertEqual(sheet, overview.title)
            left, top, right, bottom = bounds
            return [cell.value for row in overview.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right)
                    for cell in row]

        for chart in overview._charts:
            for series in chart.series:
                if not series.cat or not series.val or not series.val.numRef:
                    continue
                labels = series.cat.strRef or series.cat.numRef
                if labels:
                    data = dict(zip(cell_values(labels.f), cell_values(series.val.numRef.f), strict=True))
                    if all(data.get(label) == count for label, count in required.items()):
                        return
        self.fail(f"概览图表未引用此导出范围的用例结论和数量 {required}")

    def export_rows(self, expected, link="导出测试用例"):
        with self.page.expect_download() as event:
            self.page.get_by_role("link", name=link, exact=True).click()
        download = event.value
        self.assertTrue(download.suggested_filename.endswith(".xlsx"))
        book = load_workbook(BytesIO(download.path().read_bytes()))
        self.addCleanup(book.close)
        self.assert_overview(book, expected)
        sheet = book["功能测试用例"]
        self.assertEqual((sheet.title, sheet.max_column), ("功能测试用例", 14))
        validation = task_workbook._validator().validate_sheet(sheet, expected_cases=len(expected))
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertTrue(sheet.freeze_panes)
        if not expected:
            self.assertEqual(validation["businessGroupCount"], 1)
            self.assertTrue(sheet["M2"].value)
            return {}

        mapped = list(book["编号映射"].iter_rows(min_row=2, values_only=True))
        mapping = {ft: original for ft, original, *_ in mapped}
        self.assertEqual(len(mapped), len(expected))
        rows = {mapping[row[0].value]: row for row in sheet.iter_rows(min_row=2) if str(row[0].value).startswith("FT-")}
        self.assertEqual(set(rows), set(expected))
        for original, row in rows.items():
            with self.subTest(case=original):
                status = expected[original]
                self.assertEqual(row[9].value, VERDICTS[status])
                self.assertEqual(row[9].fill.fgColor.rgb[-6:], task_workbook.FILLS[status])
                self.assertTrue(row[5].alignment.wrap_text and row[6].alignment.wrap_text)
        return rows

    def test_project_preserves_runoob_steps_expectations_and_results(self):
        sample = self.open_project("runoob")
        rows = self.export_rows(self.expected)
        source = sample["/case-definitions/runoob/TC-UI-004"]["case"]
        case = rows[source["id"]]
        for step in source["steps"]:
            self.assertIn(step, case[5].value)
        for line in source["expected"].splitlines():
            self.assertIn(line.split(". ", 1)[1], case[6].value)
        self.assertIn("待确认", case[6].value)

    def test_xml_characters_round_trip_in_exported_case_and_source(self):
        self.open_project("stock-room")
        self.page.get_by_role("link", name="导出测试用例", exact=True).wait_for()
        case_id = "ST-API-001"
        text = '中文😀&<>"\t换行\n回车\r后\x00\x1f\ufffe|\uffff|\ud800|\udc00|字面\\ufffe'
        displayed = '中文😀&<>"\t换行\n回车\r后\\ufffe|\\uffff|\\ud800|\\udc00|字面\\ufffe'
        status = self.page.evaluate("""async (encoded) => {
          const record = await (await fetch('/api/case-definitions/stock-room/ST-API-001')).json();
          const definition = record.case;
          definition.extensions.test_case_document = { description: JSON.parse(encoded) };
          const response = await fetch('/api/projects/stock-room/cases/ST-API-001', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ case: definition, expected_content_revision: record.content_revision })
          });
          return response.status;
        }""", json.dumps(text))
        self.assertEqual(status, 200)

        rows = self.export_rows({**self.expected, case_id: "NotRun"})
        cell = rows[case_id][3]
        self.assertEqual(cell.value, displayed)
        book = cell.parent.parent
        source = "".join(row[4] for row in book["来源与冻结定义"].values if row[1] == case_id)
        frozen = json.loads(source)
        self.assertEqual(frozen["extensions"]["test_case_document"]["description"], text)
        canonical = json.dumps(frozen, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace")
        mapped = next(row for row in book["编号映射"].values if row[1] == case_id)
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), mapped[2])

    def test_folder_keeps_its_cases_and_descendants_and_exports_an_empty_scope(self):
        self.open_project("stock-room")
        folders = self.page.get_by_role("navigation", name="示例库存中心文件夹")
        folders.get_by_role("button", name="库存管理，1 条用例", exact=True).click()
        ids = ["ST-UI-001", "ST-API-001", "ST-API-002"]
        rows = self.export_rows({key: self.expected[key] for key in ids}, "导出文件夹 Excel")
        self.assertEqual(rows["ST-UI-001"][1].value, "示例库存中心-库存管理-【本级用例】")
        self.assertEqual(rows["ST-API-001"][1].value, "示例库存中心-库存管理-可售库存")
        folders.get_by_role("button", name="待补用例，0 条用例", exact=True).click()
        self.export_rows({}, "导出文件夹 Excel")

    def test_editor_exports_current_design_and_keeps_other_cases_results(self):
        sample = self.open_project("stock-room")
        before = self.export_rows(self.expected)
        identity = "ST-API-001"
        title = "读取可售库存"
        self.page.get_by_role("button", name=title, exact=True).click()
        self.page.get_by_role("dialog", name=title).get_by_role("button", name="编辑用例").click()
        editor = self.page.get_by_role("dialog", name="编辑用例")
        fields = {"功能描述": "核对查询返回的可售数量", "前置条件": "库存样例包含两件可售商品",
                  "功能测试步骤": "打开样例库存\n查询可售商品数量", "预期结果": "2. 可售数量显示为 2 件"}
        for label, value in fields.items():
            editor.get_by_label(label, exact=True).fill(value)
        editor.get_by_text("基本信息与测试数据", exact=True).click()
        editor.get_by_label("业务设计作者", exact=True).fill("样例编制人")
        editor.get_by_label("业务设计备注", exact=True).fill("保留原始盘点单位：件")
        editor.get_by_label("显示编号前缀", exact=True).fill("KCSL")
        editor.get_by_label("启用业务层级", exact=True).check()
        path = ["示例库存中心", "仓库", "货架", "盘点", "库存数量"]
        for index, part in enumerate(path):
            editor.get_by_role("button", name="添加数组项：业务层级", exact=True).click()
            editor.get_by_label(f"业务层级[{index}]", exact=True).fill(part)
        editor.get_by_role("button", name="保存修改", exact=True).click()
        self.page.get_by_role("dialog", name=title).get_by_role("button", name="关闭", exact=True).last.click()

        after = self.export_rows({**self.expected, identity: "NotRun"})
        row = after[identity]
        self.assertEqual([cell.value for cell in row[:8]], ["FT-KCSL-001", "-".join(path), path[-1],
            fields["功能描述"], fields["前置条件"], "1. 打开样例库存\n2. 查询可售商品数量", fields["预期结果"], "样例编制人"])
        self.assertIn("保留原始盘点单位：件", row[12].value)
        for case in sample["/cases"]["items"]:
            key = case["case_id"]
            if case["project_id"] != "stock-room" or key == identity or not case["current"]:
                continue
            run_id = case["state"]["scope"]["run_id"]
            observation = next(row for row in sample["/runs/" + run_id]["results"] if row["case_id"] == key)
            for export in (before, after):
                self.assertIn(run_id, export[key][8].value, key)
                if observation["actual"]:
                    self.assertIn(observation["actual"], export[key][8].value, key)
