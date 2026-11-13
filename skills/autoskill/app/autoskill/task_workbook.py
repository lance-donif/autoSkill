"""Render and read back the user-supplied functional-case workbook contract."""
from __future__ import annotations

import hashlib
import importlib.util
from functools import lru_cache

from . import paths, task_workbook_data

TEMPLATE_ID = "test-cases-14-column-v1"
HEADERS = ["用例标识", "用例名称", "功能需求追踪", "测试说明", "前置条件", "测试步骤", "预期结果", "用例编制人", "实际结果", "测试结论", "测试执行人", "验证环境", "备注", "截图"]
FIELDS = ["id", "name", "trace", "description", "preconditions", "steps", "expected", "author", "actual", "conclusion", "executor", "environment", "remarks", "screenshots"]
WIDTHS = [26, 34, 24, 36, 30, 52, 52, 20, 46, 22, 18, 30, 48, 36]
COLORS = ["FFD966", "A9D18E", "F4B183", "BFBFBF"]
FILLS = {"Pass": "E2F0D9", "Fail": "FCE4D6", "Blocked": "FFF2CC", "Cancelled": "EDEDED", "Unverified": "FFF2CC", "NotRun": "EDEDED", "Unknown": "FFF2CC"}


def _font(*, bold=False):
    from openpyxl.styles import Font

    return Font(name="宋体", size=11, bold=bold)


def _style_row(sheet, number: int, *, header=False, group=False) -> None:
    from openpyxl.styles import Alignment, Border, PatternFill, Side

    height = 1
    for index, cell in enumerate(sheet[number], 1):
        cell.font = _font(bold=header or group)
        cell.alignment = Alignment(vertical="center" if header or group else "top", wrap_text=True)
        cell.border = Border(left=Side(style="thin", color="D9D9D9"), right=Side(style="thin", color="D9D9D9"),
                             top=Side(style="thin", color="D9D9D9"), bottom=Side(style="thin", color="D9D9D9"))
        if header:
            cell.fill = PatternFill("solid", fgColor="BFBFBF")
        if cell.value is not None and not group:
            height = max(height, task_workbook_data.display_lines(str(cell.value), WIDTHS[index - 1]))
    sheet.row_dimensions[number].height = 28 if header or group else min(409, max(48, height * 16 + 8))


def _group(sheet, name: str, depth: int, append_cells, long_text: list, *, note="") -> None:
    from openpyxl.comments import Comment
    from openpyxl.styles import PatternFill

    append_cells(sheet, [name, *([""] * 11), note, ""], long_text)
    number = sheet.max_row
    _style_row(sheet, number, group=True)
    for row in sheet.iter_rows(min_row=number, max_row=number, min_col=1, max_col=12):
        for cell in row:
            cell.fill = PatternFill("solid", fgColor=COLORS[min(depth, 4) - 1])
    sheet.merge_cells(start_row=number, end_row=number, start_column=1, end_column=12)
    sheet.cell(number, 1).comment = Comment(f"autoskill:business-level={depth}", "autoSkill")


def _display(value, sheet, column: int, long_text: list):
    if not isinstance(value, str) or task_workbook_data.display_lines(value, WIDTHS[column - 1]) <= 20:
        return value
    reference = f"{sheet.title}!R{sheet.max_row + 1}C{column}"
    long_text.extend((reference, part, fragment) for part, fragment in enumerate(task_workbook_data.text_fragments(value), 1))
    return value[:180] + "\n全文见「长文本」：" + reference


def _case(sheet, row: dict, append_cells, long_text: list) -> None:
    from openpyxl.comments import Comment
    from openpyxl.styles import PatternFill

    values = ["\n".join(row[key]) if key == "screenshots" else row[key] for key in FIELDS]
    for index in (4, 5, 9, 12, 13, 14):
        values[index - 1] = _display(values[index - 1], sheet, index, long_text)
    append_cells(sheet, values, long_text)
    number = sheet.max_row
    _style_row(sheet, number)
    sheet.cell(number, 10).fill = PatternFill("solid", fgColor=FILLS[row["group"]["status"]])
    sheet.cell(number, 1).comment = Comment("原始用例标识：" + row["group"]["definition"]["id"] + "\n定义哈希：" + row["group"]["definition_hash"], "autoSkill")


def _tree(rows: list[dict]) -> dict:
    root = {"children": {}, "rows": []}
    for row in rows:
        node = root
        for part in row["group"]["business_path"]:
            node = node["children"].setdefault(part, {"children": {}, "rows": []})
        node["rows"].append(row)
    return root


def _write_tree(sheet, tree: dict, depth: int, append_cells, long_text: list) -> None:
    for name, node in tree["children"].items():
        _group(sheet, name, depth, append_cells, long_text)
        for row in node["rows"]:
            _case(sheet, row, append_cells, long_text)
        _write_tree(sheet, node, depth + 1, append_cells, long_text)


def render(book, snapshot: dict, append_cells, table, long_text: list) -> list[dict]:
    rows, data = task_workbook_data.rows(snapshot)
    sheet = book.create_sheet("功能测试用例", 0)
    append_cells(sheet, HEADERS, long_text)
    _style_row(sheet, 1, header=True)
    for index, width in enumerate(WIDTHS, 1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
    _write_tree(sheet, _tree(rows), 1, append_cells, long_text)
    if not rows:
        _group(sheet, snapshot["project"].get("name") or "【待确认：业务系统】", 1, append_cells, long_text,
               note="本任务尚未形成用例；空范围不表示覆盖或测试通过。")
    sheet.freeze_panes = "D2"
    sheet.sheet_view.zoomScale = 80
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    sheet.print_title_rows = "1:1"
    mapping = [{"display": row["id"], "original": row["group"]["definition"]["id"], "version": row["group"]["definition_hash"],
                "path": row["group"]["business_path"], "original_path": row["group"]["original_business_path"],
                "folder_path": row["group"].get("folder_path", []),
                "task": snapshot["task_id"], "runs": row["group"]["run_ids"],
                "steps": {"编号所在表": "测试数据" if "/完整步骤" in row["steps"] else "功能测试用例", "对应关系": row["step_mapping"]},
                "scope": "FT 仅在本报告快照内编号；原始标识和定义版本不变，跨任务以原始标识＋定义哈希关联。"} for row in rows]
    table(book, "编号映射", [("display", "本报告 FT 标识", 30), ("original", "原始用例标识", 28), ("version", "冻结定义 SHA256", 68),
                           ("path", "本报告业务路径", 60), ("original_path", "来源业务路径", 60), ("task", "任务标识", 30),
                           ("runs", "关联运行", 60), ("steps", "原步骤号到展示步骤号", 40), ("scope", "编号范围", 65),
                           ("folder_path", "本次导出的文件夹组织路径", 65)], mapping, long_text)
    if data:
        fragments = [{"id": item["id"], "value": fragment, "part": part}
                     for item in data for part, fragment in enumerate(task_workbook_data.text_fragments(item["value"]), 1)]
        table(book, "测试数据", [("id", "原文引用", 50), ("value", "原文片段（按片段顺序直接拼接）", 110), ("part", "片段顺序", 12)], fragments, long_text)
    return rows


@lru_cache(maxsize=1)
def _validator():
    file = paths.SKILL / "references/test-cases/scripts/validate_test_cases.py"
    spec = importlib.util.spec_from_file_location("autoskill_case_document_validator", file)
    if not spec or not spec.loader:
        raise ValueError("测试用例模板校验器不可用")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(book, rows: list[dict]) -> dict:
    sheet = book["功能测试用例"]
    if book.sheetnames[0] != sheet.title or sheet.max_column != 14:
        raise ValueError("测试用例工作簿主表或 14 列结构不匹配")
    result = _validator().validate_sheet(sheet, expected_cases=len(rows))
    result["caseGroupDepths"] = {str(depth): count for depth, count in result["caseGroupDepths"].items()}
    if not result["ok"]:
        raise ValueError("测试用例工作簿结构校验失败：" + "；".join(result["errors"][:5]))
    grouped = {number for merged in sheet.merged_cells.ranges for number in range(merged.min_row, merged.max_row + 1)}
    actual = {row[0]: row for number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2) if number not in grouped and row[0]}
    for row in rows:
        record = actual.get(row["id"])
        if not record or record[9] != row["conclusion"] or record[7] != row["author"]:
            raise ValueError("保存后的用例身份或执行结论不匹配，未交付文件")
    mapped = {(row[0], row[1], row[2]) for row in book["编号映射"].iter_rows(min_row=2, values_only=True)}
    expected = {(row["id"], row["group"]["definition"]["id"], row["group"]["definition_hash"]) for row in rows}
    if mapped != expected:
        raise ValueError("保存后的原始用例编号映射不匹配，未交付文件")
    for worksheet in book:
        if any(cell.value is not None and cell.font.name != "宋体" for line in worksheet for cell in line):
            raise ValueError("保存后的工作簿字体不符合宋体规则，未交付文件")
    folder = paths.SKILL / "references/test-cases"
    hashes = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
              for name in ("SKILL.md", "references/workbook-rules.md", "scripts/validate_test_cases.py")}
    return {"structure": result, "rule_files_sha256": hashes, "case_rows": len(rows), "case_ids_match": True,
            "semantic_validation": "结构检查不证明业务事实、独立预期或覆盖完整性；待确认项保留在用例中。",
            "numbering_scope": "报告快照内一对一映射；至少三位序号，1000 以上自然扩位。", "visual_validation": "not_performed_by_runtime"}
