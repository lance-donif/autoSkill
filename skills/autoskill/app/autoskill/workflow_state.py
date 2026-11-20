"""Machine-local workflow leases, submission journals and operation receipts."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runtime (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
  cycle_id TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'active',
  conclusion TEXT NOT NULL DEFAULT 'unassessed', domain_head TEXT,
  definition_hash TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_items (
  id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  cycle_id TEXT NOT NULL DEFAULT '',
  phase TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready', attempt_id TEXT,
  input_hash TEXT NOT NULL DEFAULT '', run_id TEXT, context TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, finished_at TEXT,
  UNIQUE(workflow_id, sequence));
CREATE TABLE IF NOT EXISTS workflow_attempts (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
  generation INTEGER NOT NULL, owner TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
  lease_token TEXT NOT NULL UNIQUE, lease_until TEXT NOT NULL, state TEXT NOT NULL,
  checkpoint TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS workflow_requests (
  scope TEXT NOT NULL, request_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
  action TEXT NOT NULL, state TEXT NOT NULL, response TEXT, journal TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, PRIMARY KEY(scope, request_key));
CREATE TABLE IF NOT EXISTS workflow_operations (
  id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, item_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
  request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, side_effect INTEGER NOT NULL, state TEXT NOT NULL,
  receipt_token TEXT NOT NULL, observation TEXT, created_at TEXT NOT NULL, finished_at TEXT,
  UNIQUE(attempt_id, request_key));
CREATE TABLE IF NOT EXISTS workflow_report_heads (
  workflow_id TEXT NOT NULL, cycle_id TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(workflow_id, cycle_id), CHECK(generation >= 0));
CREATE INDEX IF NOT EXISTS workflow_items_by_flow ON workflow_items(workflow_id, sequence);
CREATE INDEX IF NOT EXISTS workflow_attempts_by_session ON workflow_attempts(session_id, state);
CREATE INDEX IF NOT EXISTS workflow_operations_by_item ON workflow_operations(item_id, state);
"""


class WorkflowError(ValueError):
    """A structured service error shared by CLI and HTTP adapters."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def packed(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(packed(value).encode("utf-8")).hexdigest()


def identifier(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


def init() -> None:
    db.init()
    with db.connect() as connection:
        connection.executescript(SCHEMA)
        connection.execute(db.WORKFLOW_RUN_INDEX)
        db.install_state_triggers(connection)
        connection.execute("BEGIN IMMEDIATE")
        columns = {record[1] for record in connection.execute("PRAGMA table_info(workflow_items)")}
        if "cycle_id" not in columns:
            connection.execute("ALTER TABLE workflow_items ADD COLUMN cycle_id TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE workflow_items SET cycle_id = (SELECT cycle_id FROM workflow_runtime WHERE id = workflow_items.workflow_id) WHERE cycle_id = ''")
        connection.execute("INSERT OR IGNORE INTO workflow_report_heads(workflow_id,cycle_id,generation) SELECT id,cycle_id,0 FROM workflow_runtime")


def report_generation(connection, workflow_id: str, cycle_id: str) -> int:
    """Read a cycle's report head without changing history or installing schema."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workflow_report_heads'").fetchone():
        return 0
    found = connection.execute("SELECT generation FROM workflow_report_heads WHERE workflow_id = ? AND cycle_id = ?", (workflow_id, cycle_id)).fetchone()
    return found["generation"] if found else 0


def deadline(seconds: int) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise WorkflowError("validation_error", "租约秒数必须是正整数")
    return (datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def row(connection, table: str, identity: str) -> dict:
    found = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (identity,)).fetchone()
    if not found:
        raise WorkflowError("not_found", "工作流记录不存在", id=identity)
    return dict(found)


def workflow(identity: str) -> dict:
    init()
    with db.connect() as connection:
        return row(connection, "workflow_runtime", identity)


def touch(connection, workflow_id: str) -> None:
    connection.execute("UPDATE workflow_runtime SET revision = revision + 1, updated_at = ? WHERE id = ?", (db.now(), workflow_id))


def item(connection, workflow_id: str, phase: str, *, context: dict | None = None) -> str:
    identity = identifier("wfi")
    sequence = connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM workflow_items WHERE workflow_id = ?", (workflow_id,)).fetchone()[0]
    connection.execute(
        "INSERT INTO workflow_items(id,workflow_id,sequence,cycle_id,phase,context,created_at) VALUES(?,?,?,?,?,?,?)",
        (identity, workflow_id, sequence, row(connection, "workflow_runtime", workflow_id)["cycle_id"], phase, packed(context or {}), db.now()))
    return identity


def request(connection, scope: str, key: str, action: str, payload: dict) -> dict | None:
    if not isinstance(key, str) or not key.strip():
        raise WorkflowError("validation_error", "请提供稳定的 idempotency_key，重试时使用同一值")
    fingerprint = digest({"action": action, "payload": payload})
    found = connection.execute("SELECT * FROM workflow_requests WHERE scope = ? AND request_key = ?", (scope, key)).fetchone()
    if found:
        if found["payload_hash"] != fingerprint:
            raise WorkflowError("idempotency_conflict", "同一幂等键已经用于不同请求", key=key)
        return dict(found)
    connection.execute(
        "INSERT INTO workflow_requests(scope,request_key,payload_hash,action,state,created_at) VALUES(?,?,?,?,?,?)",
        (scope, key, fingerprint, action, "pending", db.now()))
    return None


def receipt(connection, scope: str, key: str, response: dict) -> dict:
    connection.execute("UPDATE workflow_requests SET state = 'completed', response = ? WHERE scope = ? AND request_key = ?",
                       (packed(response), scope, key))
    return response


def replay(found: dict | None) -> dict | None:
    return json.loads(found["response"]) if found and found["state"] == "completed" else None


def assert_lease(connection, workflow_id: str, item_id: str, attempt_id: str, token: str) -> tuple[dict, dict]:
    task = row(connection, "workflow_items", item_id)
    attempt = row(connection, "workflow_attempts", attempt_id)
    flow = row(connection, "workflow_runtime", workflow_id)
    valid = (task["workflow_id"] == workflow_id and task["attempt_id"] == attempt_id and task["state"] == "leased"
             and attempt["item_id"] == item_id and attempt["state"] == "leased" and attempt["lease_token"] == token
             and attempt["lease_until"] > db.now() and flow["lifecycle"] == "active")
    if not valid:
        raise WorkflowError("stale_lease", "这次领取已失效，请重新领取任务；旧提交不会覆盖当前状态", item_id=item_id, attempt_id=attempt_id)
    return task, attempt


def unresolved(connection, item_id: str) -> list[dict]:
    return [dict(r) for r in connection.execute(
        "SELECT id,kind,state,payload,attempt_id FROM workflow_operations "
        "WHERE item_id = ? AND side_effect = 1 AND state IN ('intended','unknown') ORDER BY created_at", (item_id,))]


def complete(connection, task: dict, *, state: str = "completed") -> None:
    connection.execute("UPDATE workflow_items SET state = ?, finished_at = ? WHERE id = ?", (state, db.now(), task["id"]))
    if task.get("attempt_id"):
        connection.execute("UPDATE workflow_attempts SET state = ?, finished_at = ? WHERE id = ?",
                           (state, db.now(), task["attempt_id"]))
    touch(connection, task["workflow_id"])
