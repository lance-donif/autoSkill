"""Relocation preserves old evidence while fencing process handles and unresolved deliveries."""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import uuid
from pathlib import Path

from . import data_refs, data_transfer, maintenance, runtime_identity

RUNTIME_FILES = ("service.json", "queue-worker.json", "queue-worker.lock", "recording.json", "recordings", "browser-control", "host-deliveries")
TABLES = {"runs", "tasks", "host_outbox", "workflow_items", "workflow_attempts", "workflow_operations", "workflow_runtime",
          "dialogue_judge_attempts", "dialogue_judgements"}
INTERRUPTED = "数据目录已重新附着；原进程状态未知，保留已有结果，未自动重放"
UNCERTAIN = "数据目录已重新附着，需要在当前宿主核实原投递；未自动重发"


def _tables(connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def activities(root: Path) -> list[dict]:
    found = []
    database = root / ".local" / "autoskill.db"
    if database.is_file():
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            if "runs" in _tables(connection):
                for row in connection.execute("SELECT * FROM runs WHERE status='Running'"):
                    run = dict(row)
                    if runtime_identity.owns(run.get("pid"), run.get("owner_identity"), root) or runtime_identity.legacy_matches(run.get("pid"), root):
                        found.append({"kind": "run", "id": run["id"], "project_id": run["project_id"], "pid": run["pid"]})
    for file in [root / ".local/recording.json", *root.glob(".local/recordings/*/recording.json")]:
        if not file.is_file():
            continue
        value = data_transfer.read_json(file)
        owner = value.get("network_capture") or {}
        if runtime_identity.owns(owner.get("pid"), owner.get("owner"), root) or runtime_identity.legacy_matches(owner.get("pid"), root):
            found.append({"kind": "recording", "id": value.get("session_id"), "project_id": value.get("project_id"), "pid": owner["pid"]})
    for file in root.glob(".local/browser-control/*.owner.json"):
        owner = data_transfer.read_json(file)
        if runtime_identity.owns(owner.get("pid"), owner, root):
            found.append({"kind": "browser", "id": file.stem, "pid": owner["pid"]})
    return found


def _task_values(task: dict, stamp: str) -> dict:
    from .server.scheduler import next_due

    now = datetime.datetime.fromisoformat(stamp)
    values = {"host_session_id": ""} if task.get("host_session_id") else {}
    if task.get("enabled") and task.get("next_due"):
        due = datetime.datetime.fromisoformat(task["next_due"])
        if due <= now:
            rule = json.loads(task["rule"])
            if rule.get("kind") == "at":
                values.update(enabled=0, next_due=None)
            else:
                following = next_due(rule, now, due if rule.get("kind") == "interval" else None)
                values["next_due"] = following.astimezone(datetime.UTC).isoformat(timespec="milliseconds")
    if values:
        values.update(revision=task.get("revision", 0) + 1, updated_at=stamp)
    return values


def _change(event: dict, table: str, row: dict, values: dict) -> None:
    values = {key: value for key, value in values.items() if key in row}
    if values:
        event["original_rows"].setdefault(table, []).append(row)
        event["updates"].append({"table": table, "id": row["id"], "values": values})


def _rows(connection, table: str, where: str = "1=1") -> list[dict]:
    if table not in _tables(connection):
        return []
    return [dict(row) for row in connection.execute("SELECT * FROM " + table + " WHERE " + where)]


def _workflow_changes(connection, event: dict, runs: set[str]) -> None:
    stamp = event["at"]
    items = [row for row in _rows(connection, "workflow_items") if row["state"] == "leased"
             or (row["state"] == "waiting_execution" and row["run_id"] in runs)]
    identities = {row["id"] for row in items}
    for row in items:
        _change(event, "workflow_items", row, {"state": "recovering"})
    for row in _rows(connection, "workflow_attempts", "state IN ('leased','waiting_execution')"):
        if row["state"] == "leased" or row["item_id"] in identities:
            _change(event, "workflow_attempts", row, {"state": "obsolete", "finished_at": stamp})
    for row in _rows(connection, "workflow_operations", "side_effect=1 AND state IN ('intended','unknown')"):
        if row["item_id"] in identities:
            _change(event, "workflow_operations", row, {"state": "unknown", "receipt_token": "relocated-" + uuid.uuid4().hex})
    workflows = {row["workflow_id"] for row in items}
    for row in _rows(connection, "workflow_runtime"):
        if row["id"] in workflows:
            _change(event, "workflow_runtime", row, {"revision": row["revision"] + 1, "updated_at": stamp,
                                                     "conclusion": row["conclusion"] if row["conclusion"] in ("violated", "mixed") else "incomplete"})


def _database_changes(connection, event: dict) -> None:
    stamp = event["at"]
    runs = _rows(connection, "runs", "status IN ('Queued','Running')")
    for run in runs:
        _change(event, "runs", run, {"status": "Fail" if run["failed"] else "Blocked", "cancel": 1, "pid": None,
                                     "claim_token": "", "owner_identity": "{}", "finished_at": stamp, "current": "",
                                     "reason": (run["reason"] + "；" if run["reason"] else "") + INTERRUPTED})
    for task in _rows(connection, "tasks"):
        _change(event, "tasks", task, _task_values(task, stamp))
    for row in _rows(connection, "host_outbox", "state IN ('pending','dispatching','unknown')"):
        payload = json.loads(row["payload"])
        parameters = payload.get("parameters", {})
        if isinstance(parameters, dict) and "host_session_id" in parameters:
            payload["parameters"] = {**parameters, "host_session_id": ""}
        _change(event, "host_outbox", row, {"state": "unknown", "owner": "", "token": "", "lease_until": None,
                                           "reason": UNCERTAIN, "updated_at": stamp, "payload": json.dumps(payload, ensure_ascii=False)})
    _workflow_changes(connection, event, {run["id"] for run in runs})
    for table, where in (("dialogue_judge_attempts", "state='leased'"), ("dialogue_judgements", "state IN ('queued','leased')")):
        for row in _rows(connection, table, where):
            _change(event, table, row, {"state": "cancelled", "finished_at": stamp})


def _sync_parents(file: Path, root: Path) -> None:
    if os.name != "nt":
        for folder in file.parents:
            descriptor = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if folder == root:
                break


def _durable_json(file: Path, value: dict, root: Path) -> None:
    data_transfer.write_json(file, value)
    _sync_parents(file, root)


def _prepare(root: Path, previous: dict, current: dict) -> dict:
    identity = uuid.uuid4().hex
    event = {"schema_version": 1, "id": identity, "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds"),
             "previous_location": previous, "location": current, "original_rows": {}, "updates": [], "runtime_files": []}
    database = root / ".local/autoskill.db"
    if database.is_file():
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            _database_changes(connection, event)
    for name in RUNTIME_FILES:
        source = root / ".local" / name
        if source.exists() or source.is_symlink():
            event["runtime_files"].append({"source": ".local/" + name, "saved": f".local/relocations/{identity}/runtime/{name}"})
    event.update(runs=event["original_rows"].get("runs", []), tasks=event["original_rows"].get("tasks", []))
    file = root / ".local/relocations" / identity / "event.json"
    _durable_json(file, event, root)
    _durable_json(root / ".local/relocation-pending.json", {"event_id": identity, "sha256": data_transfer.digest(file)}, root)
    return event


def _pending(root: Path) -> dict | None:
    value = data_transfer.read_json(root / ".local/relocation-pending.json")
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError("数据重新附着日志指针格式无效，原状态未覆盖")
    identity = value.get("event_id")
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("数据重新附着日志标识无效，原状态未覆盖")
    file = root / ".local/relocations" / identity / "event.json"
    if data_transfer.digest(file) != value.get("sha256"):
        raise ValueError("数据重新附着日志校验失败，原状态未覆盖")
    event = data_transfer.read_json(file)
    if event.get("schema_version") != 1 or event.get("id") != identity:
        raise ValueError("数据重新附着日志格式无效，原状态未覆盖")
    return event


def _apply_database(root: Path, event: dict) -> None:
    database = root / ".local/autoskill.db"
    if not database.is_file():
        if event["updates"]:
            raise ValueError("数据重新附着期间运行库丢失，未丢弃原状态日志")
        return
    with contextlib.closing(sqlite3.connect(database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS data_relocation_receipts (id TEXT PRIMARY KEY,applied_at TEXT NOT NULL)")
        if connection.execute("SELECT 1 FROM data_relocation_receipts WHERE id=?", (event["id"],)).fetchone():
            return
        for table, rows in event["original_rows"].items():
            if table not in TABLES:
                raise ValueError("数据重新附着日志包含未知运行表")
            for original in rows:
                actual = connection.execute("SELECT * FROM " + table + " WHERE id=?", (original["id"],)).fetchone()
                if actual is None or any(actual[key] != value for key, value in original.items()):
                    raise ValueError("重新附着准备后运行态已变化，未覆盖新状态：" + table + "/" + str(original["id"]))
        for update in event["updates"]:
            table, values = update["table"], update["values"]
            if table not in TABLES or any(key not in {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")} for key in values):
                raise ValueError("数据重新附着日志包含未知运行字段")
            connection.execute("UPDATE " + table + " SET " + ",".join(key + "=?" for key in values) + " WHERE id=?", (*values.values(), update["id"]))
        connection.execute("INSERT INTO data_relocation_receipts(id,applied_at) VALUES(?,?)", (event["id"], event["at"]))


def _archive_runtime(root: Path, event: dict) -> None:
    for item in event["runtime_files"]:
        source, target = data_refs.resolve(item["source"], root), data_refs.resolve(item["saved"], root)
        if target.exists():
            if source.exists():
                raise ValueError("重新附着恢复时原运行目录又出现新内容，未覆盖归档：" + item["source"])
            continue
        if not source.exists():
            raise ValueError("重新附着恢复时运行目录缺失，原日志已保留：" + item["source"])
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        _sync_parents(target, root)
        _sync_parents(source, root)


def _finish(root: Path, event: dict) -> dict:
    _apply_database(root, event)
    _archive_runtime(root, event)
    _durable_json(root / ".local/runtime-location.json", {**event["location"], "attached_at": event["at"], "attachment_id": event["id"]}, root)
    pending = root / ".local/relocation-pending.json"
    pending.unlink()
    _sync_parents(pending, root)
    return {"relocated": bool(event["previous_location"] or event["updates"] or event["runtime_files"]), "event_id": event["id"]}


def attach(root: Path, *, force: bool = False, location_root: Path | None = None) -> dict:
    """Journal before changing runtime state, and resume without replaying business operations."""
    root = root.resolve()
    current = {"machine_id": runtime_identity.machine_id(), "location_id": runtime_identity.location_id(location_root or root)}
    with maintenance.exclusive(root):
        recovered = _finish(root, event) if (event := _pending(root)) else None
        previous = data_transfer.read_json(root / ".local/runtime-location.json")
        if (not force or recovered) and all(previous.get(key) == value for key, value in current.items()):
            return recovered or {"relocated": False}
        return _finish(root, _prepare(root, previous, current))
