"""Regression checks for the bundled user case-writing rules and offline validator."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/autoskill/references/test-cases"
SCRIPT = SKILL / "scripts/validate_test_cases.py"
SPEC = importlib.util.spec_from_file_location("case_skill_validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def workbook(depth=4, count=1, marked=False):
    book = Workbook()
    sheet = book.active
    sheet.title = "功能测试用例"
    sheet.append(validator.HEADERS)
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="BFBFBF")
    names = ["1 系统", "2 模块", "3 子模块", "4 功能项", *[f"{number} 深层功能" for number in range(5, depth + 1)]][:depth]
    colors = list(validator.LEVEL_COLORS)
    for level, name in enumerate(names, 1):
        row = sheet.max_row + 1
        sheet.cell(row, 1, name)
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=12)
        sheet.cell(row, 1).fill = PatternFill("solid", fgColor=colors[min(level, 4) - 1])
        if marked:
            sheet.cell(row, 1).comment = Comment(f"原业务批注\n{validator.GROUP_LEVEL_MARKER}{level}", "fixture")
    for number in range(1, count + 1):
        sheet.append([f"FT-GNX-{number:03}", "-".join(validator.strip_chapter(name) for name in names), names[-1],
                      "验证来源指定的功能", "【待确认：测试账号与必要条件】", "1. 进入已知页面\n2. 点击【搜索】\n3. 查看结果",
                      "2. 【待确认：返回内容的独立依据】\n3. 【待确认：业务状态的独立依据】", "【待确认：用例编制人】",
                      "", "", "", "", "原备注", ""])
    for row in sheet:
        for cell in row:
            cell.font = Font(name="宋体")
    return book, sheet


class CaseSkillValidatorTests(unittest.TestCase):
    def test_original_archive_and_unchanged_host_assets_match_provenance(self):
        source = json.loads((SKILL / "source.json").read_text(encoding="utf-8"))
        archive = SKILL / source["archive"]["path"]
        self.assertEqual(archive.stat().st_size, source["archive"]["size"])
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), source["archive"]["sha256"])
        self.assertEqual(len(source["source_files"]), 5)
        for item in source["source_files"]:
            if item["path"].startswith(("assets/", "agents/")):
                self.assertEqual(hashlib.sha256((SKILL / item["path"]).read_bytes()).hexdigest(), item["sha256"])
        for item in source["adapted_files"]:
            data = (SKILL / item["path"]).read_bytes()
            self.assertEqual(len(data), item["size"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])

    def test_original_four_level_rules_keep_blank_result_marks_comments_and_extensions(self):
        book, sheet = workbook()
        row = sheet.max_row
        sheet.cell(row, 10).fill = PatternFill("solid", fgColor="FF0000")
        sheet.cell(row, 14).comment = Comment("截图原批注", "fixture")
        sheet.cell(row, 14).hyperlink = "https://example.invalid/evidence/retained"
        sheet.cell(1, 15, "原执行日期").font = Font(name="宋体")
        sheet.cell(row, 15, "2026-11-13").font = Font(name="宋体")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "已有记录.xlsx"
            book.save(path)
            book.close()
            check = load_workbook(path)
            try:
                report = validator.validate_sheet(check.active, expected_items=1, expected_cases=1)
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["caseGroupDepths"], {4: 1})
                self.assertIsNone(check.active.cell(row, 10).value)
                self.assertEqual(validator.rgb(check.active.cell(row, 10)), "FF0000")
                self.assertEqual(check.active.cell(row, 14).comment.text, "截图原批注")
                self.assertEqual(check.active.cell(row, 14).hyperlink.target, "https://example.invalid/evidence/retained")
                self.assertEqual(check.active.cell(row, 15).value, "2026-11-13")
            finally:
                check.close()

    def test_one_real_function_can_have_more_than_999_cases(self):
        book, sheet = workbook(count=1001)
        try:
            report = validator.validate_sheet(sheet, expected_items=1, expected_cases=1001)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["uniqueIdCount"], 1001)
            self.assertEqual(sheet.cell(sheet.max_row, 1).value, "FT-GNX-1001")
        finally:
            book.close()

    def test_nine_real_levels_round_trip_without_outline_limits_or_comment_loss(self):
        book, sheet = workbook(depth=9, marked=True)
        path_before = sheet.cell(sheet.max_row, 2).value
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "深层业务.xlsx"
            book.save(path)
            book.close()
            check = load_workbook(path)
            try:
                report = validator.validate_sheet(check.active, expected_items=1, expected_cases=1)
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["caseGroupDepths"], {9: 1})
                self.assertEqual(check.active.cell(check.active.max_row, 2).value, path_before)
                self.assertEqual(len(report["functionItems"][0]["path"]), 9)
                self.assertEqual(check.active.cell(10, 1).comment.text, "原业务批注\nautoskill:business-level=9")
                check.active.cell(10, 1).comment.text += "\nautoskill:business-level=8"
                self.assertFalse(validator.validate_sheet(check.active)["ok"])
            finally:
                check.close()

    def test_only_explicit_zero_case_scope_accepts_an_empty_root(self):
        book, sheet = workbook(depth=1, count=0)
        try:
            report = validator.validate_sheet(sheet, expected_items=0, expected_cases=0)
            self.assertTrue(report["ok"], report)
            self.assertIn("不表示", report["warnings"][0])
            self.assertFalse(validator.validate_sheet(sheet)["ok"])
            self.assertFalse(validator.validate_sheet(sheet, expected_cases=1)["ok"])
        finally:
            book.close()
        book, sheet = workbook(depth=2, count=0)
        try:
            self.assertFalse(validator.validate_sheet(sheet, expected_cases=0)["ok"])
        finally:
            book.close()

    def test_ft_prefixed_business_group_requires_both_marker_and_exact_merge(self):
        for marked, merged in ((True, True), (False, True), (True, False)):
            with self.subTest(marked=marked, merged=merged):
                book, sheet = workbook(depth=1, marked=marked)
                try:
                    name = "FT-真实业务系统"
                    sheet.cell(2, 1, name)
                    sheet.cell(3, 2, name)
                    sheet.cell(3, 3, name)
                    if not merged:
                        sheet.unmerge_cells("A2:L2")
                        sheet.merge_cells("A2:K2")
                    report = validator.validate_sheet(sheet, expected_items=1, expected_cases=1)
                    self.assertEqual(report["ok"], marked and merged, report)
                    self.assertEqual(sheet.cell(2, 1).value, name)
                    if report["ok"]:
                        self.assertEqual(report["functionItems"][0]["path"], [name])
                        self.assertEqual(report["uniqueIdCount"], 1)
                finally:
                    book.close()

    def test_identity_path_and_step_errors_are_not_weakened(self):
        book, sheet = workbook(count=2)
        try:
            row = sheet.max_row
            sheet.cell(row, 1, "FT-GNX-001")
            sheet.cell(row, 2, "错误路径")
            sheet.cell(row, 3, "错误功能项")
            sheet.cell(row, 7, "4. 不存在的步骤")
            report = validator.validate_sheet(sheet)
            self.assertFalse(report["ok"])
            for fragment in ("用例标识重复", "编号不连续", "用例名称应为", "功能需求追踪应为", "预期编号在步骤中不存在"):
                self.assertTrue(any(fragment in error for error in report["errors"]), report)
        finally:
            book.close()

    def test_same_pinyin_prefix_cannot_merge_distinct_function_paths(self):
        book, sheet = workbook()
        try:
            original = [cell.value for cell in sheet[sheet.max_row]]
            group_row = sheet.max_row + 1
            sheet.cell(group_row, 1, "4 另一功能项")
            sheet.cell(group_row, 1).fill = PatternFill("solid", fgColor="BFBFBF")
            sheet.cell(group_row, 1).font = Font(name="宋体")
            sheet.merge_cells(start_row=group_row, start_column=1, end_row=group_row, end_column=12)
            original[:3] = ["FT-GNX-002", "系统-模块-子模块-另一功能项", "4 另一功能项"]
            sheet.append(original)
            for cell in sheet[sheet.max_row]:
                cell.font = Font(name="宋体")
            report = validator.validate_sheet(sheet)
            self.assertTrue(any("不同功能项共用前缀" in error for error in report["errors"]), report)
        finally:
            book.close()

    def test_button_prefixes_do_not_rewrite_longer_business_names(self):
        book, sheet = workbook()
        try:
            row = sheet.max_row
            for action in ("点击搜索历史", "点击提交审批"):
                sheet.cell(row, 6, f"1. 进入已知页面\n2. {action}\n3. 查看结果")
                report = validator.validate_sheet(sheet)
                self.assertTrue(report["ok"], report)
                self.assertIn(action, sheet.cell(row, 6).value)
            sheet.cell(row, 6, "1. 进入已知页面\n2. 点击搜索\n3. 查看结果")
            self.assertTrue(any("已知按钮名称" in error for error in validator.validate_sheet(sheet)["errors"]))
        finally:
            book.close()

    def test_cli_reads_saved_workbook_and_reports_corrupt_input_as_json(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "用例.xlsx"
            book, _sheet = workbook(depth=2)
            book.save(file)
            book.close()
            command = [sys.executable, "-B", str(SCRIPT), str(file), "--expected-cases", "1"]
            good = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertTrue(json.loads(good.stdout)["ok"])
            file.write_bytes(b"not an xlsx archive")
            bad = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
            self.assertEqual(bad.returncode, 1, bad.stderr)
            self.assertFalse(json.loads(bad.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
