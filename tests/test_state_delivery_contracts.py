"""Small deterministic contracts for equivalent prose and frozen task delivery."""
import copy
import datetime
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

import test_task_workbook as fixture

from autoskill import case_document, plan_execution, reporting, task_report_data, task_workbook_data, workflow_definition


class StateDeliveryContractTests(unittest.TestCase):
    def test_prose_representation_preserves_business_text_and_technical_types(self):
        document = {"description": "验证已保存记录", "preconditions": "已有记录", "steps": ["1. 查询记录。"],
                    "expected": ["1. 返回数量 2，且开关为 true。"], "source": {"kind": "requirement", "text": "保留原值"}}
        case = {"id": "API-1", "kind": "api", "assertions": {"json_equals": {"/count": 2, "/enabled": True}}, "extensions": {}}
        case_document.apply(case, document)
        self.assertEqual(case["expected"], document["expected"][0])
        self.assertEqual(case["extensions"]["test_case_document"], document)
        original = copy.deepcopy(case)
        altered = copy.deepcopy(case)
        altered["extensions"]["test_case_document"]["steps"] = document["steps"][0]
        altered["extensions"]["test_case_document"]["expected"] = document["expected"][0]
        self.assertTrue(workflow_definition._binding_only(original, altered))
        oracles = [{"id": "o1", "kind": "api", "checks": copy.deepcopy(case["assertions"]), "functional_expected": document["expected"][0]}]
        workflow_definition._functional_mapping(case, None, oracles, [{"id": "s1"}], {"id": "w1"})
        self.assertEqual(case["extensions"]["functional_design"]["state"], "compiled_from_workflow_oracles")
        for text in ("1. 返回数量 3，且开关为 true。", "1. 返回数量 2，且开关为 true。 ", "2. 返回数量 2，且开关为 true。"):
            altered["extensions"]["test_case_document"]["expected"] = text
            self.assertFalse(workflow_definition._binding_only(original, altered))
        altered = copy.deepcopy(original)
        altered["assertions"]["json_equals"]["/count"] = "2"
        self.assertFalse(workflow_definition._binding_only(original, altered))
        self.assertEqual(document, original["extensions"]["test_case_document"])

    def test_same_snapshot_workbook_bytes_do_not_depend_on_render_clock(self):
        data = fixture.snapshot([fixture.observation(fixture.definition())])
        before = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / f"report-{year}.xlsx" for year in (2025, 2026)]
            for year, file in zip((2025, 2026), files):
                clock = SimpleNamespace(datetime=SimpleNamespace(now=lambda tz, year=year: datetime.datetime(year, 1, 2, tzinfo=tz)),
                                        timezone=datetime.timezone)
                with patch("openpyxl.writer.excel.datetime", clock), patch("zipfile.time.localtime", return_value=(year, 1, 2, 3, 4, 6, 0, 0, -1)):
                    receipt = reporting.write_task(data, file)
                self.assertTrue(receipt["validation"]["workbook_readable"])
            self.assertEqual(files[0].read_bytes(), files[1].read_bytes())
            stamp = files[0].stat().st_mtime_ns
            reporting.write_task(data, files[0])
            self.assertEqual(files[0].stat().st_mtime_ns, stamp)
            changed = copy.deepcopy(data)
            changed["title"] = "已修正的任务标题"
            original = hashlib.sha256(files[0].read_bytes()).hexdigest()
            reporting.write_task(changed, files[0])
            self.assertNotEqual(hashlib.sha256(files[0].read_bytes()).hexdigest(), original)
        self.assertEqual(data, before)

    def test_legacy_container_timestamps_are_preserved_after_content_verification(self):
        data = fixture.snapshot([fixture.observation(fixture.definition())])
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "old.xlsx"
            reporting.write_task(data, file)
            with ZipFile(file) as archive:
                members = [(member, archive.read(member)) for member in archive.infolist()]
            with ZipFile(file, "w") as archive:
                for member, original_content in members:
                    content = original_content
                    member.date_time = (2021, 2, 3, 4, 5, 6)
                    if member.filename == "docProps/core.xml":
                        root = ElementTree.fromstring(content)
                        ElementTree.SubElement(root, "{http://purl.org/dc/terms/}created").text = "2021-02-03T04:05:06Z"
                        content = ElementTree.tostring(root)
                    archive.writestr(member, content)
            old_bytes, old_stamp = file.read_bytes(), file.stat().st_mtime_ns
            reporting.write_task(data, file)
            self.assertEqual(file.read_bytes(), old_bytes)
            self.assertEqual(file.stat().st_mtime_ns, old_stamp)

    def test_blocker_and_latest_success_keep_execution_and_history_separate(self):
        case = fixture.definition()
        blocked = plan_execution.unexecuted_item({"status": "阻塞", "note": "账号未提供"}, case)
        self.assertIsNone(blocked["run_id"])
        self.assertFalse(blocked["evidence_valid"])
        self.assertIn("没有本行执行回执", blocked["note"])
        self.assertEqual(task_report_data.case_verdict(blocked), "NotRun")
        manual_pass = plan_execution.unexecuted_item({"status": "通过"}, case)
        self.assertEqual(task_report_data.case_verdict(manual_pass), "NotRun")
        data = fixture.snapshot([fixture.observation(case, "Fail", "failed"), fixture.observation(case, "Pass", "passed")])
        group = task_report_data.logical_cases(data)[0]
        self.assertEqual((group["status"], group["final_status"], group["run_ids"]), ("Fail", "Pass", ["failed", "passed"]))
        rows, _ = task_workbook_data.rows(data)
        self.assertEqual(rows[0]["conclusion"], "失败")
        self.assertIn("曾失败，后续通过", rows[0]["remarks"])


if __name__ == "__main__":
    unittest.main()
