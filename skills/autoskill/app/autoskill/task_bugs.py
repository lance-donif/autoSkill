"""List observed issues from frozen task facts without turning execution blockers into product bugs."""
from __future__ import annotations

import json
from pathlib import Path

from . import task_markdown, task_report_data

KINDS = {"business": "业务断言失败（待定位）", "execution": "执行或性能问题", "unverified": "失败记录（证据待核验）",
         "recording": "录制时的问题线索", "historical": "历史失败（执行证据已删除）"}


def _value(value) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def _text(value, limit=600) -> str:
    if value is None or value == "":
        return "未记录"
    original = _value(value)
    shown = task_markdown._text(original, limit)
    return shown + ("（完整内容见同轮 Excel）" if len(original) > limit else "")


def _classification(item: dict) -> str | None:
    result = item.get("result") or {}
    historical = item.get("historical_result") or {}
    if historical.get("status") == "Fail" or historical.get("business_status") == "Fail":
        return "historical"
    business = result.get("detail", {}).get("business_status")
    if result and task_report_data.observation_verdict(item) == "Fail":
        if not item.get("evidence_valid"):
            return "unverified"
        return "business" if business == "Fail" else "execution"
    if result.get("status") == "Blocked":
        return "execution"
    return "recording" if (item.get("recording_result") or {}).get("status") == "Fail" else None


def _entries(detail: dict, key: str) -> list:
    value = detail.get(key)
    return value if isinstance(value, list) else []


def _failed_checks(detail: dict) -> list[dict]:
    checks = []
    for key in ("assertions", "performance_assertions"):
        checks.extend(check for check in _entries(detail, key) if isinstance(check, dict) and check.get("passed") is False)
    for key in ("turns", "attempts"):
        for item in _entries(detail, key):
            if isinstance(item, dict):
                checks.extend(_failed_checks(item.get("outcome") or item))
    unique = {json.dumps(check, ensure_ascii=False, sort_keys=True): check for check in checks}
    return list(unique.values())


def _observation(item: dict, kind: str) -> dict:
    preferred = {"historical": "historical_result", "recording": "recording_result"}.get(kind, "result")
    result = item.get(preferred) or {}
    detail = result.get("detail") or result
    checks = _failed_checks(detail)
    screenshots = detail.get("screenshots") or result.get("screenshots") or []
    return {"run_id": item.get("run_id"), "status": result.get("status"), "reason": result.get("reason") or item.get("note"),
            "actual": result.get("actual"), "checks": checks, "evidence_valid": bool(item.get("evidence_valid")),
            "category": result.get("category"), "screenshots": screenshots if isinstance(screenshots, list) else [screenshots],
            "attempts": [attempt for attempt in _entries(detail, "attempts") if isinstance(attempt, dict)]}


def issues(snapshot: dict) -> list[dict]:
    """Keep every failing observation and later result within its original case version."""
    found = []
    for group in task_report_data.logical_cases(snapshot):
        observed = [(item, _classification(item) or ("execution" if not item.get("result") and snapshot.get("status") == "Blocked" else None))
                    for item in group["items"]]
        failing = [(item, kind) for item, kind in observed if kind]
        if not failing:
            continue
        kinds = list(dict.fromkeys(kind for _, kind in failing))
        found.append({"id": f"BUG-{len(found) + 1:03d}", "case_id": group["definition"]["id"],
                      "title": group["definition"].get("title"), "definition_hash": group["definition_hash"],
                      "definition": group["definition"], "kinds": kinds, "final_status": group["final_status"],
                      "observations": [_observation(item, kind) for item, kind in failing],
                      "run_ids": group["run_ids"], "verdicts": [task_report_data.observation_verdict(item) for item in group["items"]]})
    return found


def _expected(issue: dict, checks: list[dict]):
    declared = [{"检查": check.get("name", "未命名检查"), "预期": check.get("expected", "未记录")} for check in checks]
    if declared:
        return declared
    definition = issue["definition"]
    for key in ("expected", "assertions"):
        if key in definition:
            return definition[key]
    return "未记录独立预期；不能从实际结果反推预期"


def _reproduction(issue: dict) -> str:
    definition = issue["definition"]
    if definition.get("method") and definition.get("path"):
        return str(definition["method"]) + " " + str(definition["path"]) + "；参数及前置条件见同轮 Excel 中的冻结定义"
    return "按同轮 Excel 中本用例版本的步骤、前置条件和对话轮次复现；未提供的步骤不能由运行状态补造"


def _issue_lines(issue: dict) -> list[str]:
    checks = [check for observed in issue["observations"] for check in observed["checks"]]
    reasons = list(dict.fromkeys(str(item["reason"]) for item in issue["observations"] if item["reason"]))
    actual = [{"检查": check.get("name", "未命名检查"), "实际": check.get("actual", "未记录")} for check in checks]
    if not actual:
        actual = [item["actual"] for item in issue["observations"] if item["actual"] is not None]
    lines = ["", "## " + issue["id"] + " · " + _text(issue["title"], 160), "",
             "- 类型：" + "、".join(KINDS[kind] for kind in issue["kinds"]),
             "- 用例：" + _text(issue["case_id"]) + "；冻结定义 SHA-256：" + issue["definition_hash"],
             "- 问题摘要：" + _text("；".join(reasons) or "失败检查见下方预期与实际；根因尚未定位"),
             "- 预期：" + _text(_expected(issue, checks)), "- 实际：" + _text(actual or reasons or "未记录"),
             "- 复现线索：" + _text(_reproduction(issue)),
             "- 执行历史：" + " → ".join(task_markdown.LABELS[verdict] for verdict in issue["verdicts"]),
             "- 证据：运行 " + _text("、".join(issue["run_ids"])) + "；详细步骤、检查和证据引用见同轮 Excel 的执行明细"]
    screenshots = [str(path) for item in issue["observations"] for path in item["screenshots"]]
    if screenshots:
        lines.append("- 截图引用：" + _text("、".join(dict.fromkeys(screenshots))))
    if issue["final_status"] == "Pass":
        lines.append("- 后续复测：通过；本轮此前失败仍保留")
    return lines


def render(snapshot: dict, *, excel_link: str) -> tuple[str, dict]:
    summary = task_report_data.task_summary(snapshot)
    found = issues(snapshot)
    counts = {kind: sum(kind in issue["kinds"] for issue in found) for kind in KINDS}
    project = snapshot.get("project", {})
    metadata = snapshot.get("metadata", {})
    lines = ["# Bug 清单", "", "- 项目：" + _text(project.get("name") or snapshot.get("project_id")),
             "- 任务：" + _text(snapshot["task_id"]) + "；轮次：" + task_markdown._rounds(snapshot),
             "- 时间：" + _text(metadata.get("finished_at") or metadata.get("updated_at") or metadata.get("created_at")),
             "- 环境：" + _text("；".join(summary["environment"])),
             "- 任务状态：" + _text(task_markdown.LABELS.get(snapshot.get("status"), snapshot.get("status"))),
             "- 任务说明：" + _text(metadata.get("reason")),
             "", "| 分类 | 涉及用例版本数 |", "| --- | ---: |"]
    lines.extend(f"| {label} | {counts[kind]} |" for kind, label in KINDS.items())
    lines.extend(["", f"本轮共 {summary['cases']} 个用例版本、{summary['execution_rows']} 条结果；问题按用例版本归组，不表示同一根因。",
                  "问题类型区分业务断言、执行环境和录制观察。"])
    if not found:
        lines.append("本轮没有记录到可列出的失败或阻塞问题；这不等于全部需求均已验证。")
    for issue in found:
        lines.extend(_issue_lines(issue))
    incomplete = [group for group in task_report_data.logical_cases(snapshot) if group["status"] == "NotRun"]
    if incomplete:
        lines.extend(["", "## 未执行用例", "", "| 用例 | 状态 | 说明 |", "| --- | --- | --- |"])
        for group in incomplete:
            reasons = list(dict.fromkeys(item.get("note") or (item.get("result") or {}).get("reason") or "" for item in group["items"]))
            lines.append("| " + _text(group["definition"]["id"]) + " | " + task_markdown.LABELS[group["status"]] + " | " + _text("；".join(reasons)) + " |")
    lines.extend(["", f"[查看同轮 Excel 与完整证据引用]({excel_link})", ""])
    return "\n".join(lines), {"issues": len(found), "counts": counts}


def write_task(snapshot: dict, destination: Path, *, excel_link: str) -> dict:
    text, summary = render(snapshot, excel_link=excel_link)
    task_markdown.write_text(destination, text)
    return {"bugs_validation": {"bugs_readable": True}, "bugs_count": summary["issues"], "bugs_counts": summary["counts"]}
