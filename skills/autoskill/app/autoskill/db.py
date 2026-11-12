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

from . import paths

SCHEMA = """
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
  pid INTEGER, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
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
"""
FINISHED = ("Pass", "Fail", "Blocked", "Cancelled")


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def connect():
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
    with connect() as conn:
        conn.executescript(SCHEMA)


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
