"""Durable per-item deletion, executor fencing and reference-aware local evidence cleanup."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from . import data_refs, db, execution_inputs, maintenance, paths, projects, runtime_identity
from .locking import file_lock, named_lock, project_lock

IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
FAILURES = (OSError, ValueError, TypeError, KeyError, sqlite3.Error)


def _identity(value: str) -> str:
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError("标识无效：请使用列表中实际存在的标识")
    return value


def _execution_file(run_id: str) -> Path:
    name = hashlib.sha256(_identity(run_id).encode()).hexdigest()
    return paths.local() / "locks" / ("execution-" + name + ".lock")


def execution_guard(run_id: str):
    """Hold across claim, execution, adapter cleanup and sheet synchronization."""
    return file_lock(_execution_file(run_id))


@contextlib.contextmanager
def _try_execution(run_id: str):
    with maintenance.access(paths.workspace()):
        file = _execution_file(run_id)
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a+b") as stream:
            try:
                if os.name == "nt":
                    import msvcrt

                    if not os.fstat(stream.fileno()).st_size:
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                yield False
                return
            try:
                yield True
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _response() -> dict:
    return {"deleted": [], "pending": [], "errors": []}


def _error(identity: dict, error: Exception, *, requested: bool = False) -> dict:
    return {**identity, "code": "cleanup_failed" if requested else "delete_failed", "message": str(error),
            "retryable": requested, "delete_requested": requested}


def _sheet_folders(project_id: str, run: dict | None = None) -> list[str]:
    from .runner import plan_folder, plan_reference

    values = {str(paths.case_skill())}
    if run and run.get("plan_path"):
        values.add(run["plan_path"])
    for table in ("runs", "tasks"):
        values.update(row["plan_path"] for row in db.rows(f"SELECT DISTINCT plan_path FROM {table} WHERE project_id = ? AND plan_path != ''", (project_id,)))
    values.update(str(folder) for folder in (paths.local() / "case-plans").glob("*/autoskill-cases"))
    folders = [plan_folder(value) for value in values]
    return sorted({plan_reference(folder.parent if folder.suffix.lower() == ".xlsx" else folder) for folder in folders})


def _latest(project_id: str, excluded: str | None) -> dict:
    rows = db.rows("SELECT r.* FROM results r JOIN runs u ON u.id = r.run_id WHERE r.project_id = ? "
                   "AND r.prerequisite = 0 AND u.trigger != 'login' AND u.delete_requested = 0 AND r.run_id != ? ORDER BY r.id",
                   (project_id, excluded or ""))
    return {row["case_id"]: row for row in rows}


def _sheet_status(project_id: str, case_id: str, latest: dict, current: dict, deleted_case: bool) -> tuple[str, str]:
    from . import plan

    if deleted_case or case_id not in current:
        return "未执行", "关联的用例已删除，需重新关联可执行用例"
    result = latest.get(case_id)
    if not result:
        return "未执行", "没有保留的执行证据，需重新回放"
    if not execution_inputs.result_current(project_id, current[case_id], result):
        return "未执行", "用例已修改或执行证据不再有效，需重新回放"
    detail = json.loads(result["detail"])
    if result["status"] == "Pass" and not detail.get("business_verified"):
        return "未执行", "没有完整业务断言的实际通过证据"
    return plan.STATUS.get(result["status"], "未执行"), result.get("reason", "")


def _current_cases(project_id: str) -> dict:
    try:
        return {case["id"]: case for case in projects.cases(project_id)}
    except ValueError:
        return {}


def _refresh_sheets(project_id: str, case_ids: list[str], folders: list[str], *, excluded: str | None = None, deleted_case: bool = False) -> None:
    from . import plan
    from .runner import plan_folder

    for value in folders:
        folder = plan_folder(value)
        if not folder.is_dir() or not any(folder.glob("*.xlsx")):
            continue
        token = plan._selected_folder.set(folder)
        try:
            with plan._sheet_lock():
                book, sheet, columns = plan._open()
                try:
                    current = _current_cases(project_id) if not deleted_case else {}
                    latest = _latest(project_id, excluded)
                    changed = False
                    for number, row in plan._rows(sheet, columns):
                        linked_project, _, linked_case = row["replay"].partition("/")
                        if linked_project == project_id and linked_case in case_ids:
                            status, note = _sheet_status(project_id, linked_case, latest, current, deleted_case)
                            plan._set_status(sheet, columns, number, status, note)
                            changed = True
                    if changed:
                        plan._save(book)
                finally:
                    book.close()
        finally:
            plan._selected_folder.reset(token)


def _reports(project_id: str) -> None:
    folder = _managed_directory("reports") / _identity(project_id)
    if folder.is_symlink() or not folder.resolve().is_relative_to(paths.local().resolve()):
        raise ValueError("报告目录是外部链接，未删除链接目标")
    for file in folder.glob(project_id + "-测试报告-*.*"):
        if file.suffix in (".xlsx", ".md") and file.is_file():
            file.unlink()


def delete_cases(items: list[dict]) -> dict:
    if not isinstance(items, list):
        raise ValueError("items 必须是包含 project_id、case_id 的数组")
    db.init()
    result = _response()
    for item in items:
        identity = {key: item.get(key) for key in ("project_id", "case_id")} if isinstance(item, dict) else {"item": item}
        try:
            if not isinstance(item, dict):
                raise ValueError("每个用例需要 project_id 和 case_id")
            project_id, case_id = item.get("project_id"), item.get("case_id")
            deleted = projects.delete_case(project_id, case_id)
            warnings = []
            try:
                _refresh_sheets(project_id, [case_id], _sheet_folders(project_id), deleted_case=True)
                with project_lock(project_id):
                    _reports(project_id)
            except FAILURES as error:
                warnings.append("用例已删除；派生展示刷新未完成：" + str(error))
            result["deleted"].append({**deleted, "warnings": warnings})
        except FAILURES as error:
            result["errors"].append(_error(identity, error))
    return result


def _tables(connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _cancel_judges(connection, run_id: str) -> None:
    if "dialogue_judgements" not in _tables(connection):
        return
    connection.execute("UPDATE dialogue_judgements SET state = 'cancelled', finished_at = ? WHERE run_id = ? AND state IN ('queued','leased')", (db.now(), run_id))
    connection.execute("UPDATE dialogue_judge_attempts SET state = 'cancelled', finished_at = ? WHERE judge_id IN "
                       "(SELECT id FROM dialogue_judgements WHERE run_id = ?) AND state = 'leased'", (db.now(), run_id))
    connection.execute("UPDATE host_outbox SET state = 'cancelled', updated_at = ? WHERE occurrence_id IN "
                       "(SELECT id FROM dialogue_judgements WHERE run_id = ?) AND state != 'delivered'", (db.now(), run_id))


def _request(run_id: str) -> dict | None:
    run = db.one("SELECT project_id FROM runs WHERE id = ?", (run_id,))
    if not run:
        return None
    with project_lock(run["project_id"]), db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE runs SET delete_requested = 1, deletion_error = '', cancel = 1, "
                           "finished_at = CASE WHEN status = 'Queued' THEN ? ELSE finished_at END, "
                           "status = CASE WHEN status = 'Queued' THEN 'Cancelled' ELSE status END WHERE id = ?", (db.now(), run_id))
        _cancel_judges(connection, run_id)
        _unlink_workflows(connection, run_id)
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def _native_active(run_id: str) -> bool:
    folder = _managed_directory("runs") / run_id / "native"
    if folder.parent.is_symlink():
        return False
    if folder.is_symlink():
        raise ValueError("原生执行目录是外部链接，未删除链接目标")
    if not folder.is_dir():
        return False
    supervisor_file = folder / "supervisor.json"
    supervisor = _owned_marker(supervisor_file)
    unfinished = not (folder / "process-result.json").is_file()
    launching = not supervisor_file.exists() and _owned_marker(folder / "process-spec.json", "owner_pid")
    active = supervisor or (unfinished and (_owned_marker(folder / "heartbeat.json") or launching))
    if active:
        (folder / "cancel").touch()
    return active


def _owned_marker(file: Path, pid_key: str = "pid") -> bool:
    if not file.is_file():
        return False
    value = json.loads(file.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("原生进程归属标记格式无效，未删除执行证据")
    return runtime_identity.owns(value.get(pid_key), value.get("owner_identity"))


def _managed_directory(name: str) -> Path:
    folder = paths.local() / name
    if folder.is_symlink() or not folder.resolve().is_relative_to(paths.local().resolve()):
        raise ValueError("执行文件目录是外部链接，未删除链接目标")
    return folder


def _input_folder(run: dict) -> Path:
    return _managed_directory("native-inputs" if run.get("family") == "native" else "execution-inputs")


def _objects_folder(folder: Path) -> Path:
    objects = folder / "objects"
    if objects.is_symlink() or not objects.resolve().is_relative_to(folder.resolve()):
        raise ValueError("冻结对象目录是外部链接，未删除链接目标")
    return objects


def _references(manifest: dict, native: bool) -> set[str]:
    references = manifest.get("frozen_files", []) if native else manifest.get("sources", {}).get("files", {}).values()
    found = {ref["sha256"] for ref in references}
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in found):
        raise ValueError("冻结对象标识无效，未删除工作区外文件")
    return found


def _inventory(run: dict) -> dict:
    if run.get("deletion_manifest"):
        return json.loads(run["deletion_manifest"])
    value = run.get("input_manifest")
    objects = set()
    if value:
        file, folder = data_refs.resolve(value), _input_folder(run)
        if file.parent.resolve() != folder.resolve() or file.is_symlink() or file.suffix != ".json":
            raise ValueError("执行快照路径不属于此工作区，未删除任何外部文件")
        if file.is_file():
            objects = _references(json.loads(file.read_bytes()), run.get("family") == "native")
        _objects_folder(folder)
    case_ids = {row["case_id"] for row in db.rows("SELECT case_id FROM results WHERE run_id = ?", (run["id"],))}
    case_ids.update(json.loads(run["case_ids"]))
    inventory = {"input_manifest": data_refs.store(value) if value else value, "objects": sorted(objects), "case_ids": sorted(case_ids),
                 "plan_folders": _sheet_folders(run["project_id"], run)}
    db.update("runs", run["id"], {"deletion_manifest": inventory})
    return inventory


def _remove_inputs(run: dict, inventory: dict) -> None:
    folder, name = _input_folder(run), inventory.get("input_manifest")
    if not name:
        return
    file = data_refs.resolve(name)
    if file.parent.resolve() != folder.resolve() or file.is_symlink():
        raise ValueError("执行快照路径不属于此工作区")
    if _shared_input(run["id"], file):
        return
    objects = _objects_folder(folder)
    file.unlink(missing_ok=True)
    used = set()
    for manifest in folder.glob("*.json"):
        used.update(_references(json.loads(manifest.read_bytes()), run.get("family") == "native"))
    for digest in set(inventory["objects"]) - used:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("冻结对象标识无效")
        (objects / digest).unlink(missing_ok=True)


def _shared_input(run_id: str, file: Path) -> bool:
    for row in db.rows("SELECT input_manifest FROM runs WHERE id != ? AND input_manifest != ''", (run_id,)):
        with contextlib.suppress(ValueError):
            if data_refs.resolve(row["input_manifest"]) == file:
                return True
    return False


def _remove_judges(connection, run_id: str) -> None:
    if "dialogue_judgements" not in _tables(connection):
        return
    identities = [row[0] for row in connection.execute("SELECT id FROM dialogue_judgements WHERE run_id = ?", (run_id,))]
    for identity in identities:
        connection.execute("DELETE FROM dialogue_judge_attempts WHERE judge_id = ?", (identity,))
        connection.execute("DELETE FROM host_outbox WHERE occurrence_id = ?", (identity,))
        connection.execute("DELETE FROM workflow_requests WHERE scope IN (?,?)", ("judge-submit:" + identity, "judge-control:" + identity))
    connection.execute("DELETE FROM dialogue_judgements WHERE run_id = ?", (run_id,))
    connection.execute("DELETE FROM workflow_requests WHERE scope IN (?,?)", ("judge-request:" + run_id, "judge-cancel:" + run_id))
    connection.execute("DELETE FROM workflow_requests WHERE scope LIKE 'judge-claim:%' AND json_valid(response) "
                       "AND json_extract(response, '$.task.run_id') = ?", (run_id,))


def _unlink_workflows(connection, run_id: str) -> None:
    if "workflow_items" not in _tables(connection):
        return
    for item in connection.execute("SELECT * FROM workflow_items WHERE run_id = ?", (run_id,)).fetchall():
        context = json.loads(item["context"])
        context["evidence_deleted"] = {"run_id": run_id, "at": db.now()}
        waiting = item["state"] == "waiting_execution"
        connection.execute("UPDATE workflow_items SET run_id = NULL, state = ?, context = ? WHERE id = ?",
                           ("cancelled" if waiting else item["state"], json.dumps(context, ensure_ascii=False), item["id"]))
        connection.execute("UPDATE workflow_runtime SET conclusion = 'incomplete', lifecycle = CASE WHEN ? THEN 'paused' ELSE lifecycle END, "
                           "updated_at = ?, revision = revision + 1 WHERE id = ? AND cycle_id = ?", (waiting, db.now(), item["workflow_id"], item["cycle_id"]))


def _remove_rows(run: dict) -> None:
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _remove_judges(connection, run["id"])
        _unlink_workflows(connection, run["id"])
        connection.execute("DELETE FROM results WHERE run_id = ?", (run["id"],))
        connection.execute("UPDATE schedule_occurrences SET run_id = NULL, status = 'deleted', reason = '执行记录已删除' WHERE run_id = ?", (run["id"],))
        connection.execute("UPDATE tasks SET last_run_id = (SELECT id FROM runs WHERE task_id = tasks.id AND id != ? "
                           "AND delete_requested = 0 ORDER BY created_at DESC,id DESC LIMIT 1) WHERE last_run_id = ?", (run["id"], run["id"]))
        connection.execute("INSERT OR IGNORE INTO deleted_runs(id,deleted_at) VALUES(?,?)", (run["id"], db.now()))
        connection.execute("DELETE FROM runs WHERE id = ? AND delete_requested = 1", (run["id"],))


def _pending(run_id: str) -> dict:
    return {"run_id": run_id, "status": "cancelling", "message": "已请求取消，执行结束后自动删除记录和对应文件"}


def cleanup(run_id: str) -> dict:
    """Retry one persistent deletion after its executor and native child have stopped."""
    _identity(run_id)
    with _try_execution(run_id) as acquired:
        if not acquired:
            return {"pending": _pending(run_id)}
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run:
            return {"deleted": {"run_id": run_id, "already_deleted": True}}
        if not run["delete_requested"]:
            return {}
        if (run["status"] == "Running" and runtime_identity.owns(run["pid"], run.get("owner_identity"))) or _native_active(run_id):
            return {"pending": _pending(run_id)}
        family = "native" if run.get("family") == "native" else "replay"
        with named_lock("input-store", family):
            inventory = _inventory(run)
        _refresh_sheets(run["project_id"], inventory["case_ids"], inventory["plan_folders"], excluded=run_id)
        with project_lock(run["project_id"]), named_lock("input-store", family):
            evidence = _managed_directory("runs") / run_id
            if evidence.is_symlink():
                evidence.unlink()
            elif evidence.exists():
                shutil.rmtree(evidence)
            _remove_inputs(run, inventory)
            _reports(run["project_id"])
            _remove_rows(run)
        return {"deleted": {"run_id": run_id, "already_deleted": False}}


def _cleanup_result(run_id: str) -> dict:
    try:
        return cleanup(run_id)
    except FAILURES as error:
        db.execute("UPDATE runs SET deletion_error = ? WHERE id = ? AND delete_requested = 1", (str(error), run_id))
        return {"errors": _error({"run_id": run_id}, error, requested=True)}


def delete_runs(run_ids: list[str], *, start_worker: bool = True) -> dict:
    if not isinstance(run_ids, list):
        raise ValueError("run_ids 必须是运行标识数组")
    db.init()
    result = _response()
    requested = False
    for run_id in run_ids:
        try:
            _identity(run_id)
            run = _request(run_id)
            requested = requested or bool(run)
            outcome = _cleanup_result(run_id)
            for key, value in outcome.items():
                result[key].append(value)
        except FAILURES as error:
            result["errors"].append(_error({"run_id": run_id}, error))
    if requested and start_worker and (result["pending"] or result["errors"]):
        from .server import jobs

        result["worker"] = jobs.ensure_worker()
    return result


def sweep() -> None:
    for row in db.rows("SELECT id FROM runs WHERE delete_requested = 1"):
        _cleanup_result(row["id"])
