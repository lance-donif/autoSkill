"""Excel workbook and Markdown summary for a project: its cases and what the latest replay of each showed."""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from . import db, paths, projects
from .replay import ir

STATUS = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", None: "未执行"}
FILL = {"Pass": "E2F0D9", "Fail": "FCE4D6", "Blocked": "FFF2CC", None: "EDEDED"}
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
        stale = bool(result) and result["case_revision"] != case["revision"]
        status = None if not result or stale else result["status"]
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
            "status": status, "status_text": STATUS[status] + ("（用例已修改，需重新回放）" if stale else ""),
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
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")  # noqa: DTZ005  # Report filenames use the machine's local wall clock.
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
