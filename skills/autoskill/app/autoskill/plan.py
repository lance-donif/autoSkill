"""The case sheet: test cases designed from a requirement document, kept as an Excel file in a swappable skill.

The default session uses `autoskill-cases` next to the autoskill skill; named sessions have separate
default folders. AUTOSKILL_CASE_SKILL selects an explicit folder. Replace that folder (or just the
Excel file in it) with another of the same name and the same commands execute those cases instead.
Columns are found by their header text, so a hand-made sheet works as long as it has 用例编号 and 用例标题.
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import uuid
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from . import execution_inputs, paths, projects
from .locking import file_lock, project_lock

COLUMNS = [("id", "用例编号", 12), ("module", "子项目", 14), ("title", "用例标题", 32), ("type", "操作类型", 10),
           ("priority", "优先级", 8), ("preconditions", "前置条件", 24), ("steps", "操作步骤", 50), ("expected", "预期结果", 40),
           ("status", "执行结果", 10), ("note", "备注", 40), ("replay", "回放用例", 22)]
FUNCTIONAL_FIELDS = ("description", "author", "remarks", "source", "requirement_ids", "business_path", "prefix")
ALIASES = {"编号": "id", "用例ID": "id", "模块": "module", "标题": "title", "用例名称": "title", "类型": "type", "步骤": "steps",
           "测试步骤": "steps", "期望结果": "expected", "结果": "status", "是否通过": "status"}
STATUS = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞"}
FILL = {"通过": "E2F0D9", "失败": "FCE4D6", "阻塞": "FFF2CC"}
_selected_folder: ContextVar[Path | None] = ContextVar("autoskill_plan_folder", default=None)
SKILL_TEXT = """---
name: autoskill-cases
description: 要执行的测试用例表（Excel）。执行、回放或汇报这些用例时使用；需要 autoSkill 技能。整个文件夹可以替换成同名的另一份用例。
---
# 测试用例表

用例在本目录的内部编辑表（{file}），共 {count} 条{project}。该 11 列表供命令读写，不作为用户交付 Excel。
按上传 test-cases 规范生成的正式 14 列设计文档在 {delivery}；向用户提供正式文件，设计阶段执行结论为未执行。

执行本目录的用例表时，把当前任务进程的 `AUTOSKILL_CASE_SKILL` 设置为本 `SKILL.md` 所在目录，确保读取的是这一份表。
正在使用命名会话时，下面每条命令都继续使用相同的 `--session` 标识；不要切换到另一个任务的录制。以下示例省略了该全局前缀。

1. `autoskill plan show` 读出全部用例。
2. 按用例逐条执行：`autoskill record start <项目>` → 按「操作步骤」操作页面 → 用 `autoskill check` 写上「预期结果」→
   `autoskill record stop --title <用例标题> --subproject <子项目> --plan <用例编号>`。
3. `autoskill run <项目>` 回放；每条用例的通过、失败、阻塞会自动写回 Excel 的「执行结果」列。
4. 无法自动执行的用例：`autoskill plan mark <用例编号> 阻塞 --note <原因>`。
"""


@contextlib.contextmanager
def use_folder(folder: str | Path):
    """Select one explicit sheet for an adapter without changing shared environment variables."""
    token = _selected_folder.set(Path(folder))
    try:
        yield
    finally:
        _selected_folder.reset(token)


@contextlib.contextmanager
def _sheet_lock():
    """Explicitly shared folders use the same stable lock even across different workspaces."""
    project_id = _managed_project(_folder())
    guard = project_lock(project_id) if project_id is not None else contextlib.nullcontext()
    try:
        with file_lock(_folder() / ".autoskill-plan.lock"), guard:
            yield
    except PermissionError:
        raise ValueError(f"用例表正被其他程序打开（例如 Excel），关闭后重试：{_file()}") from None


def _managed_project(folder: Path) -> str | None:
    """Project exports share the lock of managed sheets while legacy sheets keep their own scope."""
    try:
        relative = folder.resolve().relative_to(paths.projects_dir().resolve())
    except ValueError:
        return None
    parts = relative.parts
    return parts[0] if len(parts) == 4 and parts[1:3] == ("testcases", "design-plans") and projects.ID.fullmatch(parts[0]) else None


def _sheet_locked(operation):
    """Acquire the sheet before any project locks used by the operation."""
    @wraps(operation)
    def locked(*args, **kwargs):
        with _sheet_lock():
            return operation(*args, **kwargs)
    return locked


def _folder() -> Path:
    return _selected_folder.get() or paths.case_skill()


def _file() -> Path:
    folder = _folder()
    found = sorted(f for f in folder.glob("*.xlsx") if not f.name.startswith("~$")) if folder.is_dir() else []
    return found[0] if found else folder / "cases.xlsx"


def _open():
    """(workbook, sheet, {field: column number}); missing result columns are added so any sheet can take verdicts."""
    from openpyxl import load_workbook

    file = _file()
    if not file.is_file():
        raise ValueError(f"还没有用例表；先用 autoskill plan write <项目> --file <用例.json> 生成，或把用例 Excel 放到 {_folder()}")
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
    temporary = None
    try:
        candidate = file.with_name(f".autoskill-plan-{uuid.uuid4().hex}.tmp")
        candidate.touch(exist_ok=False)
        temporary = candidate
        if file.is_file():
            shutil.copymode(file, temporary)
        book.save(temporary)
        temporary.replace(file)
    except PermissionError:
        raise ValueError(f"用例表正被其他程序打开（例如 Excel），关闭后重试：{file}") from None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
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
    cell.data_type = "s"
    cell.fill = PatternFill("solid", fgColor=FILL[status]) if status in FILL else PatternFill()
    if note is not None:
        sheet.cell(number, columns["note"], note).data_type = "s"


def _delivery(project_id: str, info: dict, items: list[dict], folder: Path) -> str:
    """Render the public design document separately from the compatible operational sheet."""
    from . import reporting

    cases = []
    for number, item in enumerate(items, 1):
        identity = item.get("id") or f"TC-{number:03d}"
        document = {key: item[key] for key in ("description", "preconditions", "steps", "expected", "source", "business_path", "prefix", "author", "remarks") if item.get(key)}
        document.setdefault("description", item["title"])
        definition = {**item, "id": identity, "kind": "scenario", "module": item.get("module") or "未分组",
                      "extensions": {"test_case_document": document}}
        cases.append({"definition": definition, "result": None, "run_id": None, "evidence_valid": False})
    identity = execution_inputs.content_hash(items)
    destination = folder / "exports" / ("functional-design-" + identity[:16] + ".xlsx")
    snapshot = {"schema": 1, "kind": "design", "task_id": "design-" + identity, "project_id": project_id,
                "project": {"id": project_id, "name": info["name"], "base_url": info["base_url"]},
                "title": "设计用例表", "status": "Design", "cases": cases, "results": [], "run_ids": [],
                "sources": {"design_source_hash": identity}, "notes": ["设计文档不等于执行结果；真实录制及回归后另交付任务报告。"], "metadata": {}}
    with project_lock(project_id):
        reporting.write_task(snapshot, destination)
    return str(destination)


def _source_hash(row: dict) -> str:
    return execution_inputs.content_hash({key: str(row.get(key, "") or "") for key, _, _ in COLUMNS[:8]})


def _source_rows(rows: list[dict]) -> dict:
    source_file = _folder() / "functional-design.json"
    if not source_file.is_file():
        return {}
    source = json.loads(source_file.read_bytes())
    metadata = _folder() / "design.json"
    revision = execution_inputs.content_hash(json.loads(metadata.read_bytes())) if metadata.is_file() else None
    if source.get("online_revision") != revision:
        return {}
    return {row["id"]: source["rows"][row["id"]]["document"] for row in rows
            if source.get("rows", {}).get(row["id"], {}).get("row_hash") == _source_hash(row)}


def source_metadata() -> dict:
    """Return only source fields still bound to the operational workbook's exact business rows."""
    if not (_folder() / "functional-design.json").is_file() or not _file().is_file():
        return {}
    book, sheet, columns = _open()
    try:
        return _source_rows([row for _, row in _rows(sheet, columns)])
    finally:
        book.close()


@_sheet_locked
def write(project_id: str, file: str, *, delivery: bool = True) -> dict:
    """Turn designed cases (a JSON array) into the Excel case sheet and its skill."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    managed = _managed_project(_folder())
    if managed is not None and managed != project_id:
        raise ValueError("项目内设计表不能写入其他项目的用例")
    info = projects.get(project_id)
    try:
        items = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"读不了用例文件 {file}：{error}") from None
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) and i.get("title") for i in items):
        raise ValueError("用例文件需要是数组，每条至少有 title；可用字段：id module title type priority preconditions steps expected")
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "用例"
    sheet.append([label for _, label, _ in COLUMNS])
    source_rows = {}
    for number, item in enumerate(items, 1):
        steps = item.get("steps", "")
        if isinstance(steps, list):
            steps = "\n".join(step if re.match(r"^\d+\.\s", step) else f"{i}. {step}" for i, step in enumerate(steps, 1))
        row = {**item, "id": item.get("id") or f"TC-{number:03d}", "module": item.get("module") or "未分组", "steps": steps,
               "priority": item.get("priority") or "P1", "status": "未执行", "note": "", "replay": ""}
        sheet.append([str(row.get(key, "") or "") for key, _, _ in COLUMNS])
        source_rows[str(row["id"])] = {"row_hash": _source_hash(row), "document": {key: item[key] for key in FUNCTIONAL_FIELDS if key in item}}
    for index, (_, _, width) in enumerate(COLUMNS, 1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
        sheet.cell(1, index).font = Font(bold=True)
    for line in sheet.iter_rows(min_row=2):
        for cell in line:
            cell.data_type = "s"  # designed case content is literal text, including values beginning with "="
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "A2"
    meta = book.create_sheet("信息")
    for pair in (("项目", project_id), ("名称", info["name"]), ("被测地址", info["base_url"])):
        meta.append(pair)
        meta.cell(meta.max_row, 2).data_type = "s"
    saved = _save(book)
    if delivery:
        metadata = folder / "design.json"
        revision = execution_inputs.content_hash(json.loads(metadata.read_bytes())) if metadata.is_file() else None
        execution_inputs.write_json(folder / "functional-design.json", {"schema": 1, "online_revision": revision, "rows": source_rows})
    public_file = _delivery(project_id, info, items, folder) if delivery else None
    (folder / "SKILL.md").write_text(SKILL_TEXT.format(file=Path(saved).name, count=len(items), project=f"，项目 {project_id}",
                                                     delivery=public_file or "在线设计用例表的「下载 14 列 Excel」"), encoding="utf-8")
    return {"skill": str(folder), "excel": saved, "cases": len(items), "project": project_id,
            "excel_role": "内部编辑表（11 列），非用户交付 Excel", "delivery_excel": public_file,
            "next": "向用户提供 delivery_excel 正式 14 列设计文档；excel 仅供内部编辑。按 autoskill plan show 逐条执行，录制结束时加 --plan <用例编号>"}


def show() -> dict:
    book, sheet, columns = _open()
    meta = book["信息"] if "信息" in book.sheetnames else None
    project = str(meta.cell(1, 2).value or "") if meta else ""
    rows = [row for _, row in _rows(sheet, columns)]
    metadata_file = _folder() / "design.json"
    extra_rows = json.loads(metadata_file.read_bytes()).get("rows", {}) if metadata_file.is_file() else {}
    for row in rows:
        extra = extra_rows.get(row["id"], {})
        row.update({key: extra[key] for key in FUNCTIONAL_FIELDS if key in extra})
    sources = _source_rows(rows)
    for row in rows:
        row.update(sources.get(row["id"], {}))
        if metadata_file.is_file() and project:
            row.setdefault("business_path", [projects.meta(project)["name"], row.get("module") or "未分组"])
    return {"skill": str(_folder()), "excel": str(_file()), "project": project or None, "cases": rows, "summary": _summary(sheet, columns)}


def _find(sheet, columns, case_id: str) -> int | None:
    return next((number for number, row in _rows(sheet, columns) if row["id"] == case_id), None)


@_sheet_locked
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
        managed = _managed_project(_folder())
        if managed is not None and managed != project_id:
            raise ValueError("项目内设计表不能关联其他项目的用例")
        with _sheet_lock():
            book, sheet, columns = _open()
            number = _find(sheet, columns, case_id)
            if number is None:
                return {"error": f"用例表里没有 {case_id}，没有关联；用例本身已保存"}
            sheet.cell(number, columns["replay"], f"{project_id}/{replay_case}")
            if not assertions:
                sheet.cell(number, columns["note"], "录制时没有写断言：只能证明动作可以完成")
            _save(book)
            if managed is not None and (_folder() / "design.json").is_file():
                from . import case_plans

                case_plans.recorded_binding(project_id, case_id, replay_case, assertions)
    except ValueError as error:
        return {"error": str(error)}
    return {"case": case_id, "replay": f"{project_id}/{replay_case}", "next": "autoskill run 回放后，结果会写回用例表"}


def _current_cases(project_id: str) -> dict[str, dict]:
    """A removed project invalidates its old verdicts without interrupting other rows."""
    try:
        projects.path(project_id)
    except ValueError:
        return {}
    return {case["id"]: case for case in projects.cases(project_id)}


def sync(quiet: bool = False, *, folder: str | Path | None = None) -> dict | None:
    """Use the queued task's sheet without mutating process-wide environment variables."""
    token = _selected_folder.set(Path(folder) if folder is not None else _selected_folder.get())
    try:
        return _sync(quiet)
    finally:
        _selected_folder.reset(token)


def _sync(quiet: bool) -> dict | None:
    """Write the latest replay verdict of every linked row back into the sheet."""
    from . import reporting

    try:
        with _sheet_lock():
            book, sheet, columns = _open()
            metadata_file = _folder() / "design.json"
            design = json.loads(metadata_file.read_bytes()) if metadata_file.is_file() else None
            managed_project = _managed_project(_folder())
            latest: dict[str, dict] = {}
            cases: dict[str, dict] = {}
            updated = 0
            for number, row in _rows(sheet, columns):
                project_id, _, replay_case = row["replay"].partition("/")
                if not replay_case:
                    continue
                if managed_project is not None and project_id != managed_project:
                    _set_status(sheet, columns, number, "未执行", "关联属于其他项目，不能借用其执行结果")
                    updated += 1
                    continue
                if project_id not in latest:
                    latest[project_id] = reporting.latest_results(project_id)
                    cases[project_id] = _current_cases(project_id)
                result = latest[project_id].get(replay_case)
                if design is not None:
                    from . import case_plans

                    evidence = case_plans._evidence(project_id, row, design.get("rows", {}).get(row["id"], {}), latest[project_id], cases[project_id])
                    _set_status(sheet, columns, number, case_plans.STATUS[evidence["status"]], evidence["reason"])
                    updated += 1
                    continue
                if result and result["status"] in STATUS:
                    case = cases[project_id].get(replay_case)
                    detail = json.loads(result["detail"])
                    if not case or not execution_inputs.result_current(project_id, case, result):
                        _set_status(sheet, columns, number, "未执行", "用例已修改或不再有效，需重新回放")
                    elif result["status"] == "Pass" and not detail.get("business_verified"):
                        _set_status(sheet, columns, number, "未执行", "动作已完成；没有完整业务断言的实际通过证据")
                    else:
                        _set_status(sheet, columns, number, STATUS[result["status"]], "" if result["status"] == "Pass" else result["reason"])
                    updated += 1
            if not updated:
                return None if quiet else {"updated": 0, "note": "没有已关联并回放过的用例；录制时加 --plan <用例编号> 关联"}
            return {"excel": _save(book), "updated": updated, "summary": _summary(sheet, columns)}
    except ValueError as error:
        if quiet:
            return None if "还没有用例表" in str(error) else {"error": str(error)}
        raise
