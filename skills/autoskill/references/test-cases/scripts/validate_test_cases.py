#!/usr/bin/env python3
"""Read-only validation of layered functional test-case workbooks."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils.exceptions import InvalidFileException

HEADERS = ["用例标识", "用例名称", "功能需求追踪", "测试说明", "前置条件",
           "测试步骤", "预期结果", "用例编制人", "实际结果", "测试结论",
           "测试执行人", "验证环境", "备注", "截图"]
LEVEL_COLORS = {"FFD966": 1, "A9D18E": 2, "F4B183": 3, "BFBFBF": 4}
# Preserve zero padding for small lists without imposing a 999-case ceiling.
ID_PATTERN = re.compile(r"^(FT-[A-Z0-9]+)-(\d{3,})$")
NUMBERED = re.compile(r"^(\d+)\. (\S.*)$")
GROUP_LEVEL_MARKER = "autoskill:business-level="


def rgb(cell):
    color = cell.fill.fgColor
    return str(color.rgb)[-6:].upper() if color.type == "rgb" else ""


def strip_chapter(name):
    return re.sub(r"^\s*(?:第[一二三四五六七八九十百\d]+[章节]\s*|\d+(?:\.\d+)*(?:[、.)）]\s*|\s+))", "", name).strip()


def excluded_rows(spec):
    rows = set()
    for part in spec.split(","):
        if not part.strip():
            continue
        limits = [int(value) for value in part.strip().split(":")]
        if len(limits) > 2 or min(limits) < 1 or limits[-1] < limits[0]:
            raise ValueError("排除行须为正整数或起止范围，如 2:6,10")
        rows.update(range(limits[0], limits[-1] + 1))
    return rows


def numbered_lines(value, label, case_id, errors):
    numbers = []
    for line in str(value or "").splitlines():
        if not line.strip():
            continue
        match = NUMBERED.fullmatch(line)
        if not match:
            errors.append(f"{case_id} {label}编号或分行不规范: {line[:65]}")
        else:
            numbers.append(int(match.group(1)))
    if not numbers:
        errors.append(f"{case_id} {label}缺少编号内容")
    if len(numbers) != len(set(numbers)):
        errors.append(f"{case_id} {label}编号重复")
    return numbers


def _check_steps(values, case_id, errors, warnings):
    steps = numbered_lines(values[5], "步骤", case_id, errors)
    expected = numbered_lines(values[6], "预期", case_id, errors)
    if steps != list(range(1, len(steps) + 1)):
        errors.append(f"{case_id} 步骤必须从 1 连续编号")
    if any(number not in steps for number in expected):
        errors.append(f"{case_id} 预期编号在步骤中不存在: {expected}")
    if expected != sorted(expected):
        errors.append(f"{case_id} 预期编号顺序错误")
    if re.search(r"(?m)^\d+\.\s*等待", str(values[5] or "")):
        warnings.append(f"{case_id} 含单列被动等待，请核对必要时点并改成检查动作")
    if re.search(r"点击(?:搜索|新增|提交)(?:按钮)?(?=$|[，。；、\s])", str(values[5] or "")):
        errors.append(f"{case_id} 已知按钮名称须使用【界面原文】")


def _group_level(cell, row, errors):
    markers = [line for line in (cell.comment.text if cell.comment else "").splitlines() if line.startswith(GROUP_LEVEL_MARKER)]
    if not markers:
        return LEVEL_COLORS.get(rgb(cell)), False
    raw = markers[0].removeprefix(GROUP_LEVEL_MARKER)
    if len(markers) != 1 or not raw.isascii() or not raw.isdecimal() or int(raw) < 1:
        errors.append(f"第 {row} 行的业务层级注记无效或重复")
        return None, True
    level = int(raw)
    expected_color = next(color for color, depth in LEVEL_COLORS.items() if depth == min(level, 4))
    if rgb(cell) != expected_color:
        errors.append(f"第 {row} 行的业务层级注记与分组颜色不一致")
        return None, True
    return level, True


class _SheetValidation:
    def __init__(self, sheet, font, exclude):
        self.sheet, self.font, self.exclude = sheet, font, exclude
        self.errors, self.warnings, self.font_errors = [], [], []
        self.merges = list(sheet.merged_cells.ranges)
        self.stack, self.ids, self.roots = [], [], []
        self.groups, self.items = {}, {}
        self.item_prefixes, self.prefix_paths = defaultdict(set), defaultdict(set)
        self.sequences, self.depths = defaultdict(list), Counter()

    def check_header(self):
        headers = [self.sheet.cell(1, column).value for column in range(1, 15)]
        if headers != HEADERS:
            self.errors.append(f"表头应为 14 列标准字段，实际: {headers}")
        if rgb(self.sheet.cell(1, 1)) != "BFBFBF":
            self.errors.append("表头填充色应为 #BFBFBF")
        if any(merged.min_row <= 1 <= merged.max_row for merged in self.merges):
            self.errors.append("表头不允许合并")

    def check_row(self, row):
        if row in self.exclude:
            return
        cells = [self.sheet.cell(row, column) for column in range(1, max(14, self.sheet.max_column) + 1)]
        self.font_errors.extend(cell.coordinate for cell in cells if not isinstance(cell, MergedCell)
                                and cell.value not in (None, "") and cell.font.name != self.font)
        if row == 1:
            return
        values = [cell.value for cell in cells]
        first = str(values[0] or "").strip()
        if not first:
            if any(value not in (None, "") for value in values):
                self.errors.append(f"第 {row} 行有内容但缺少用例标识或分组名称")
            return
        row_merges = [merged for merged in self.merges if merged.min_row <= row <= merged.max_row]
        if first.startswith("FT-"):
            self.check_case(first, cells, values, row_merges)
        else:
            self.check_group(row, first, cells, row_merges)

    def check_case(self, case_id, cells, values, row_merges):
        self.ids.append(case_id)
        if row_merges:
            self.errors.append(f"用例行被合并: {case_id}")
        missing = [chr(65 + index) for index, value in enumerate(values[:8]) if value is None or not str(value).strip()]
        if missing:
            self.errors.append(f'{case_id} 必填列为空: {",".join(missing)}')
        # Execution and attachment columns may retain colored result marks.
        self.errors.extend(f"{case_id} 内容区非白底: {cell.coordinate}" for cell in cells[:8]
                           if cell.fill.fill_type == "solid" and rgb(cell) not in ("FFFFFF", "000000"))
        match = ID_PATTERN.fullmatch(case_id)
        self.check_case_path(case_id, values, match)
        if not match:
            self.errors.append(f"用例标识格式错误: {case_id}")
        else:
            self.sequences[match.group(1)].append(int(match.group(2)))
        _check_steps(values, case_id, self.errors, self.warnings)

    def check_case_path(self, case_id, values, match):
        if not self.stack:
            self.errors.append(f"{case_id} 不在业务分组下面")
            return
        path = tuple(name for _, name in self.stack)
        self.items[path] = self.items.get(path, 0) + 1
        self.groups[path]["caseCount"] += 1
        self.depths[len(path)] += 1
        expected_name = "-".join(strip_chapter(name) for name in path)
        if values[1] != expected_name:
            self.errors.append(f"{case_id} 用例名称应为 {expected_name}")
        if values[2] != path[-1]:
            self.errors.append(f"{case_id} 功能需求追踪应为 {path[-1]}")
        if match:
            self.item_prefixes[path].add(match.group(1))
            self.prefix_paths[match.group(1)].add(path)

    def check_group(self, row, first, cells, row_merges):
        level, marked = _group_level(cells[0], row, self.errors)
        full_merge = any(merged.min_row == row == merged.max_row and merged.min_col == 1 and merged.max_col == 12 for merged in row_merges)
        if not level or not full_merge:
            self.errors.append(f"无法识别的分组行 {row}: {first}；核对配色与 A:L 合并范围")
            return
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        if marked and level != (self.stack[-1][0] + 1 if self.stack else 1):
            self.errors.append(f"第 {row} 行的业务层级注记缺少上级分组")
        if level == 1:
            self.roots.append(first)
        elif not self.stack:
            self.errors.append(f"分组缺少一级归属: {first}")
        self.stack.append((level, first))
        path = tuple(name for _, name in self.stack)
        if path in self.groups:
            self.errors.append(f'分组路径重复: {"-".join(path)}')
        self.groups[path] = {"row": row, "level": level, "caseCount": 0}

    def check_groups(self, expected_cases):
        if len(self.roots) != 1:
            self.errors.append(f"一级分组数量应为 1，实际 {len(self.roots)}")
        empty_scope = expected_cases == 0 and not self.ids and len(self.roots) == 1 and len(self.groups) == 1
        if empty_scope:
            self.warnings.append("本任务尚未形成用例；空范围报告不表示测试覆盖完成或业务通过。")
        for path, info in self.groups.items():
            children = any(len(other) > len(path) and other[:len(path)] == path for other in self.groups)
            if not children and info["caseCount"] == 0 and not empty_scope:
                self.errors.append(f'末级功能项没有用例: {"-".join(path)}')
            if children and info["caseCount"]:
                self.errors.append(f'父分组直接混入用例明细: {"-".join(path)}')

    def check_identities(self):
        duplicates = [case_id for case_id, count in Counter(self.ids).items() if count > 1]
        if duplicates:
            self.errors.append(f"用例标识重复: {duplicates}")
        for prefix, numbers in self.sequences.items():
            if numbers != list(range(1, len(numbers) + 1)):
                self.errors.append(f"编号不连续或未从 001 开始: {prefix}")
        for path, prefixes in self.item_prefixes.items():
            if len(prefixes) != 1:
                self.errors.append(f'同一功能项存在多个前缀: {"-".join(path)}')
        for prefix, item_paths in self.prefix_paths.items():
            if len(item_paths) > 1:
                self.errors.append(f"不同功能项共用前缀: {prefix}")

    def finish(self, expected_items, expected_cases):
        self.check_groups(expected_cases)
        self.check_identities()
        if expected_items is not None and len(self.items) != expected_items:
            self.errors.append(f"功能项数量: 实际 {len(self.items)}，预期 {expected_items}")
        if expected_cases is not None and len(self.ids) != expected_cases:
            self.errors.append(f"用例数量: 实际 {len(self.ids)}，预期 {expected_cases}")
        if self.font_errors:
            self.errors.append(f"字体应为 {self.font}，不匹配 {len(self.font_errors)} 格，示例: {self.font_errors[:12]}")
        return {
            "ok": not self.errors, "sheet": self.sheet.title, "rootCount": len(self.roots),
            "businessGroupCount": len(self.groups), "caseGroupDepths": dict(self.depths),
            "functionItemCount": len(self.items),
            "functionItems": [{"path": list(path), "name": path[-1], "caseCount": count} for path, count in self.items.items()],
            "caseCount": len(self.ids), "uniqueIdCount": len(set(self.ids)),
            "excludedRows": sorted(self.exclude), "errors": self.errors, "warnings": self.warnings,
        }


def validate_sheet(sheet, *, expected_items=None, expected_cases=None, font="宋体", exclude=None):
    validation = _SheetValidation(sheet, font, exclude or set())
    validation.check_header()
    for row in range(1, sheet.max_row + 1):
        validation.check_row(row)
    return validation.finish(expected_items, expected_cases)


def main():
    parser = argparse.ArgumentParser(description="校验业务分组＋用例明细的 14 列测试用例工作簿")
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--sheet", default="功能测试用例")
    parser.add_argument("--expected-items", type=int)
    parser.add_argument("--expected-cases", type=int)
    parser.add_argument("--font", default="宋体")
    parser.add_argument("--exclude-rows", default="", help="模板示例所在行，如 2:6,10")
    args = parser.parse_args()
    try:
        workbook = load_workbook(args.workbook, read_only=False, data_only=False)
        try:
            report = validate_sheet(workbook[args.sheet], expected_items=args.expected_items,
                                    expected_cases=args.expected_cases, font=args.font,
                                    exclude=excluded_rows(args.exclude_rows))
        finally:
            workbook.close()
        report["workbook"] = str(args.workbook.resolve())
    except (OSError, ValueError, KeyError, BadZipFile, InvalidFileException) as exc:
        report = {"ok": False, "errors": [str(exc)]}
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
