"""One read-only projection of execution, checks, evidence and delivery state.

Stored verdicts remain historical facts. A current projection cannot claim a
verified pass when its evidence is unavailable or its business checks are absent.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from . import data_refs, db, execution_inputs, paging, paths

_lock = threading.RLock()
_watched: dict[str, set[Path]] = {}
_checks: dict[str, tuple[tuple, bool]] = {}
_projection = ContextVar("task_state_projection", default=None)


@contextmanager
def projection():
    """Reuse path resolution and parsed artifacts only within one read projection."""
    if _projection.get() is not None:
        yield
        return
    token = _projection.set({"root": paths.workspace(), "paths": {}, "json": {}, "reports": {}})
    try:
        yield
    finally:
        _projection.reset(token)


def _projected(function):
    @wraps(function)
    def read(*args, **kwargs):
        with projection():
            return function(*args, **kwargs)
    return read


def _root() -> Path:
    current = _projection.get()
    return current["root"] if current is not None else paths.workspace()


def _local() -> Path:
    return _root() / ".local"


def _resolve(value) -> Path:
    current = _projection.get()
    if current is None:
        return data_refs.resolve(value)
    key = str(value)
    if key not in current["paths"]:
        current["paths"][key] = data_refs.resolve(value, root=current["root"])
    return current["paths"][key]


def _watch(file: Path) -> Path:
    with _lock:
        _watched.setdefault(str(_root()), set()).add(file)
    return file


def _stamp(file: Path) -> tuple:
    try:
        stat = file.stat()
        return str(file), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except OSError:
        return str(file), None


def watch_json(file: Path) -> dict:
    """Read an observed artifact and register its metadata for revision polling."""
    _watch(file)
    current = _projection.get()
    key = _stamp(file)
    if current is not None and key in current["json"]:
        return current["json"][key]
    value = json.loads(file.read_bytes())
    result = value if isinstance(value, dict) else {}
    if current is not None:
        current["json"][key] = result
    return result


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value or "{}")
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):
        return {}


def _rows(connection, sql: str, args: tuple = ()) -> list[dict]:
    if connection is None:
        with db.connect() as current:
            return _rows(current, sql, args)
    return [dict(row) for row in connection.execute(sql, args)]


def _has(connection, name: str) -> bool:
    return connection is not None and bool(_rows(connection, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)))


def _scope(row: dict, connection=None) -> dict:
    scope = {key: row.get(key) for key in ("project_id", "workflow_id", "cycle_id", "run_id", "case_id", "attempt_id")}
    run_id = scope["run_id"]
    if run_id and connection is not None and _has(connection, "workflow_items"):
        found = _rows(connection, "SELECT i.workflow_id,i.cycle_id,i.attempt_id,f.cycle_id current_cycle_id FROM workflow_items i "
                      "JOIN workflow_runtime f ON f.id=i.workflow_id WHERE i.run_id = ? ORDER BY i.sequence DESC LIMIT 1", (run_id,))
        if found:
            scope.update({key: value for key, value in found[0].items() if key != "attempt_id"})
            scope["workflow_attempt_id"] = found[0]["attempt_id"]
            scope["historical"] = scope["cycle_id"] != scope["current_cycle_id"]
    return scope


def _state(lifecycle: str, phase: str, outcome: str, *, scope: dict, reason: str = "", **values) -> dict:
    state = {"lifecycle": lifecycle, "phase": phase, "outcome": outcome,
             "checks": {"business": "unverified", "performance": "not_applicable", "quality": "not_applicable"},
             "evidence": "unverified", "report": "not_generated", "delivery": "not_applicable",
             "scope": scope, "reason": reason or "", **values}
    state["revision"] = paging.revision(state)
    return state


def watch_current(project_id: str) -> None:
    """Observe live definitions separately from immutable execution evidence."""
    folder = paths.projects_dir() / project_id
    _watch(paths.local() / "credentials.json")
    for file in (folder, folder / "project.json", folder / "qa-config.json", folder / "auth.json", folder / "testcases", folder / "domain" / "HEAD.json"):
        _watch(file)
    for file in folder.glob("*.json"):
        _watch(file)
    for file in (folder / "testcases").glob("*.json"):
        _watch(file)
    for directory in (folder / "analysis", folder / "sources", folder / "inputs"):
        _watch(directory)
        if directory.is_dir():
            for file in directory.rglob("*"):
                _watch(file)


@_projected
def current_result(project_id: str, case: dict | None, row: dict, *, connection=None) -> dict:
    """Qualify current case validation without rewriting its historical run verdict."""
    historical = result(row, connection=connection)
    watch_current(project_id)
    current = bool(case) and execution_inputs.result_current(project_id, case, row)
    value = {**historical, "current": current, "historical_outcome": historical["outcome"]}
    if not current and value["outcome"] == "pass":
        value.update(outcome="unverified", reason="当前用例、依赖或执行配置已变化；历史执行事实保留")
    value["revision"] = paging.revision({key: item for key, item in value.items() if key != "revision"})
    return value


def _axis(value, *, default: str = "unverified") -> str:
    return {"Pass": "pass", "Fail": "fail", "Unverified": "unverified", "pass": "pass", "fail": "fail"}.get(value, default) if isinstance(value, str) else default


def _object_rows(value) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _aggregate(values: list[str], default: str = "unverified") -> str:
    if "fail" in values:
        return "fail"
    applicable = [value for value in values if value != "not_applicable"]
    if not applicable:
        return default
    return "pass" if all(value == "pass" for value in applicable) else "unverified"


@_projected
def report(report_id: str, *, run_row: dict | None = None) -> dict:
    """Verify report artifacts without generating or retrying an export."""
    from . import task_reports

    try:
        folder = task_reports._folder(report_id)
        _watch(_local() / "task-report-locations" / (report_id + ".json"))
        _watch(folder / "receipt.json")
        if report_id.startswith("run_"):
            run_row = run_row if run_row is not None else db.one("SELECT status,delete_requested FROM runs WHERE id=?", (report_id.removeprefix("run_"),))
            if not run_row or run_row.get("delete_requested"):
                return {"report_id": report_id, "status": "skipped", "retryable": False, "reason": "运行已删除或正在删除"}
            if run_row.get("status") not in db.FINISHED:
                return {"report_id": report_id, "status": "pending", "retryable": False, "reason": "运行尚未结束"}
        saved = watch_json(folder / "receipt.json")
        if saved.get("report_id") != report_id:
            return {"report_id": report_id, "status": "failed", "retryable": True, "error": "报告收据身份不匹配"}
        files = [folder / "receipt.json"]
        files.extend(_watch(_resolve(saved[key])) for key in ("excel", "markdown", "bugs") if saved.get(key))
        current = _projection.get()
        cache_key = (report_id, tuple(_stamp(file) for file in files))
        if cache_key not in current["reports"]:
            current["reports"][cache_key] = task_reports._validate(saved)
        return dict(current["reports"][cache_key])
    except FileNotFoundError:
        return {"report_id": report_id, "status": "not_generated", "retryable": bool(run_row),
                "retry_url": "/api/task-reports/" + report_id + "/retry"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"report_id": report_id, "status": "failed", "retryable": True, "error": "报告文件或收据无法核验"}


def _evidence(run_row: dict, row: dict, detail: dict) -> bool:
    files = set()
    try:
        files.add(_resolve(run_row["input_manifest"]))
        manifest = watch_json(_resolve(run_row["input_manifest"]))
        documents = [manifest]
        if run_row.get("family") == "native":
            from . import native_run, task_report_data

            manifest = native_run._frozen(run_row, manifest=manifest)
            folder = _local() / "runs" / run_row["id"] / "native"
            outcome = watch_json(folder / "outcome.json")
            documents.append(outcome)
            files.add(folder / "outcome.json")
            files.update(_local() / "native-inputs" / "objects" / ref["sha256"] for ref in manifest.get("frozen_files", []))
            files.update(_resolve(ref["path"]) if data_refs.is_absolute(ref["path"]) else folder / ref["path"] for ref in outcome.get("evidence", []))
            def verify():
                return task_report_data._native_evidence(run_row, manifest, outcome, [{**row, "detail": detail}])
        else:
            files.update(_local() / "execution-inputs" / "objects" / ref["sha256"] for ref in manifest.get("sources", {}).get("files", {}).values())
            for attempt in detail.get("attempts", []):
                receipt_path = _resolve(attempt["path"])
                files.add(receipt_path)
                receipt = watch_json(receipt_path)
                documents.append(receipt)
                files.update(_local() / "runs" / run_row["id"] / artifact["path"] for artifact in receipt.get("artifacts", []))
            def verify():
                return execution_inputs.result_evidence(run_row, {**row, "detail": detail})
        for file in files:
            _watch(file)
        signature = tuple(sorted(_stamp(file) for file in files))
        # Bind cached proof to the contents actually read, even if a writer
        # replaces an artifact before its metadata signature is sampled.
        key = paging.revision([str(_root()), run_row.get("input_hash"), row, documents])
        with _lock:
            cached = _checks.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        valid = bool(verify())
        if signature != tuple(sorted(_stamp(file) for file in files)):
            return valid
        with _lock:
            if len(_checks) >= 4096:
                _checks.clear()
            _checks[key] = (signature, valid)
        return valid
    except (OSError, KeyError, ValueError, TypeError):
        return False


@_projected
def result(row: dict, detail=None, *, connection=None) -> dict:
    """Project a case result, retaining failed attempts independently of the last one."""
    if connection is None:
        with db.connect() as current:
            return result(row, detail, connection=current)
    row = dict(row)
    details = _json(row.get("detail") if detail is None else detail)
    run_rows = _rows(connection, "SELECT * FROM runs WHERE id = ?", (row.get("run_id"),))
    attempts = details.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    if attempts and isinstance(attempts[-1], dict):
        row["attempt_id"] = attempts[-1].get("id")
    business = _axis(details.get("business_status"))
    if any(_axis(attempt.get("business_status")) == "fail" for attempt in attempts if isinstance(attempt, dict)):
        business = "fail"
    performance_rows = _object_rows(details.get("performance_assertions"))
    performance_rows += [item for item in _object_rows(details.get("assertions")) if item.get("scope") == "performance" or item.get("source") == "performance_target"]
    for turn in _object_rows(details.get("turns")):
        performance_rows.extend(_object_rows(turn.get("performance_assertions")))
    if details.get("verdict_scope") == "performance":
        performance_rows.extend(_object_rows(details.get("assertions")))
    performance = _axis(details.get("performance_status"), default="not_applicable") if details.get("performance_status") != "Unverified" else "not_applicable"
    if not performance_rows and details.get("performance_status") == "Unverified" and run_rows and run_rows[0].get("family") != "native":
        try:
            manifest = watch_json(_resolve(run_rows[0]["input_manifest"]))
            frozen = manifest.get("cases", {}).get(row.get("case_id"), {})
            if "max_ms" in frozen.get("assertions", {}):
                performance = "unverified"
        except (OSError, ValueError, KeyError, TypeError):
            performance = "unverified"
    if performance_rows:
        performance = "fail" if any(item.get("passed") is False for item in performance_rows) else "pass" if all(item.get("passed") is True for item in performance_rows) else "unverified"
    if performance == "not_applicable" and details.get("performance_status") == "Unverified" and performance_rows:
        performance = "unverified"
    quality = _axis(details.get("quality_status"), default="not_applicable")
    if details.get("turns") or details.get("verdict_scope") == "quality_and_performance":
        quality = business
    status = str(row.get("status", "Unverified")).lower()
    blocked = status == "blocked" or bool(details.get("execution_blocker"))
    lifecycle = "cancelled" if status == "cancelled" else "completed"
    phase = "blocked" if blocked else "cancelled" if status == "cancelled" else "settled"
    evidence = "valid" if run_rows and _evidence(run_rows[0], row, details) else "invalid"
    checks = {"business": business, "performance": performance, "quality": quality}
    if status == "fail" or "fail" in checks.values():
        outcome = "fail"
    elif blocked:
        outcome = "blocked"
    elif lifecycle == "cancelled":
        outcome = "cancelled"
    else:
        outcome = "pass" if business == "pass" and evidence == "valid" else "unverified"
    receipt = report("run_" + row["run_id"], run_row=run_rows[0] if run_rows else None) if row.get("run_id") else {}
    return _state(lifecycle, phase, outcome, scope=_scope(row, connection), reason=row.get("reason", ""),
                  checks=checks, evidence=evidence, report=receipt.get("status", "not_generated"),
                  last_attempt_outcome=_axis(details.get("last_attempt_status"), default=status))


@_projected
def run(row: dict, *, results=None, result_states=None, connection=None) -> dict:
    """Aggregate all target cases; prerequisites cannot establish task success."""
    if connection is None:
        with db.connect() as current:
            return run(row, results=results, result_states=result_states, connection=current)
    row = dict(row)
    identity = row["id"]
    stored = str(row.get("status", "Unknown")).lower()
    phases = {"queued": ("pending", "queued"), "running": ("active", "running"), "cancelled": ("cancelled", "cancelled"), "blocked": ("completed", "blocked")}
    lifecycle, phase = phases.get(stored, ("completed", "settled") if stored in ("pass", "fail") else ("unknown", "unknown"))
    rows = results if results is not None else _rows(connection, "SELECT * FROM results WHERE run_id = ? AND prerequisite = 0 ORDER BY id", (identity,))
    states = result_states if result_states is not None else [result(value, connection=connection) for value in rows if not value.get("prerequisite")]
    checks = {axis: _aggregate([value["checks"][axis] for value in states], "unverified" if axis == "business" else "not_applicable") for axis in ("business", "performance", "quality")}
    unfinished = len(states) < max(row.get("total", 0), row.get("done", 0)) or not states
    if unfinished:
        checks = {key: "unverified" if value == "pass" else value for key, value in checks.items()}
    evidence = "invalid" if any(value["evidence"] == "invalid" for value in states) else "valid" if states and not unfinished else "unverified"
    failed = stored == "fail" or any(value["outcome"] == "fail" for value in states)
    if failed:
        outcome = "fail"
    elif lifecycle in ("pending", "active"):
        outcome = "pending"
    elif lifecycle == "cancelled":
        outcome = "cancelled"
    elif stored == "blocked" or any(value["outcome"] == "blocked" for value in states):
        outcome = "blocked"
    else:
        outcome = "pass" if checks["business"] == "pass" and evidence == "valid" else "unverified"
    delivery = "not_applicable"
    if row.get("task_id") and _has(connection, "host_outbox"):
        delivered = _rows(connection, "SELECT o.state FROM host_outbox o JOIN schedule_occurrences s ON s.id=o.occurrence_id WHERE s.run_id=? ORDER BY o.updated_at DESC LIMIT 1", (identity,))
        if delivered:
            delivery = delivered[0]["state"]
    receipt = report("run_" + identity, run_row=row)
    return _state(lifecycle, phase, outcome, scope=_scope({**row, "run_id": identity}, connection), checks=checks,
                  evidence=evidence, report=receipt["status"], delivery=delivery, reason=row.get("reason", ""))


def counts(states: list[dict]) -> dict:
    """Count independently projected outcomes without treating stored Pass as proof."""
    outcomes = Counter(state["outcome"] for state in states)
    judged = outcomes["pass"] + outcomes["fail"]
    return {"passed": outcomes["pass"], "failed": outcomes["fail"], "blocked": outcomes["blocked"],
            "unverified": outcomes["unverified"], "cancelled": outcomes["cancelled"], "pending": outcomes["pending"],
            "judged": judged, "pass_rate": round(outcomes["pass"] / judged, 4) if judged else None,
            "state_counts": {key: outcomes[key] for key in ("pass", "fail", "blocked", "unverified", "cancelled", "pending")}}


@_projected
def run_values(row: dict, *, results=None, result_states=None, connection=None) -> dict:
    """Expose verified target counts while preserving the original recorded counters."""
    if connection is None:
        with db.connect() as current:
            return run_values(row, results=results, result_states=result_states, connection=current)
    row = dict(row)
    rows = results if results is not None else _rows(connection, "SELECT * FROM results WHERE run_id=? AND prerequisite=0 ORDER BY id", (row["id"],))
    states = result_states if result_states is not None else [result(value, connection=connection) for value in rows]
    state = run(row, results=rows, result_states=states, connection=connection)
    totals = counts(states)
    missing = max(0, max(row.get("total", 0), row.get("done", 0)) - len(rows))
    key = "pending" if state["lifecycle"] in ("pending", "active") else "cancelled" if state["lifecycle"] == "cancelled" else "unverified"
    totals[key] += missing
    totals["state_counts"][key] += missing
    return {**row, **totals, "state": state,
            "recorded_counts": dict(row.get("recorded_counts") or {name: row.get(name, 0) for name in ("passed", "failed", "blocked")})}


@_projected
def workflow(summary: dict, *, connection=None) -> dict:
    """Project the current workflow cycle without claiming host work."""
    if connection is None:
        from . import host_bindings

        with host_bindings.snapshot() as current:
            return _workflow(summary, current)
    return _workflow(summary, connection)


def _workflow(summary: dict, connection) -> dict:
    phase = summary.get("cycle_state") or summary.get("current_phase") or "awaiting_host"
    lifecycle = summary.get("lifecycle", "unknown")
    if lifecycle in ("cancelled", "paused"):
        phase = lifecycle
    elif summary.get("complete") or phase == "settled":
        lifecycle, phase = "completed", "settled"
    identity = summary.get("id", summary.get("workflow_id"))
    runs = (_rows(connection, "SELECT * FROM runs WHERE id IN (SELECT run_id FROM workflow_items WHERE workflow_id=? AND cycle_id=?)", (identity, summary.get("cycle_id")))
            if _has(connection, "workflow_items") else [])
    states = [run(row, connection=connection) for row in runs]
    checks = {axis: _aggregate([value["checks"][axis] for value in states], "unverified" if axis == "business" else "not_applicable") for axis in ("business", "performance", "quality")}
    evidence = "invalid" if any(value["evidence"] == "invalid" for value in states) else "valid" if states and all(value["evidence"] == "valid" for value in states) else "unverified"
    failed = any(value["outcome"] == "fail" for value in states) or summary.get("conclusion") in ("violated", "mixed")
    if failed:
        outcome = "fail"
    elif lifecycle == "cancelled":
        outcome = "cancelled"
    elif lifecycle != "completed":
        outcome = "pending"
    else:
        outcome = "pass" if checks["business"] == "pass" and evidence == "valid" else "unverified"
    from . import task_reports

    report_id = task_reports.workflow_report_id(identity, summary.get("cycle_id"), connection=connection)
    receipt = report(report_id)
    delivery = "not_applicable"
    if _has(connection, "host_outbox"):
        deliveries = _rows(connection, "SELECT state,payload FROM host_outbox WHERE json_extract(payload,'$.workflow_id') = ? ORDER BY updated_at DESC", (identity,))
        matching = [value for value in deliveries if _json(value["payload"]).get("cycle_id") == summary.get("cycle_id")]
        if matching:
            delivery = matching[0]["state"]
    return _state(lifecycle, phase, outcome, scope=_scope({**summary, "workflow_id": summary.get("id", summary.get("workflow_id"))}),
                  checks=checks, evidence=evidence, report=receipt["status"], delivery=delivery, reason=summary.get("reason", ""))


def revision() -> str:
    """Read a tiny database version and metadata of artifacts already observed."""
    from . import host_bindings

    with host_bindings.snapshot() as connection:
        version = _rows(connection, "SELECT revision FROM state_revision WHERE id = 1") if _has(connection, "state_revision") else []
    with _lock:
        files = list(_watched.get(str(paths.workspace()), set()))
    metadata = sorted(_stamp(file) for file in files)
    return hashlib.sha256(json.dumps([version, metadata], separators=(",", ":")).encode()).hexdigest()
