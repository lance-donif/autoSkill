"""The case sheet: test cases designed from a requirement document, kept as an Excel file in a swappable skill.

The skill is always the folder `autoskill-cases` next to the autoskill skill. Replace that folder (or just
the Excel file in it) with another of the same name and the same commands execute those cases instead.
Columns are found by their header text, so a hand-made sheet works as long as it has 用例编号 and 用例标题.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import paths, projects

COLUMNS = [("id", "用例编号", 12), ("module", "子项目", 14), ("title", "用例标题", 32), ("type", "操作类型", 10),
           ("priority", "优先级", 8), ("preconditions", "前置条件", 24), ("steps", "操作步骤", 50), ("expected", "预期结果", 40),
           ("status", "执行结果", 10), ("note", "备注", 40), ("replay", "回放用例", 22)]
ALIASES = {"编号": "id", "用例ID": "id", "模块": "module", "标题": "title", "用例名称": "title", "类型": "type", "步骤": "steps",
           "测试步骤": "steps", "期望结果": "expected", "结果": "status", "是否通过": "status"}
STATUS = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞"}
FILL = {"通过": "E2F0D9", "失败": "FCE4D6", "阻塞": "FFF2CC"}
SKILL_TEXT = """---
name: autoskill-cases
description: 要执行的测试用例表（Excel）。执行、回放或汇报这些用例时使用；需要 autoSkill 技能。整个文件夹可以替换成同名的另一份用例。
---
# 测试用例表

用例在本目录的 Excel 文件里（{file}），共 {count} 条{project}。

1. `autoskill plan show` 读出全部用例。
2. 按用例逐条执行：`autoskill record start <项目>` → 按「操作步骤」操作页面 → 用 `autoskill check` 写上「预期结果」→
   `autoskill record stop --title <用例标题> --subproject <子项目> --plan <用例编号>`。
3. `autoskill run <项目>` 回放；每条用例的通过、失败、阻塞会自动写回 Excel 的「执行结果」列。
4. 无法自动执行的用例：`autoskill plan mark <用例编号> 阻塞 --note <原因>`。
"""


def _file() -> Path:
    folder = paths.case_skill()
    found = sorted(f for f in folder.glob("*.xlsx") if not f.name.startswith("~$")) if folder.is_dir() else []
    return found[0] if found else folder / "cases.xlsx"


def _open():
    """(workbook, sheet, {field: column number}); missing result columns are added so any sheet can take verdicts."""
    from openpyxl import load_workbook

    file = _file()
    if not file.is_file():
        raise ValueError(f"还没有用例表；先用 autoskill plan write <项目> --file <用例.json> 生成，或把用例 Excel 放到 {paths.case_skill()}")
    book = load_workbook(file)
    sheet = book.worksheets[0]
    labels = {label: key for key, label, _ in COLUMNS}
    columns = {}
    for cell in sheet[1]:
        key = labels.get(str(cell.value or "").strip()) or ALIASES.get(str(cell.value or "").strip())
        if key:
            columns.setdefault(key, cell.column)
    if "id" not in columns or "title" not in columns:
        raise ValueError(f"用例表 {file} 的第一行需要有「用例编号」和「用例标题」两列")
    for key, label, _ in COLUMNS:
        if key in ("status", "note", "replay") and key not in columns:
            columns[key] = sheet.max_column + 1
            sheet.cell(1, columns[key], label)
    return book, sheet, columns


def _save(book) -> str:
    file = _file()
    try:
        book.save(file)
    except PermissionError:
        raise ValueError(f"用例表正被其他程序打开（例如 Excel），关闭后重试：{file}") from None
    return str(file)


def _rows(sheet, columns):
    for number in range(2, sheet.max_row + 1):
        if sheet.cell(number, columns["id"]).value:
            yield number, {key: str(sheet.cell(number, column).value or "") for key, column in columns.items()}


def _summary(sheet, columns) -> dict:
    counts = {"通过": 0, "失败": 0, "阻塞": 0, "未执行": 0}
    for _, row in _rows(sheet, columns):
        counts[row["status"] if row["status"] in counts else "未执行"] += 1
    return counts


def _set_status(sheet, columns, number: int, status: str, note: str | None = None) -> None:
    from openpyxl.styles import PatternFill

    cell = sheet.cell(number, columns["status"], status)
    cell.fill = PatternFill("solid", fgColor=FILL[status]) if status in FILL else PatternFill()
    if note is not None:
        sheet.cell(number, columns["note"], note)


def write(project_id: str, file: str) -> dict:
    """Turn designed cases (a JSON array) into the Excel case sheet and its skill."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    info = projects.get(project_id)
    try:
        items = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"读不了用例文件 {file}：{error}") from None
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) and i.get("title") for i in items):
        raise ValueError("用例文件需要是数组，每条至少有 title；可用字段：id module title type priority preconditions steps expected")
    folder = paths.case_skill()
    folder.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "用例"
    sheet.append([label for _, label, _ in COLUMNS])
    for number, item in enumerate(items, 1):
        steps = item.get("steps", "")
        if isinstance(steps, list):
            steps = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
        row = {**item, "id": item.get("id") or f"TC-{number:03d}", "module": item.get("module") or "未分组", "steps": steps,
               "priority": item.get("priority") or "P1", "status": "未执行", "note": "", "replay": ""}
        sheet.append([str(row.get(key, "") or "") for key, _, _ in COLUMNS])
    for index, (_, _, width) in enumerate(COLUMNS, 1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
        sheet.cell(1, index).font = Font(bold=True)
    for line in sheet.iter_rows(min_row=2):
        for cell in line:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "A2"
    meta = book.create_sheet("信息")
    for pair in (("项目", project_id), ("名称", info["name"]), ("被测地址", info["base_url"])):
        meta.append(pair)
    saved = _save(book)
    (folder / "SKILL.md").write_text(SKILL_TEXT.format(file=Path(saved).name, count=len(items), project=f"，项目 {project_id}"), encoding="utf-8")
    return {"skill": str(folder), "excel": saved, "cases": len(items), "project": project_id,
            "next": "把 excel 的路径告诉用户；然后按 autoskill plan show 逐条执行，录制结束时加 --plan <用例编号>"}


def show() -> dict:
    book, sheet, columns = _open()
    meta = book["信息"] if "信息" in book.sheetnames else None
    project = str(meta.cell(1, 2).value or "") if meta else ""
    return {"skill": str(paths.case_skill()), "excel": str(_file()), "project": project or None,
            "cases": [row for _, row in _rows(sheet, columns)], "summary": _summary(sheet, columns)}


def _find(sheet, columns, case_id: str) -> int | None:
    return next((number for number, row in _rows(sheet, columns) if row["id"] == case_id), None)


def mark(case_id: str, status: str, note: str = "") -> dict:
    book, sheet, columns = _open()
    number = _find(sheet, columns, case_id)
    if number is None:
        raise ValueError(f"用例表里没有 {case_id}")
    _set_status(sheet, columns, number, status, note)
    return {"case": case_id, "status": status, "excel": _save(book), "summary": _summary(sheet, columns)}


def link(case_id: str, project_id: str, replay_case: str, assertions: int) -> dict:
    """Remember which recorded case carries out a row; never fails the recording that has just been saved."""
    try:
        book, sheet, columns = _open()
        number = _find(sheet, columns, case_id)
        if number is None:
            return {"error": f"用例表里没有 {case_id}，没有关联；用例本身已保存"}
        sheet.cell(number, columns["replay"], f"{project_id}/{replay_case}")
        if not assertions:
            sheet.cell(number, columns["note"], "录制时没有写断言：只能证明动作可以完成")
        _save(book)
    except ValueError as error:
        return {"error": str(error)}
    return {"case": case_id, "replay": f"{project_id}/{replay_case}", "next": "autoskill run 回放后，结果会写回用例表"}


def sync(quiet: bool = False) -> dict | None:
    """Write the latest replay verdict of every linked row back into the sheet."""
    from . import reporting

    try:
        book, sheet, columns = _open()
        latest: dict[str, dict] = {}
        updated = 0
        for number, row in _rows(sheet, columns):
            project_id, _, replay_case = row["replay"].partition("/")
            if not replay_case:
                continue
            if project_id not in latest:
                latest[project_id] = reporting.latest_results(project_id)
            result = latest[project_id].get(replay_case)
            if result and result["status"] in STATUS:
                _set_status(sheet, columns, number, STATUS[result["status"]], "" if result["status"] == "Pass" else result["reason"])
                updated += 1
        if not updated:
            return None if quiet else {"updated": 0, "note": "没有已关联并回放过的用例；录制时加 --plan <用例编号> 关联"}
        return {"excel": _save(book), "updated": updated, "summary": _summary(sheet, columns)}
    except ValueError as error:
        if quiet:
            return None if "还没有用例表" in str(error) else {"error": str(error)}
        raise
