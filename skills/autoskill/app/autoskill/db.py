"""Machine-local run history and case index (SQLite, in workspace/.local).

Case JSON files in each project stay the single source of truth; the `cases`
table is an index rebuilt from them, so an imported project shows up at once.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3
import uuid

from . import maintenance, paths, runtime_identity
from .locking import named_lock

WORKFLOW_RUN_INDEX = ("CREATE INDEX IF NOT EXISTS workflow_items_by_run ON workflow_items(run_id, sequence DESC) "
                      "WHERE run_id IS NOT NULL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_revision (
  id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO state_revision(id,revision) VALUES(1,0);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
  base_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (
  project_id TEXT NOT NULL, case_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
  module TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT 'P1', category TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1, lifecycle TEXT NOT NULL DEFAULT 'active',
  replayable INTEGER NOT NULL DEFAULT 0, depends_on TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, archived_at TEXT,
  PRIMARY KEY (project_id, case_id));
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, batch_id TEXT, project_id TEXT NOT NULL, engine TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT 'manual', task_id TEXT, status TEXT NOT NULL,
  case_ids TEXT NOT NULL DEFAULT '[]', total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
  passed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
  current TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', cancel INTEGER NOT NULL DEFAULT 0,
  pid INTEGER, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, headed INTEGER NOT NULL DEFAULT 0,
  claim_token TEXT NOT NULL DEFAULT '', force_login INTEGER NOT NULL DEFAULT 0, session_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS runs_by_time ON runs (created_at);
CREATE INDEX IF NOT EXISTS runs_by_project ON runs (project_id, created_at);
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, project_id TEXT NOT NULL,
  case_id TEXT NOT NULL, case_title TEXT NOT NULL, kind TEXT NOT NULL, engine TEXT NOT NULL,
  status TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
  actual TEXT NOT NULL DEFAULT '', duration_ms INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
  prerequisite INTEGER NOT NULL DEFAULT 0, case_revision INTEGER NOT NULL DEFAULT 1,
  detail TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS results_by_run ON results (run_id);
CREATE INDEX IF NOT EXISTS results_by_time ON results (started_at);
CREATE INDEX IF NOT EXISTS results_by_case ON results (project_id, case_id, started_at);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, engine TEXT NOT NULL,
  case_ids TEXT NOT NULL DEFAULT '[]', rule TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  next_due TEXT, last_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_requests (
  scope TEXT NOT NULL, request_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
  response TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (scope, request_key));
CREATE TABLE IF NOT EXISTS schedule_occurrences (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, due_at TEXT NOT NULL,
  status TEXT NOT NULL, run_id TEXT, delivery_id TEXT, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE (task_id, revision, due_at));
CREATE TABLE IF NOT EXISTS host_outbox (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, occurrence_id TEXT NOT NULL UNIQUE, host TEXT NOT NULL,
  payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', owner TEXT NOT NULL DEFAULT '',
  token TEXT NOT NULL DEFAULT '', lease_until TEXT, receipt TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS host_outbox_pending ON host_outbox (host, state, created_at);
CREATE TABLE IF NOT EXISTS deleted_cases (
  project_id TEXT NOT NULL, case_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
  PRIMARY KEY(project_id, case_id));
CREATE TABLE IF NOT EXISTS deleted_runs (id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_task_bindings (
  id TEXT PRIMARY KEY, host TEXT NOT NULL, host_session_id TEXT NOT NULL,
  task_session_id TEXT NOT NULL, project_id TEXT, workflow_id TEXT, cycle_id TEXT, run_id TEXT,
  label TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS host_task_bindings_by_session ON host_task_bindings(host,host_session_id,created_at,id);
CREATE TABLE IF NOT EXISTS host_task_selections (
  host TEXT NOT NULL, host_session_id TEXT NOT NULL, selected_binding_id TEXT,
  revision INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(host,host_session_id));
CREATE TABLE IF NOT EXISTS host_task_requests (
  host TEXT NOT NULL, host_session_id TEXT NOT NULL, request_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(host,host_session_id,request_key));
CREATE TABLE IF NOT EXISTS execution_requests (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL, payload TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, UNIQUE(scope,idempotency_key));
"""
FINISHED = ("Pass", "Fail", "Blocked", "Cancelled")


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def connect():
    with maintenance.access(paths.workspace()), _connection() as conn:
        yield conn


@contextlib.contextmanager
def _connection():
    conn = sqlite3.connect(paths.local() / "autoskill.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    # Serialize the initial WAL transition as well as migrations across fresh CLI processes.
    with named_lock("database", "schema"):
        _initialize()


def install_state_triggers(connection) -> None:
    """Increment one committed workspace revision only for real business-row changes."""
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'state_revision'")]
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
        changed = " OR ".join('OLD."' + name.replace('"', '""') + '" IS NOT NEW."' + name.replace('"', '""') + '"' for name in columns)
        for event in ("INSERT", "UPDATE", "DELETE"):
            identity = "state_revision_" + table + "_" + event.lower()
            name = '"' + identity.replace('"', '""') + '"'
            condition = " WHEN " + changed if event == "UPDATE" else ""
            statement = f"CREATE TRIGGER {name} AFTER {event} ON {quoted}{condition} BEGIN UPDATE state_revision SET revision = revision + 1 WHERE id=1; END"
            existing = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (identity,)).fetchone()
            if existing and existing["sql"] != statement:
                connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            connection.execute(statement.replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1))


def _initialize() -> None:
    fresh = not (paths.workspace() / ".local/autoskill.db").exists()
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")  # Concurrent CLI/worker startups must not migrate the same column twice.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        for name, definition in (("headed", "INTEGER NOT NULL DEFAULT 0"), ("claim_token", "TEXT NOT NULL DEFAULT ''"),
                                 ("force_login", "INTEGER NOT NULL DEFAULT 0"), ("session_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("family", "TEXT NOT NULL DEFAULT 'replay'"), ("load_mode", "TEXT NOT NULL DEFAULT 'performance'"),
                                 ("input_manifest", "TEXT NOT NULL DEFAULT ''"),
                                 ("input_hash", "TEXT NOT NULL DEFAULT ''"), ("plan_path", "TEXT NOT NULL DEFAULT ''"),
                                 ("delete_requested", "INTEGER NOT NULL DEFAULT 0"), ("deletion_error", "TEXT NOT NULL DEFAULT ''"),
                                 ("deletion_manifest", "TEXT NOT NULL DEFAULT ''"), ("owner_identity", "TEXT NOT NULL DEFAULT '{}'"),
                                 ("environment_id", "TEXT NOT NULL DEFAULT ''"), ("environment_name", "TEXT NOT NULL DEFAULT ''"),
                                 ("account_id", "TEXT NOT NULL DEFAULT ''"), ("account_label", "TEXT NOT NULL DEFAULT ''"),
                                 ("account_lock_key", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS runs_by_session ON runs(session_id,created_at,id)")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_items'").fetchone():
            conn.execute(WORKFLOW_RUN_INDEX)
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for name, definition in (("kind", "TEXT NOT NULL DEFAULT 'replay'"), ("asset_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("parameters", "TEXT NOT NULL DEFAULT '{}'"), ("workflow_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("host", "TEXT NOT NULL DEFAULT 'generic'"), ("revision", "INTEGER NOT NULL DEFAULT 1"),
                                 ("overlap", "TEXT NOT NULL DEFAULT 'skip'"), ("session_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("plan_path", "TEXT NOT NULL DEFAULT ''"), ("host_session_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("headed", "INTEGER"), ("environment_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("account_id", "TEXT NOT NULL DEFAULT ''")):
            if name not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        result_columns = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
        for name in ("case_content_hash", "input_hash"):
            if name not in result_columns:
                conn.execute(f"ALTER TABLE results ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        install_state_triggers(conn)
    if fresh:
        runtime_identity.note_new_database(paths.workspace())


def claim_run(run_id: str, pid: int, maximum: int, token: str = "") -> bool:
    """Reserve a process slot; serialize replay runs per project and per shared platform account."""
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE runs SET status = 'Running', started_at = ?, pid = ?, claim_token = ?, owner_identity = ? "
            "WHERE id = ? AND status = 'Queued' AND cancel = 0 AND delete_requested = 0 "
            "AND (SELECT COUNT(*) FROM runs WHERE status = 'Running') < ? "
            "AND NOT EXISTS (SELECT 1 FROM runs active WHERE active.status = 'Running' "
            "AND active.family = 'replay' AND runs.family = 'replay' "
            "AND (active.project_id = runs.project_id "
            "OR (runs.account_lock_key != '' AND active.account_lock_key = runs.account_lock_key)))",
            (now(), pid, token, json.dumps(runtime_identity.current(pid)), run_id, maximum)).rowcount)


def activate_run(run_id: str, pid: int, token: str) -> bool:
    """A spawned child consumes its reservation once, before any case can execute."""
    if not token:
        return False
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE runs SET pid = ?, claim_token = '', owner_identity = ? WHERE id = ? AND status = 'Running' AND claim_token = ?",
            (pid, json.dumps(runtime_identity.current(pid)), run_id, token)).rowcount)


def finish_run(run_id: str, pid: int, counts: dict, status: str, reason: str) -> None:
    """Finish only the active executor's row; cancellation also wins during the final case or cleanup."""
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET done = ?, passed = ?, failed = ?, blocked = ?, "
            "status = CASE WHEN cancel = 1 THEN 'Cancelled' ELSE ? END, "
            "reason = CASE WHEN cancel = 1 THEN '' ELSE ? END, current = '', finished_at = ? "
            "WHERE id = ? AND status = 'Running' AND pid = ? AND claim_token = '' AND owner_identity = ?",
            (counts["done"], counts["passed"], counts["failed"], counts["blocked"], status, reason, now(), run_id, pid, json.dumps(runtime_identity.current(pid))))


def rows(sql: str, args: tuple | list = ()) -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def one(sql: str, args: tuple | list = ()) -> dict | None:
    found = rows(sql, args)
    return found[0] if found else None


def execute(sql: str, args: tuple | list = ()) -> None:
    with connect() as conn:
        conn.execute(sql, args)


def insert(table: str, values: dict) -> None:
    keys = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    packed = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in values.values()]
    execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", packed)


def update(table: str, key: str, values: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in values)
    packed = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in values.values()]
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*packed, key])
