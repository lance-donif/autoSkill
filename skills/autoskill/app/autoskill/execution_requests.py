"""Idempotent execution intents sharing the existing immutable-input and run queue services."""
from __future__ import annotations

import contextlib
import json
import sqlite3

from . import db, execution_inputs, maintenance, native_run, paths, projects, runner
from .locking import named_lock

_COMMON = {"family", "project_id", "session_id", "trigger", "task_id"}
_REPLAY = {"case_ids", "engine", "headed", "force_login", "batch_id", "candidate_cases", "plan_path"}
_NATIVE = {"asset_id", "head", "parameters", "host", "host_session_id"}
_RUN_FIELDS = ("id", "family", "project_id", "engine", "session_id", "trigger", "task_id", "batch_id", "status",
               "total", "done", "passed", "failed", "blocked", "current", "reason", "cancel", "created_at", "started_at", "finished_at", "input_hash")


class ExecutionRequestError(ValueError):
    """A request could not be admitted or recovered without changing its identity."""

    def __init__(self, code: str, message: str, status_code: int = 400, **details):
        super().__init__(message)
        self.code, self.status_code, self.details = code, status_code, details


def _text(value, field: str, *, optional: bool = False, empty: bool = False):
    if optional and value is None:
        return value
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ExecutionRequestError("validation_error", f"{field} 必须是{'文本' if empty else '非空文本'}")
    return value


def _normalize(request: dict) -> dict:
    if not isinstance(request, dict) or any(not isinstance(key, str) for key in request):
        raise ExecutionRequestError("validation_error", "request 必须是执行请求对象")
    family = request.get("family")
    if family not in ("replay", "native"):
        raise ExecutionRequestError("validation_error", "family 必须是 replay 或 native")
    unknown = set(request) - _COMMON - (_REPLAY if family == "replay" else _NATIVE)
    if unknown:
        raise ExecutionRequestError("validation_error", "执行请求包含未知字段：" + "、".join(sorted(unknown)))
    base = {"family": family, "project_id": _text(request.get("project_id"), "project_id"),
            "session_id": _text(request.get("session_id"), "session_id", optional=True, empty=True),
            "trigger": _text(request.get("trigger", "manual"), "trigger"),
            "task_id": _text(request.get("task_id"), "task_id", optional=True, empty=True)}
    value = {**base, **(_replay_options(request) if family == "replay" else _native_options(request))}
    try:
        return json.loads(execution_inputs.canonical(value))
    except (ValueError, TypeError) as error:
        raise ExecutionRequestError("validation_error", "执行请求必须是有效 JSON，数值不能是 NaN 或 Infinity") from error


def _replay_options(request: dict) -> dict:
    chosen = request.get("case_ids")
    if chosen is not None and (not isinstance(chosen, list) or any(not isinstance(item, str) or not item.strip() for item in chosen)):
        raise ExecutionRequestError("validation_error", "case_ids 必须是用例标识数组或 null")
    candidates = request.get("candidate_cases")
    if candidates is not None and (not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates)):
        raise ExecutionRequestError("validation_error", "candidate_cases 必须是完整候选用例数组或 null")
    headed, force = request.get("headed"), request.get("force_login", False)
    if (headed is not None and type(headed) is not bool) or type(force) is not bool:
        raise ExecutionRequestError("validation_error", "headed 和 force_login 必须是布尔值；headed 可为 null")
    return {"case_ids": chosen, "candidate_cases": candidates, "engine": _text(request.get("engine", "playwright"), "engine"),
            "headed": headed, "force_login": force, "batch_id": _text(request.get("batch_id"), "batch_id", optional=True, empty=True),
            "plan_path": _text(request.get("plan_path"), "plan_path", optional=True)}


def _native_options(request: dict) -> dict:
    parameters = request.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        raise ExecutionRequestError("validation_error", "parameters 必须是对象或 null")
    return {"asset_id": _text(request.get("asset_id"), "asset_id"), "head": _text(request.get("head"), "head", optional=True),
            "parameters": parameters or {}, "host": _text(request.get("host", "generic"), "host"),
            "host_session_id": _text(request.get("host_session_id", ""), "host_session_id", empty=True)}


def _find(connection, scope: str, key: str) -> dict | None:
    row = connection.execute("SELECT * FROM execution_requests WHERE scope = ? AND idempotency_key = ?", (scope, key)).fetchone()
    return dict(row) if row else None


def _view(connection, receipt: dict) -> dict:
    row = connection.execute("SELECT * FROM runs WHERE id = ?", (receipt["run_id"],)).fetchone()
    deleted = connection.execute("SELECT 1 FROM deleted_runs WHERE id = ?", (receipt["run_id"],)).fetchone()
    removed = deleted or (row and row["delete_requested"])
    run = {key: row[key] for key in _RUN_FIELDS} if row and not removed else None
    if run:
        run["case_ids"] = json.loads(row["case_ids"])
    return {"request_id": receipt["id"], **{key: receipt[key] for key in ("scope", "idempotency_key", "payload_hash", "run_id", "created_at")},
            "state": "deleted" if removed else "accepted" if row else "missing", "run": run}


def _match(receipt: dict, payload_hash: str) -> None:
    if receipt["payload_hash"] != payload_hash:
        raise ExecutionRequestError("idempotency_conflict", "此请求标识已用于不同的执行参数；请查询原请求，新的执行意图使用新标识", 409,
                                    request_id=receipt["id"], run_id=receipt["run_id"])


def lookup(scope: str, idempotency_key: str) -> dict:
    """Read a committed intent and current run state without initialization, dispatch or report generation."""
    _text(scope, "scope")
    _text(idempotency_key, "idempotency_key")
    file = paths.workspace() / ".local" / "autoskill.db"
    missing = ExecutionRequestError("request_not_found", "尚未找到此执行请求的提交收据", 404)
    with maintenance.access(paths.workspace()):
        if not file.is_file():
            raise missing
        with contextlib.closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'execution_requests'").fetchone():
                raise missing
            receipt = _find(connection, scope, idempotency_key)
            if receipt is None:
                raise missing
            return _view(connection, receipt)


def _prepare(request: dict) -> dict:
    options = {key: value for key, value in request.items() if key != "family"}
    try:
        projects.reindex(request["project_id"])
        return (runner.prepare if request["family"] == "replay" else native_run.prepare)(**options)
    except ValueError as error:
        raise ExecutionRequestError("validation_error", str(error)) from error


def _commit(scope: str, key: str, request: dict, digest: str) -> dict:
    with db.connect() as connection:
        previous = _find(connection, scope, key)
        if previous:
            _match(previous, digest)
            return _view(connection, previous)
    prepared = _prepare(request)
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = _find(connection, scope, key)
        if previous:
            _match(previous, digest)
            return _view(connection, previous)
        runner.enqueue(prepared, connection=connection)
        receipt = {"id": db.new_id("execution-request"), "scope": scope, "idempotency_key": key, "payload_hash": digest,
                   "payload": execution_inputs.canonical(request).decode("utf-8"), "run_id": prepared["id"], "created_at": db.now()}
        connection.execute("INSERT INTO execution_requests (id,scope,idempotency_key,payload_hash,payload,run_id,created_at) VALUES (?,?,?,?,?,?,?)",
                           tuple(receipt[key] for key in ("id", "scope", "idempotency_key", "payload_hash", "payload", "run_id", "created_at")))
        return _view(connection, receipt)


def start(scope: str, idempotency_key: str, request: dict) -> dict:
    """Atomically enqueue one request; retries retain its first frozen inputs, even after deletion."""
    _text(scope, "scope")
    _text(idempotency_key, "idempotency_key")
    normalized = _normalize(request)
    digest = execution_inputs.content_hash(normalized)
    db.init()
    with named_lock("execution-request", execution_inputs.canonical([scope, idempotency_key]).decode("utf-8")):
        result = _commit(scope, idempotency_key, normalized, digest)
    if result["state"] == "accepted" and result["run"]["status"] not in db.FINISHED:
        from .server import jobs

        try:
            dispatcher = jobs.ensure_worker()
        except Exception as error:  # noqa: BLE001  # A committed run must remain recoverable when dispatcher startup or its response fails.
            dispatcher = {"status": "unavailable", "pid": None, "reason": "执行请求已保存，后台队列启动失败：" + type(error).__name__}
        result["queue_dispatcher"] = dispatcher
    return result
