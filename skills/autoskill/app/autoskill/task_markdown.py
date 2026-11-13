"""Render a short task summary from the same immutable facts as the workbook."""
from __future__ import annotations

import html
import os
import re
import uuid
from pathlib import Path

from . import task_report_data

LABELS = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", "NotRun": "未执行", "Unknown": "未知",
          "Unverified": "业务未验证", "Cancelled": "已取消"}


def _text(value, limit: int = 160) -> str:
    if value is None or value == "":
        return "未记录"
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    if len(text) > limit:
        text = text[:limit] + "…"
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", text)


def _rounds(snapshot: dict) -> str:
    if snapshot["kind"] == "recording":
        return "录制定义，尚未回放"
    rounds = sorted({str(item["validation_round"]) for item in snapshot.get("cases", []) if item.get("validation_round") is not None})
    values = []
    if snapshot.get("cycle_id"):
        values.append("周期 " + _text(snapshot["cycle_id"]))
    if rounds:
        values.append("验证轮次 " + _text("、".join(rounds)))
    return "；".join(values) or "本次运行"


def render(snapshot: dict, *, excel_link: str) -> tuple[str, dict]:
    """Keep counts and issue classification shared with the Excel renderer, without detailed dumps."""
    summary = task_report_data.task_summary(snapshot)
    metadata = snapshot.get("metadata", {})
    project = snapshot.get("project", {})
    started = metadata.get("started_at") or metadata.get("created_at")
    finished = metadata.get("finished_at") or metadata.get("updated_at")
    environment = summary.get("environment") or []
    lines = ["# 测试任务简报", "", "- 项目：" + _text(project.get("name") or snapshot.get("project_id")),
             "- 任务：" + _text(snapshot.get("title")) + "（" + _text(snapshot["task_id"]) + "）",
             "- 轮次：" + _rounds(snapshot), "- 时间：" + _text(started) + " → " + _text(finished),
             "- 环境：" + (_text("；".join(str(value) for value in environment), 240) if environment else "未记录"),
             "- 执行/流程状态：" + _text(snapshot.get("status")), "", "## 结果汇总", "",
             f"逻辑用例 {summary['cases']} 条；执行结果行 {summary['execution_rows']} 条。", "",
             "| 结果 | 数量 |", "| --- | ---: |"]
    lines.extend(f"| {label} | {summary['counts'].get(status, 0)} |" for status, label in LABELS.items())
    lines.extend(["", "## 主要问题", ""])
    issues = summary.get("issues") or []
    for issue in issues[:5]:
        reason = str(issue.get("reason") or "具体原因见 Excel").strip().splitlines()[0]
        if reason.startswith(("{", "[")):
            reason = "原因明细见 Excel"
        lines.append("- " + _text(issue.get("case_id")) + "：" + _text(LABELS.get(issue.get("status"), issue.get("status"))) + "；" + _text(reason))
    if len(issues) > 5:
        lines.append(f"- 另有 {len(issues) - 5} 项问题，详见 Excel。")
    if not issues:
        lines.append("本次汇总没有列出的主要问题；未执行、未知和业务未验证不代表通过。")
    lines.extend(["", f"[查看完整 Excel]({excel_link})", ""])
    return "\n".join(lines), summary


def write_task(snapshot: dict, destination: Path, *, excel_link: str) -> dict:
    text, summary = render(snapshot, excel_link=excel_link)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_text(encoding="utf-8") != text:
            raise ValueError("生成后的 Markdown 读回核验失败，未交付文件")
        temporary.replace(destination)
        if destination.read_text(encoding="utf-8") != text:
            raise ValueError("Markdown 保存后内容不一致，未交付文件")
    finally:
        temporary.unlink(missing_ok=True)
    return {"markdown_validation": {"markdown_readable": True}, "summary_counts": summary["counts"],
            "summary_cases": summary["cases"], "summary_execution_rows": summary["execution_rows"]}
