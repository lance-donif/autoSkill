"""Archive failed and blocked replay runs as single-ZIP error packs.

Linux deployments archive failed runs into the configured error folder, and the dashboard
downloads the exact same single-ZIP error pack for any run with failed or blocked cases.
Passing runs write nothing and plaintext passwords are never written into error packs.
"""
from __future__ import annotations

import contextlib
import copy
import datetime
import json
import re
import uuid
import zipfile
from pathlib import Path

from . import data_refs, db, execution_inputs, paths, projects, runner, sync_conflicts

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class RunNotFoundError(ValueError):
    """Raised when a run id does not exist in the local database."""

    def __init__(self, run_id: str):
        super().__init__(f"运行 {run_id} 不存在")
        self.code = "not_found"


def _detail(row: dict) -> dict:
    value = row.get("detail")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _local_stamp(value: str | None) -> tuple[str, str]:
    """Return (YYYY-MM-DD, YYYYMMDD-HHMM) in local time for deterministic archive names."""
    parsed = None
    if value:
        with contextlib.suppress(ValueError):
            parsed = datetime.datetime.fromisoformat(value).astimezone()
    if parsed is None:
        parsed = datetime.datetime.now().astimezone()
    return parsed.strftime("%Y-%m-%d"), parsed.strftime("%Y%m%d-%H%M")


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = SAFE_NAME.sub("_", str(value or "")).strip("._")
    return cleaned or fallback


def _unique_zip_path(folder: Path, stem: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / f"{stem}.zip"
    if not candidate.exists():
        return candidate
    index = 2
    while (folder / f"{stem}-{index}.zip").exists():
        index += 1
    return folder / f"{stem}-{index}.zip"


def _screenshot_files(run_id: str, detail: dict) -> list[Path]:
    folder = runner.evidence_dir(run_id)
    return [folder / name for name in detail.get("screenshots") or []
            if isinstance(name, str) and Path(name).name == name and (folder / name).is_file()]


def _attempt_files(detail: dict) -> list[tuple[dict, Path | None]]:
    rows = []
    for ref in detail.get("attempts") or []:
        if not isinstance(ref, dict) or not ref.get("path"):
            continue
        resolved = None
        with contextlib.suppress(ValueError):
            candidate = data_refs.resolve(ref["path"])
            if candidate.is_file():
                resolved = candidate
        rows.append((ref, resolved))
    return rows


def _extract_account(run: dict, manifest: dict, rows: list[dict]) -> dict:
    """Resolve non-secret account metadata from frozen inputs, run metadata or shared store."""
    account = manifest.get("account") if isinstance(manifest.get("account"), dict) else {}
    if not account:
        for row in rows:
            detail_account = _detail(row).get("account")
            if isinstance(detail_account, dict) and (detail_account.get("account_id") or detail_account.get("label") or detail_account.get("username")):
                account = detail_account
                break
    account_id = account.get("id") or account.get("account_id") or run.get("account_id") or ""
    if account_id and not account.get("username"):
        stored = sync_conflicts.load_platform_store().get("accounts", {}).get(account_id)
        if isinstance(stored, dict):
            account = {**stored, **account}
    cleaned = sync_conflicts.strip_passwords(copy.deepcopy(account)) if account else {}
    if account_id and not cleaned.get("id"):
        cleaned["id"] = account_id
    if run.get("account_label") and not cleaned.get("label"):
        cleaned["label"] = run["account_label"]
    return cleaned


def _counts(run: dict, rows: list[dict]) -> dict:
    try:
        planned = json.loads(run.get("case_ids") or "[]")
    except (TypeError, ValueError):
        planned = []
    target_rows = [row for row in rows if not row.get("prerequisite")] or rows
    passed = sum(row["status"] == "Pass" for row in target_rows)
    failed = sum(row["status"] == "Fail" for row in target_rows)
    blocked = sum(row["status"] == "Blocked" for row in target_rows)
    executed = len(target_rows)
    total = max(int(run.get("total") or 0), len(planned) if isinstance(planned, list) else 0, executed)
    not_run = max(0, total - executed)
    return {
        "total": total,
        "executed": executed,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "not_run": not_run,
    }


def _collect_case_closure(failed_ids: list[str], manifest: dict, project_id: str) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Gather frozen definitions for failed cases plus their transitive prerequisites."""
    pool: dict[str, dict] = {}
    if isinstance(manifest.get("cases"), dict):
        pool.update({key: copy.deepcopy(value) for key, value in manifest["cases"].items() if isinstance(value, dict)})
    if not pool:
        with contextlib.suppress(ValueError):
            pool.update({item["id"]: item for item in projects.cases(project_id, active_only=False)})

    collected: dict[str, dict] = {}
    prereqs_by_case: dict[str, list[str]] = {}
    for case_id in failed_ids:
        if case_id in pool:
            collected[case_id] = sync_conflicts.strip_passwords(pool[case_id])
        seen = {case_id}
        queue = list(projects.depends_on(pool[case_id])) if case_id in pool else []
        deps: list[str] = []
        while queue:
            dep = queue.pop(0)
            if dep in seen or dep not in pool:
                continue
            seen.add(dep)
            deps.append(dep)
            collected[dep] = sync_conflicts.strip_passwords(pool[dep])
            queue.extend(projects.depends_on(pool[dep]))
        prereqs_by_case[case_id] = deps
    return collected, prereqs_by_case


def _markdown(manifest: dict) -> str:
    run = manifest["run"]
    environment = manifest.get("environment") or {}
    account = manifest.get("account") or {}
    counts = manifest["counts"]
    account_text = account.get("label") or account.get("username") or account.get("id") or "—"
    lines = [
        f"# 执行错误包：{manifest.get('project_name') or manifest['project_id']}",
        "",
        f"- 项目：{manifest['project_id']}",
        f"- 运行编号：{manifest['run_id']}（{run['status']}）",
        f"- 执行时间：{manifest.get('executed_at') or '—'}",
        f"- 结束时间：{manifest.get('finished_at') or '—'}",
        f"- 环境：{environment.get('name') or '项目地址'}"
        + (f"（{environment.get('base_url')}）" if environment.get("base_url") else ""),
        f"- 账号：{account_text}",
        (f"- 统计：总用例 {counts['total']} / 本次执行 {counts['executed']} / 通过 {counts['passed']}"
         f" / 失败 {counts['failed']} / 受阻 {counts['blocked']} / 未执行 {counts['not_run']}"),
        "",
    ]
    for item in manifest["cases"]:
        lines += [
            f"## {item['case_id']} {item['title']}",
            "",
            f"- 结果：{item['status']}" + (f"（{item['category']}）" if item["category"] else ""),
            f"- 原因：{item['reason'] or item['actual'] or '—'}",
        ]
        if item.get("case_file"):
            lines.append(f"- 用例定义：`{item['case_file']}`")
        if item.get("prerequisite_files"):
            lines.append("- 前置用例：" + "、".join(f"`{path}`" for path in item["prerequisite_files"]))
        for check in item["failed_checks"]:
            lines.append(f"- 断言 {check['name']}：期望 {check['expected']}，实际 {check['actual']}")
        for path in item["screenshots"]:
            lines.append(f"- 截图：`{path}`")
        for attempt in item["attempts"]:
            if attempt.get("path"):
                lines.append(f"- 执行回执：`{attempt['path']}`")
        if item.get("log_file"):
            lines.append(f"- 详细日志：`{item['log_file']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _prepare_pack_data(run_id: str) -> tuple[dict, dict[str, dict], dict[str, bytes], dict[str, dict]]:
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise RunNotFoundError(run_id)
    if run.get("family") not in (None, "replay"):
        raise ValueError("仅回放运行支持导出错误包")
    rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,))
    failed = [row for row in rows if row["status"] in ("Fail", "Blocked")]
    if not failed:
        raise ValueError("本次运行没有失败或受阻用例，无需生成错误包")

    try:
        frozen = execution_inputs.load(run)
    except (OSError, ValueError):
        frozen = {}
    environment = sync_conflicts.strip_passwords(copy.deepcopy(frozen.get("environment") or {}))
    if not environment and run.get("environment_id"):
        environment = {"id": run["environment_id"], "name": run.get("environment_name") or run["environment_id"]}
    account = _extract_account(run, frozen, rows)
    project_row = db.one("SELECT name FROM projects WHERE id = ?", (run["project_id"],))
    project_name = project_row["name"] if project_row else run["project_id"]

    failed_ids = list(dict.fromkeys(row["case_id"] for row in failed))
    collected_cases, prereqs_by_case = _collect_case_closure(failed_ids, frozen, run["project_id"])
    binary_assets: dict[str, bytes] = {}
    logs: dict[str, dict] = {}
    case_entries: list[dict] = []

    for row in failed:
        case_id = row["case_id"]
        detail = _detail(row)
        failed_checks = [{"name": check.get("name"), "expected": check.get("expected"), "actual": check.get("actual")}
                         for check in detail.get("assertions") or [] if check.get("passed") is False]
        response = detail.get("response")
        shot_rels: list[str] = []
        for shot in _screenshot_files(run_id, detail):
            rel = f"evidence/screenshots/{shot.name}"
            binary_assets[rel] = shot.read_bytes()
            shot_rels.append(rel)

        attempt_entries: list[dict] = []
        for idx, (ref, resolved) in enumerate(_attempt_files(detail), 1):
            attempt_id = _safe_segment(str(ref.get("id") or f"attempt-{idx}"), f"attempt-{idx}")
            rel = f"evidence/attempts/{attempt_id}.json"
            if resolved is not None:
                try:
                    attempt_data = sync_conflicts.strip_passwords(json.loads(resolved.read_bytes()))
                    binary_assets[rel] = (json.dumps(attempt_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                except (OSError, ValueError):
                    binary_assets[rel] = resolved.read_bytes()
            attempt_entries.append({
                "id": ref.get("id"),
                "kind": ref.get("kind"),
                "status": ref.get("status"),
                "business_status": ref.get("business_status"),
                "path": rel if resolved is not None else "",
            })

        log_name = _safe_segment(case_id, "case")
        log_rel = f"evidence/logs/{log_name}.json"
        sanitized_detail = sync_conflicts.strip_passwords(detail)
        logs[log_rel] = {
            "case_id": case_id,
            "title": row["case_title"],
            "status": row["status"],
            "category": row.get("category", ""),
            "reason": row.get("reason", ""),
            "actual": row.get("actual", ""),
            "started_at": row.get("started_at"),
            "duration_ms": row.get("duration_ms"),
            "detail": sanitized_detail,
        }

        case_rel = f"cases/{_safe_segment(case_id, 'case')}.json" if case_id in collected_cases else ""
        prereq_rels = [f"cases/{_safe_segment(dep, 'dep')}.json" for dep in prereqs_by_case.get(case_id, []) if dep in collected_cases]
        case_account = sync_conflicts.strip_passwords(detail.get("account")) if isinstance(detail.get("account"), dict) else account
        case_entries.append({
            "case_id": case_id,
            "title": row["case_title"],
            "kind": row["kind"],
            "engine": row["engine"],
            "status": row["status"],
            "category": row.get("category", ""),
            "reason": row.get("reason", ""),
            "actual": row.get("actual", ""),
            "started_at": row.get("started_at"),
            "duration_ms": row.get("duration_ms"),
            "prerequisite": bool(row.get("prerequisite")),
            "case_file": case_rel,
            "prerequisite_files": prereq_rels,
            "failed_checks": failed_checks,
            "request": sync_conflicts.strip_passwords(detail.get("request")),
            "response": (sync_conflicts.strip_passwords({key: response.get(key) for key in ("status", "headers", "body", "bytes")})
                         if isinstance(response, dict) else None),
            "screenshots": shot_rels,
            "attempts": attempt_entries,
            "log_file": log_rel,
            "account": case_account,
        })

    manifest = {
        "schema": 2,
        "kind": "error_pack",
        "generated_at": db.now(),
        "project_id": run["project_id"],
        "project_name": project_name,
        "run_id": run["id"],
        "executed_at": run.get("started_at") or run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "run": {key: run.get(key) for key in ("id", "project_id", "engine", "status", "trigger", "created_at", "started_at", "finished_at", "reason")},
        "counts": _counts(run, rows),
        "environment": environment,
        "account": account,
        "cases": case_entries,
    }
    return manifest, collected_cases, binary_assets, logs


def _write_error_zip(target: Path, manifest: dict, collected_cases: dict[str, dict], binary_assets: dict[str, bytes], logs: dict[str, dict]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("errors.md", _markdown(manifest))
            for case_id, definition in sorted(collected_cases.items()):
                archive.writestr(f"cases/{_safe_segment(case_id, 'case')}.json",
                                 json.dumps(definition, ensure_ascii=False, indent=2) + "\n")
            for rel, data in sorted(binary_assets.items()):
                archive.writestr(rel, data)
            for rel, payload in sorted(logs.items()):
                archive.writestr(rel, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def build_zip(run_id: str, *, force: bool = False) -> Path:
    """Build or reuse this run's cached single-ZIP error pack for dashboard download and archiving."""
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise RunNotFoundError(run_id)
    _day, minute = _local_stamp(run.get("finished_at") or run.get("created_at"))
    suffix = _safe_segment(run_id[-6:], "run")
    stem = f"{run['project_id']}-error-{minute}-{suffix}"
    cached = paths.local() / "error-packs" / run_id / f"{stem}.zip"
    if not force and cached.is_file():
        with contextlib.suppress(zipfile.BadZipFile, OSError), zipfile.ZipFile(cached) as archive:
            if archive.testzip() is None and "manifest.json" in archive.namelist():
                return cached
    manifest, collected_cases, binary_assets, logs = _prepare_pack_data(run_id)
    return _write_error_zip(cached, manifest, collected_cases, binary_assets, logs)


def has_error_pack(run_id: str) -> bool:
    """Return True when the replay run has at least one Fail or Blocked result."""
    run = db.one("SELECT family FROM runs WHERE id = ?", (run_id,))
    if not run or run.get("family") not in (None, "replay"):
        return False
    row = db.one("SELECT 1 AS hit FROM results WHERE run_id = ? AND status IN ('Fail', 'Blocked') LIMIT 1", (run_id,))
    return bool(row)


def for_run(run_id: str) -> dict | None:
    """Write this run's single-ZIP error pack to AUTOSKILL_ERROR_DIR; returns None when nothing failed."""
    folder = paths.error_dir()
    if folder is None:
        return None
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run or run.get("family") not in (None, "replay"):
        return None
    rows = db.rows("SELECT status FROM results WHERE run_id = ?", (run_id,))
    failed_count = sum(row["status"] in ("Fail", "Blocked") for row in rows)
    if not failed_count:
        return None
    cached = build_zip(run_id, force=True)
    day, _minute = _local_stamp(run.get("finished_at") or run.get("created_at"))
    target = _unique_zip_path(folder / day, cached.stem)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(cached.read_bytes())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"file": str(target), "filename": target.name, "cached": str(cached), "cases": failed_count}
