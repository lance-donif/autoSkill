"""Archive failed and blocked replay runs into the configured error folder.

Linux deployments watch that folder for alerts; everything stays in the data directory except
these copies. A run with no failed or blocked target writes nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import data_refs, db, execution_inputs, paths, runner


def _detail(row: dict) -> dict:
    value = row.get("detail")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _screenshots(run_id: str, detail: dict) -> list[str]:
    folder = runner.evidence_dir(run_id)
    return [str(folder / name) for name in detail.get("screenshots") or []
            if isinstance(name, str) and Path(name).name == name and (folder / name).is_file()]


def _attempts(detail: dict) -> list[dict]:
    rows = []
    for ref in detail.get("attempts") or []:
        if not isinstance(ref, dict) or not ref.get("path"):
            continue
        try:
            path = str(data_refs.resolve(ref["path"]))
        except ValueError:
            path = str(ref["path"])
        rows.append({"id": ref.get("id"), "kind": ref.get("kind"), "status": ref.get("status"),
                     "business_status": ref.get("business_status"), "path": path})
    return rows


def _case(row: dict, run_id: str) -> dict:
    detail = _detail(row)
    failed_checks = [{"name": check.get("name"), "expected": check.get("expected"), "actual": check.get("actual")}
                     for check in detail.get("assertions") or [] if check.get("passed") is False]
    response = detail.get("response")
    return {"case_id": row["case_id"], "title": row["case_title"], "kind": row["kind"], "engine": row["engine"],
            "status": row["status"], "category": row.get("category", ""), "reason": row.get("reason", ""),
            "actual": row.get("actual", ""), "started_at": row.get("started_at"), "duration_ms": row.get("duration_ms"),
            "prerequisite": bool(row.get("prerequisite")), "failed_checks": failed_checks,
            "request": detail.get("request"),
            "response": {key: response.get(key) for key in ("status", "headers", "body", "bytes")} if isinstance(response, dict) else None,
            "screenshots": _screenshots(run_id, detail), "attempts": _attempts(detail)}


def _markdown(report: dict) -> str:
    run = report["run"]
    environment = report.get("environment") or {}
    lines = [f"# 执行失败归档：{run['project_id']}", "",
             f"- 运行：{run['id']}（{run['status']}）",
             f"- 结束时间：{run.get('finished_at') or '—'}",
             f"- 环境：{environment.get('name') or '项目地址'}"
             + (f"（{environment.get('base_url')}）" if environment.get("base_url") else ""),
             f"- 用例：共 {report['counts']['total']} 条，失败 {report['counts']['failed']}，受阻 {report['counts']['blocked']}",
             ""]
    for item in report["cases"]:
        lines += [f"## {item['case_id']} {item['title']}", "",
                  f"- 结果：{item['status']}" + (f"（{item['category']}）" if item["category"] else ""),
                  f"- 原因：{item['reason'] or item['actual'] or '—'}"]
        for check in item["failed_checks"]:
            lines.append(f"- 断言 {check['name']}：期望 {check['expected']}，实际 {check['actual']}")
        for path in item["screenshots"]:
            lines.append(f"- 截图：{path}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_text(file: Path, text: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name("." + file.name + "." + db.new_id("tmp") + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(file)
    finally:
        temporary.unlink(missing_ok=True)


def for_run(run_id: str) -> dict | None:
    """Write this run's failure archive; returns its receipt, or None when nothing failed."""
    folder = paths.error_dir()
    if folder is None:
        return None
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run or run.get("family") not in (None, "replay"):
        return None
    rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,))
    failed = [row for row in rows if row["status"] in ("Fail", "Blocked")]
    if not failed:
        return None
    try:
        environment = execution_inputs.load(run).get("environment") or {}
    except (OSError, ValueError):
        environment = {}
    stamp = (run.get("finished_at") or db.now())[:10]
    target = folder / stamp / run["project_id"] / run_id
    report = {"schema": 1, "kind": "replay_failure", "generated_at": db.now(),
              "run": {key: run.get(key) for key in ("id", "project_id", "engine", "status", "trigger", "created_at", "finished_at", "reason")},
              "environment": environment,
              "counts": {"total": len(rows), "failed": sum(row["status"] == "Fail" for row in rows),
                         "blocked": sum(row["status"] == "Blocked" for row in rows)},
              "cases": [_case(row, run_id) for row in failed]}
    json_file, markdown_file = target / "errors.json", target / "errors.md"
    execution_inputs.write_json(json_file, report)
    _write_text(markdown_file, _markdown(report))
    return {"folder": str(target), "json": str(json_file), "markdown": str(markdown_file), "cases": len(failed)}
