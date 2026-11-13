"""Explicit host/task references and versioned selection, independent of business state."""
from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager

from . import db, maintenance, paging, paths

FIELDS = ("task_session_id", "project_id", "workflow_id", "cycle_id", "run_id", "label")


class BindingError(ValueError):
    """Structured identity, request-replay and optimistic concurrency errors."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


def identity(host: str, host_session_id: str) -> tuple[str, str]:
    if not isinstance(host, str) or not host or not isinstance(host_session_id, str):
        raise BindingError("validation_error", "宿主名称和宿主会话标识需要是文字")
    return host, host_session_id


@contextmanager
def snapshot():
    """Read an existing database snapshot without migration, mkdir or report generation."""
    file = paths.workspace() / ".local" / "autoskill.db"
    if not file.is_file():
        yield None
        return
    with maintenance.access(paths.workspace()):
        connection = sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()


def has_table(connection, name: str) -> bool:
    return connection is not None and bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def selection(connection, host: str, host_session_id: str) -> dict:
    row = (connection.execute("SELECT selected_binding_id,revision FROM host_task_selections WHERE host = ? AND host_session_id = ?",
                              (host, host_session_id)).fetchone() if has_table(connection, "host_task_selections") else None)
    return {"binding_id": row["selected_binding_id"], "revision": row["revision"]} if row else {"binding_id": None, "revision": 0}


def binding(connection, host: str, host_session_id: str, binding_id: str) -> dict:
    if not isinstance(binding_id, str):
        raise BindingError("validation_error", "任务绑定标识需要是文字")
    row = (connection.execute("SELECT * FROM host_task_bindings WHERE id = ? AND host = ? AND host_session_id = ?",
                              (binding_id, host, host_session_id)).fetchone() if has_table(connection, "host_task_bindings") else None)
    if not row:
        raise BindingError("not_found", "当前宿主会话没有这条任务绑定", binding_id=binding_id)
    return dict(row)


def page_info(total: int, returned: int, limit: int, offset: int, revision: str, query: dict) -> dict:
    end = offset + returned
    token = {"version": 1, "offset": end, "revision": revision, "query": paging.revision(query)}
    cursor = base64.urlsafe_b64encode(json.dumps(token, separators=(",", ":")).encode()).decode().rstrip("=") if end < total else None
    return {"total": total, "returned": returned, "limit": limit, "has_more": end < total, "next_cursor": cursor, "content_revision": revision}


def binding_page(connection, host: str, host_session_id: str, *, limit: int = 20, cursor: str | None = None) -> dict:
    if type(limit) is not int or limit < 1:
        raise BindingError("validation_error", "每页数量需要是正整数")
    query, parameters = {"host": host, "host_session_id": host_session_id}, (host, host_session_id)
    exists = has_table(connection, "host_task_bindings")
    counts = connection.execute("SELECT COUNT(*) n,COALESCE(SUM(revision),0) versions FROM host_task_bindings WHERE host = ? AND host_session_id = ?",
                                parameters).fetchone() if exists else {"n": 0, "versions": 0}
    revision = paging.revision([query, counts["n"], counts["versions"]])
    offset = paging.position(cursor, revision, query)
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM host_task_bindings WHERE host = ? AND host_session_id = ? ORDER BY created_at,id LIMIT ? OFFSET ?",
        (*parameters, limit, offset))] if exists else []
    return {"items": rows, "page": page_info(counts["n"], len(rows), limit, offset, revision, query)}


def list_bindings(host: str, host_session_id: str, *, limit: int = 20, cursor: str | None = None) -> dict:
    identity(host, host_session_id)
    with snapshot() as connection:
        return {"bindings": binding_page(connection, host, host_session_id, limit=limit, cursor=cursor),
                "selection": selection(connection, host, host_session_id)}


def _request(connection, body: dict, action: str) -> tuple[str, dict | None]:
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        raise BindingError("validation_error", "请提供稳定的 idempotency_key，重试时使用同一值")
    digest = paging.revision({"action": action, "body": body})
    row = connection.execute("SELECT payload_hash,response FROM host_task_requests WHERE host = ? AND host_session_id = ? AND request_key = ?",
                             (body["host"], body["host_session_id"], key)).fetchone()
    if row and row["payload_hash"] != digest:
        raise BindingError("idempotency_conflict", "同一幂等键已经用于不同请求")
    return digest, json.loads(row["response"]) if row else None


def _receipt(connection, body: dict, digest: str, result: dict) -> dict:
    connection.execute("INSERT INTO host_task_requests(host,host_session_id,request_key,payload_hash,response,created_at) VALUES(?,?,?,?,?,?)",
                       (body["host"], body["host_session_id"], body["idempotency_key"], digest,
                        json.dumps(result, ensure_ascii=False, separators=(",", ":")), db.now()))
    return result


def _expected(value, actual: int) -> None:
    if type(value) is not int or value != actual:
        raise BindingError("revision_conflict", "任务绑定或选择已变化，请读取最新版本后重试", expected_revision=value, revision=actual)


def _reference(connection, table: str, row_id: str) -> dict:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone() if has_table(connection, table) else None
    if not row:
        raise BindingError("not_found", "绑定引用的记录不存在", id=row_id)
    return dict(row)


def _project(value: dict, project_id: str) -> None:
    if value["project_id"] and value["project_id"] != project_id:
        raise BindingError("reference_conflict", "绑定中的项目与工作流或运行不一致")
    value["project_id"] = project_id


def _references(connection, value: dict) -> None:
    if value["workflow_id"]:
        flow = _reference(connection, "workflow_runtime", value["workflow_id"])
        _project(value, flow["project_id"])
        value["cycle_id"] = value["cycle_id"] or flow["cycle_id"]
        if value["cycle_id"] != flow["cycle_id"] and not connection.execute(
                "SELECT 1 FROM workflow_items WHERE workflow_id = ? AND cycle_id = ? LIMIT 1", (value["workflow_id"], value["cycle_id"])).fetchone():
            raise BindingError("reference_conflict", "周期不属于这条工作流")
    elif value["cycle_id"]:
        raise BindingError("reference_conflict", "绑定周期时需要对应的工作流")
    if value["run_id"]:
        run = _reference(connection, "runs", value["run_id"])
        _project(value, run["project_id"])
        if value["workflow_id"] and not connection.execute(
                "SELECT 1 FROM workflow_items WHERE workflow_id = ? AND cycle_id = ? AND run_id = ?",
                (value["workflow_id"], value["cycle_id"], value["run_id"])).fetchone():
            raise BindingError("reference_conflict", "运行不属于绑定的工作流周期")
    if value["project_id"]:
        _reference(connection, "projects", value["project_id"])


def _values(body: dict, previous: dict | None) -> dict:
    value = {key: body.get(key, previous.get(key) if previous else ("" if key in ("task_session_id", "label") else None)) for key in FIELDS}
    for key, item in value.items():
        if not isinstance(item, str) and (key in ("task_session_id", "label") or item is not None):
            raise BindingError("validation_error", "任务会话、标签和关联标识需要是文字", field=key)
        if key not in ("task_session_id", "label") and item == "":
            value[key] = None
    return value


def upsert(body: dict) -> dict:
    """Publish one reference update and its retry receipt in the same SQLite transaction."""
    host, host_session_id = identity(body.get("host"), body.get("host_session_id"))
    db.init()
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        digest, previous_result = _request(connection, body, "bind")
        if previous_result is not None:
            return previous_result
        previous = binding(connection, host, host_session_id, body["binding_id"]) if body.get("binding_id") else None
        _expected(body.get("expected_revision", 0), previous["revision"] if previous else 0)
        value = _values(body, previous)
        _references(connection, value)
        if previous:
            if any(value[key] != previous[key] for key in FIELDS):
                connection.execute("UPDATE host_task_bindings SET task_session_id=?,project_id=?,workflow_id=?,cycle_id=?,run_id=?,label=?,revision=revision+1,updated_at=? WHERE id=?",
                                   (*[value[key] for key in FIELDS], db.now(), previous["id"]))
            binding_id = previous["id"]
        else:
            binding_id, stamp = db.new_id("binding"), db.now()
            connection.execute("INSERT INTO host_task_bindings(id,host,host_session_id,task_session_id,project_id,workflow_id,cycle_id,run_id,label,created_at,updated_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (binding_id, host, host_session_id, *[value[key] for key in FIELDS], stamp, stamp))
        result = {"binding": binding(connection, host, host_session_id, binding_id), "selection": selection(connection, host, host_session_id)}
        return _receipt(connection, body, digest, result)


def select(body: dict) -> dict:
    """Change explicit selection without selecting a task merely because it is recent."""
    host, host_session_id = identity(body.get("host"), body.get("host_session_id"))
    db.init()
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        digest, previous_result = _request(connection, body, "select")
        if previous_result is not None:
            return previous_result
        current = selection(connection, host, host_session_id)
        _expected(body.get("expected_revision"), current["revision"])
        binding_id = body.get("binding_id")
        if binding_id is not None:
            binding(connection, host, host_session_id, binding_id)
        connection.execute("INSERT INTO host_task_selections(host,host_session_id,selected_binding_id,revision,updated_at) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(host,host_session_id) DO UPDATE SET selected_binding_id=excluded.selected_binding_id,revision=excluded.revision,updated_at=excluded.updated_at",
                           (host, host_session_id, binding_id, current["revision"] + 1, db.now()))
        return _receipt(connection, body, digest, {"selection": selection(connection, host, host_session_id)})


def request_receipt(host: str, host_session_id: str, *, kind: str, key: str, scope: str = "") -> dict:
    """Look up the exact prior request; absence is not proof an in-flight call never happened."""
    identity(host, host_session_id)
    if kind not in ("binding", "workflow", "judge") or not isinstance(key, str) or not key or not isinstance(scope, str):
        raise BindingError("validation_error", "需要 binding/workflow/judge 类型、原请求 key 和 scope")
    result = {"kind": kind, "key": key, "scope": scope, "state": "missing", "response": None}
    with snapshot() as connection:
        if kind == "binding":
            row = (connection.execute("SELECT response,'completed' state FROM host_task_requests WHERE host=? AND host_session_id=? AND request_key=?",
                                      (host, host_session_id, key)).fetchone() if has_table(connection, "host_task_requests") else None)
        else:
            row = (connection.execute("SELECT response,state FROM workflow_requests WHERE scope=? AND request_key=?", (scope, key)).fetchone()
                   if has_table(connection, "workflow_requests") else None)
        if row:
            result.update(state=row["state"], response=json.loads(row["response"]) if row["response"] else None)
        return result
