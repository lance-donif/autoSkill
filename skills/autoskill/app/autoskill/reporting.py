"""Excel workbook and Markdown summary for a project: its cases and what the latest replay of each showed."""
from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from pathlib import Path

from . import db, execution_inputs, paths, projects
from .replay import ir

STATUS = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", "Cancelled": "已取消", "Unverified": "业务未验证", None: "未执行"}
FILL = {"Pass": "E2F0D9", "Fail": "FCE4D6", "Blocked": "FFF2CC", "Cancelled": "EDEDED", "Unverified": "FFF2CC", None: "EDEDED"}
BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def latest_results(project_id: str) -> dict[str, dict]:
    """The most recent counted result of every case (prerequisite runs and automatic logins excluded)."""
    rows = db.rows(
        "SELECT r.* FROM results r JOIN runs u ON u.id = r.run_id WHERE r.project_id = ? AND r.prerequisite = 0 "
        "AND u.trigger != 'login' AND r.id IN (SELECT MAX(x.id) FROM results x JOIN runs xu ON xu.id = x.run_id "
        "WHERE x.project_id = ? AND x.prerequisite = 0 AND xu.trigger != 'login' GROUP BY x.case_id)",
        (project_id, project_id))
    return {row["case_id"]: row for row in rows}


def _text(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = BAD_CHARS.sub("", text)
    return text[:32000]


def _rows(project_id: str) -> list[dict]:
    results = latest_results(project_id)
    out = []
    for case in projects.cases(project_id):
        result = results.get(case["id"])
        stale = bool(result) and not execution_inputs.result_current(project_id, case, result)
        detail = json.loads(result["detail"]) if result else {}
        unverified = bool(result) and result["status"] == "Pass" and not detail.get("business_verified")
        status = None if not result or stale else result["status"]
        if unverified:
            status = None
        if case["kind"] == "ui":
            automation = case.get("extensions", {}).get("automation") or {}
            steps = ["打开 " + str(automation.get("start_url", "")), *[s.get("name") or ir.describe(s) for s in automation.get("actions", [])]]
            checks = [s.get("name") or ir.describe(s) for s in automation.get("assertions", [])]
        else:
            steps = [f'{case["method"]} {case["path"]}']
            rules = case["assertions"]
            checks = [f'状态码 {rules["status"]}', *[f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in rules.get("json_equals", {}).items()]]
            if "schema" in rules:
                checks.append("响应结构与录制时一致")
        out.append({
            "id": case["id"], "kind": "页面" if case["kind"] == "ui" else "接口", "module": case["module"], "title": case["title"],
            "priority": case["priority"], "depends": "、".join(projects.depends_on(case)),
            "steps": "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), "checks": "\n".join(checks),
            "status": status, "status_text": STATUS[status] + ("（输入或证据已变更，需重新回放）" if stale else "（动作完成，业务未验证）" if unverified else ""),
            "reason": "" if status in ("Pass", None) else result["reason"],
            "engine": "" if not result else result["engine"], "ran_at": "" if not result else result["started_at"],
            "ms": "" if not result else result["duration_ms"],
        })
    return out


COLUMNS = [("id", "用例编号", 14), ("kind", "类型", 8), ("module", "模块", 14), ("title", "用例标题", 30), ("priority", "优先级", 8),
           ("depends", "前置用例", 16), ("steps", "回放步骤", 52), ("checks", "断言", 44), ("status_text", "最近结果", 14),
           ("reason", "失败或阻塞原因", 44), ("engine", "回放引擎", 12), ("ran_at", "最近执行时间", 24), ("ms", "耗时(ms)", 10)]


def build(project_id: str, out: str | None = None) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    info = projects.get(project_id)
    rows = _rows(project_id)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:6]  # noqa: DTZ005  # Local display time plus unique identity prevents concurrent overwrites.
    folder = Path(out) if out else paths.local() / "reports" / project_id
    folder.mkdir(parents=True, exist_ok=True)
    counts = {key: sum(r["status"] == key for r in rows) for key in ("Pass", "Fail", "Blocked", None)}
    judged = counts["Pass"] + counts["Fail"]

    book = Workbook()
    sheet = book.active
    sheet.title = "用例与结果"
    sheet.append([label for _, label, _ in COLUMNS])
    for row in rows:
        sheet.append([_text(row[key]) if key != "ms" else row[key] for key, _, _ in COLUMNS])
    head_fill = PatternFill("solid", fgColor="1C5CAB")
    for index, (_key, _, width) in enumerate(COLUMNS, 1):
        cell = sheet.cell(1, index)
        cell.font, cell.fill = Font(color="FFFFFF", bold=True), head_fill
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[cell.column_letter].width = width
    status_col = [key for key, _, _ in COLUMNS].index("status_text") + 1
    for line, row in enumerate(rows, 2):
        for cell in sheet[line]:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, str):
                cell.data_type = "s"  # user content is always text, never a formula
        sheet.cell(line, status_col).fill = PatternFill("solid", fgColor=FILL[row["status"]])
    sheet.freeze_panes = "E2"
    sheet.auto_filter.ref = sheet.dimensions

    overview = book.create_sheet("概览")
    lines = [("项目", info["name"]), ("被测地址", info["base_url"]),
             ("生成时间", datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),  # noqa: DTZ005  # Report text preserves local wall-clock time.
             ("用例总数", len(rows)), ("通过", counts["Pass"]), ("失败", counts["Fail"]), ("阻塞", counts["Blocked"]),
             ("未执行", counts[None]), ("通过率", f"{counts['Pass'] / judged:.1%}" if judged else "—"),
             ("口径", "每条用例取最近一次回放结果；通过率 = 通过 ÷（通过 + 失败），阻塞与未执行不计入。")]
    for label, value in lines:
        overview.append([label, value])
        if isinstance(value, str):
            overview.cell(overview.max_row, 2).data_type = "s"
    overview.column_dimensions["A"].width, overview.column_dimensions["B"].width = 14, 70
    for cell in overview["A"]:
        cell.font = Font(bold=True)

    xlsx = folder / f"{project_id}-测试报告-{stamp}.xlsx"
    book.save(xlsx)

    def cell(text) -> str:
        return str(text).replace("|", "\\|").replace("\n", " ")

    md = [f"# {info['name']} 测试报告", "", f"- 被测地址：{info['base_url']}",
          f"- 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",  # noqa: DTZ005  # Report text preserves local wall-clock time.
          f"- 用例 {len(rows)} 条：通过 {counts['Pass']}，失败 {counts['Fail']}，阻塞 {counts['Blocked']}，未执行 {counts[None]}",
          f"- 通过率：{counts['Pass'] / judged:.1%}（通过 ÷（通过 + 失败），阻塞与未执行不计入）" if judged else "- 通过率：—（还没有可判定的结果）",
          "", "| 用例 | 类型 | 标题 | 最近结果 | 原因 |", "| --- | --- | --- | --- | --- |"]
    md += [f"| {r['id']} | {r['kind']} | {cell(r['title'])} | {r['status_text']} | {cell(r['reason'])} |" for r in rows]
    markdown = folder / f"{project_id}-测试报告-{stamp}.md"
    markdown.write_text("\n".join(md) + "\n", encoding="utf-8")
    return {"excel": str(xlsx), "markdown": str(markdown), "cases": len(rows), "passed": counts["Pass"], "failed": counts["Fail"],
            "blocked": counts["Blocked"], "not_run": counts[None], "pass_rate": round(counts["Pass"] / judged, 4) if judged else None}


def _definition_fields(case: dict) -> dict:
    kind = case.get("kind", "unknown")
    if kind == "ui":
        automation = case.get("extensions", {}).get("automation", {})
        actions = automation.get("actions", [])
        steps = ["打开 " + str(automation.get("start_url", "")), *[step.get("name") or ir.describe(step) for step in actions]]
        checks = [step.get("name") or ir.describe(step) for step in [*actions, *automation.get("assertions", [])] if "assertion" in step]
    elif kind == "api":
        steps = [str(case.get("method", "")) + " " + str(case.get("path", ""))]
        checks = [json.dumps(case.get("assertions", {}), ensure_ascii=False)]
    else:
        steps = [str(turn.get("question", "")) for turn in case.get("turns", [])]
        checks = [json.dumps(case.get("assertions", []), ensure_ascii=False)]
    return {"id": case["id"], "kind": {"ui": "页面", "api": "接口", "native": "原生/对话", "scenario": "设计场景"}.get(kind, "输入不可用"),
            "title": case.get("title", case["id"]), "module": case.get("module", ""), "priority": case.get("priority", ""),
            "depends": "、".join(projects.depends_on(case)), "steps": "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1)),
            "checks": "\n".join(checks)}


def _task_row(item: dict) -> dict:
    result = item.get("result") or {}
    detail = result.get("detail", {})
    status = result.get("status")
    note = item.get("note", "")
    if status == "Pass" and (not item.get("evidence_valid") or detail.get("business_status") != "Pass"):
        status = "Unverified"
        note += " 动作结果不等于完整业务验证；缺少有效证据或独立业务断言。"
    historical = item.get("historical_result") or {}
    if historical.get("business_status") == "Fail" or historical.get("status") == "Fail":
        status = "Fail"
        note += " 保留历史失败摘要；原始执行证据已删除。"
    if item.get("recording_result"):
        note += " 录制观察：" + json.dumps(item["recording_result"], ensure_ascii=False)
    fields = _definition_fields(item["definition"])
    business = detail.get("business_status", "Unverified")
    if not item.get("evidence_valid") and business != "Fail":
        business = "Unverified"
    return {**fields, "status": status, "status_text": STATUS.get(status, str(status)),
            "reason": "\n".join(value for value in (result.get("reason", ""), note) if value),
            "engine": result.get("engine", ""), "ran_at": result.get("started_at", ""), "ms": result.get("duration_ms", ""),
            "run_id": item.get("run_id", ""), "business": business,
            "scenarios": "、".join(item.get("scenario_ids", [])), "round": item.get("validation_round", ""),
            "actual": result.get("actual", "") or json.dumps(detail.get("assertions", []), ensure_ascii=False)}


def _append_cells(sheet, values, long_text: list) -> None:
    clean, extended = [], []
    for index, original in enumerate(values, 1):
        value = json.dumps(original, ensure_ascii=False) if isinstance(original, (dict, list)) else original
        if isinstance(value, str):
            value = BAD_CHARS.sub("", value)
            if len(value) > 32000:
                extended.append((index, value))
                value = ""
        clean.append(value)
    sheet.append(clean)
    for index, value in extended:
        reference = f"{sheet.title}!R{sheet.max_row}C{index}"
        long_text.extend((reference, part + 1, value[start:start + 32000]) for part, start in enumerate(range(0, len(value), 32000)))
        sheet.cell(sheet.max_row, index).value = "全文见「长文本」：" + reference + "\n" + value[:1000]
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"


def _table(book, title: str, columns: list, rows: list, long_text: list):
    from openpyxl.styles import Alignment, Font, PatternFill

    sheet = book.create_sheet(title)
    _append_cells(sheet, [label for _key, label, _width in columns], long_text)
    for row in rows:
        _append_cells(sheet, [row.get(key, "") for key, _label, _width in columns], long_text)
    for index, (_key, _label, width) in enumerate(columns, 1):
        cell = sheet.cell(1, index)
        cell.font, cell.fill = Font(color="FFFFFF", bold=True), PatternFill("solid", fgColor="1C5CAB")
        sheet.column_dimensions[cell.column_letter].width = width
    for row in sheet:
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    return sheet


def _turn_rows(snapshot: dict) -> list[dict]:
    rows = []
    for result in snapshot["results"]:
        rows.extend({"case_id": result["case_id"], "run_id": result["run_id"], "turn": turn.get("turn"),
                         "question": turn.get("question"), "answer": turn.get("answer"), "status": STATUS.get(turn.get("status"), turn.get("status")),
                         "business": turn.get("business_status"), "checks": turn.get("assertions", []),
                         "performance": turn.get("performance_assertions", []), "timings": turn.get("timings", {}),
                         "judgement": turn.get("judgement"), "reason": turn.get("reason", "")}
                    for turn in result.get("detail", {}).get("turns", []))
    return rows


def _task_sheets(book, snapshot: dict, rows: list[dict], long_text: list) -> None:
    columns = [(key, "本次结果" if key == "status_text" else label, width) for key, label, width in COLUMNS]
    columns += [("run_id", "运行标识", 26), ("business", "业务判断", 16), ("round", "验证轮次", 10), ("scenarios", "关联场景", 24), ("actual", "实际结果", 48)]
    _table(book, "用例与结果", columns, rows, long_text)
    _table(book, "执行明细", [("run_id", "运行标识", 26), ("case_id", "用例编号", 24), ("prerequisite", "自动前置", 10),
                           ("status", "执行结果", 14), ("reason", "原因", 42), ("detail", "原始结果与证据引用", 90)], snapshot["results"], long_text)
    turns = _turn_rows(snapshot)
    if turns:
        _table(book, "多轮对话", [("case_id", "所属用例", 24), ("run_id", "运行标识", 26), ("turn", "轮次", 8), ("question", "提问", 42),
                             ("answer", "实际回答", 55), ("status", "结果", 14), ("business", "业务判断", 16), ("checks", "独立预期与断言", 60),
                             ("performance", "性能断言", 38), ("timings", "响应时延", 38), ("judgement", "独立语义判断", 60), ("reason", "原因", 42)], turns, long_text)
    facts = [{"key": key, "value": value} for key, value in snapshot["sources"].items()]
    facts += [{"key": "用例定义：" + item["definition"]["id"], "value": item["definition"]} for item in snapshot["cases"]]
    _table(book, "来源与冻结定义", [("key", "来源", 32), ("value", "内容或证据引用", 110)], facts, long_text)


def write_task(snapshot: dict, destination: Path) -> dict:
    """Render frozen task facts with the existing built-in layout, separately from delivery state."""
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    long_text = []
    rows = [_task_row(item) for item in snapshot["cases"]]
    overview = [{"key": key, "value": value} for key, value in {
        "任务类型": snapshot["kind"], "任务标识": snapshot["task_id"], "项目": snapshot["project"]["name"],
        "任务标题": snapshot["title"], "任务状态": snapshot["status"], "渲染模板": "builtin-project-columns",
        "用例结果行数": len(rows), "独立用例数": len({row["id"] for row in rows}),
        "通过": sum(row["status"] == "Pass" for row in rows), "失败": sum(row["status"] == "Fail" for row in rows),
        "阻塞": sum(row["status"] == "Blocked" for row in rows), "未执行": sum(row["status"] is None for row in rows),
        "业务未验证": sum(row["status"] == "Unverified" for row in rows), "已取消": sum(row["status"] == "Cancelled" for row in rows),
        "范围与说明": snapshot["notes"], "任务元数据": snapshot["metadata"],
        "统计口径": "仅本任务冻结范围。重复验证每轮单列，保留早期失败；多轮对话属于同一用例，逐轮明细另列。未执行、业务未验证、阻塞和取消不算通过。",
    }.items()]
    _table(book, "任务概览", [("key", "项目", 24), ("value", "内容", 100)], overview, long_text)
    _task_sheets(book, snapshot, rows, long_text)
    if long_text:
        _table(book, "长文本", [("ref", "原单元格", 38), ("part", "片段顺序", 12), ("text", "全文片段", 110)],
               [{"ref": ref, "part": part, "text": text} for ref, part, text in long_text], [])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        book.save(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        _verify_task_workbook(temporary, rows)
        temporary.replace(destination)
    finally:
        book.close()
        temporary.unlink(missing_ok=True)
    return {"renderer": "builtin-project-columns", "template_id": "builtin-project-columns",
            "validation": {"workbook_readable": True, "case_rows": len(rows), "case_ids_match": True}}


def _verify_task_workbook(file: Path, expected: list[dict]) -> None:
    from openpyxl import load_workbook

    with file.open("rb") as stream:
        check = load_workbook(stream, read_only=True, data_only=False)
        try:
            sheet = check["用例与结果"]
            actual = list(sheet.iter_rows(min_row=2, max_col=1, values_only=True))
            if [row[0] for row in actual] != [row["id"] for row in expected]:
                raise ValueError("生成后的 Excel 用例范围核验失败，未交付文件")
        finally:
            check.close()
