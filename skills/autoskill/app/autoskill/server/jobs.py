"""Run queue: start queued runs in their own processes, fire schedules, notice runs that died."""
from __future__ import annotations

import contextlib
import errno
import json
import os
import subprocess
import sys
import threading
import time
import uuid

from .. import db, deletion, execution_inputs, paths, runner, sync_import
from ..locking import named_lock
from . import scheduler

_dispatchers: dict[int, subprocess.Popen] = {}
_dispatcher_lock = threading.Lock()
_report_children: dict[str, tuple[subprocess.Popen, str]] = {}
_report_lock = threading.RLock()
_checkpoint_after = 0.0


def _state_file():
    return paths.workspace() / ".local" / "queue-worker.json"


def _state() -> dict:
    try:
        value = json.loads(_state_file().read_bytes())
        return value if isinstance(value, dict) and isinstance(value.get("pid"), int) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _worker_lock():
    """A lifetime OS lock is authoritative even when a stale PID has been reused."""
    file = paths.local() / "queue-worker.lock"
    with file.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            def acquire():
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield False
            return
        try:
            yield True
        finally:
            release()


def _launch_dispatcher(token: str) -> subprocess.Popen:
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    child = subprocess.Popen([sys.executable, "-m", "autoskill.server.jobs", "--token", token], cwd=str(paths.APP),
                             env=paths.child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=os.name != "nt")
    with _dispatcher_lock:
        _dispatchers[child.pid] = child
    return child


def _reap_dispatchers() -> None:
    with _dispatcher_lock:
        for pid, child in list(_dispatchers.items()):
            if child.poll() is not None:
                _dispatchers.pop(pid, None)


def ensure_worker() -> dict:
    """Start one independent queue drainer after enqueue commits; startup failure never loses the run ID."""
    _reap_dispatchers()
    with named_lock("queue-dispatcher", "startup"):
        deadline, child, token = time.monotonic() + 10, None, uuid.uuid4().hex
        while time.monotonic() < deadline:
            if child is None:
                with _worker_lock() as free:
                    state = _state()
                    if not free and runner.process_alive(state.get("pid")):
                        return {"status": "running", "pid": state["pid"], "reason": ""}
                    if free:
                        _state_file().unlink(missing_ok=True)
                if free:
                    try:
                        child = _launch_dispatcher(token)
                    except OSError as error:
                        return {"status": "unavailable", "pid": None, "reason": "后台队列无法启动：" + type(error).__name__}
            else:
                # The child writes its token only after taking the lifetime lock. Probing
                # that lock during startup can make the child mistake us for a dispatcher.
                state = _state()
                if state.get("token") == token and state.get("pid") == child.pid and child.poll() is None:
                    return {"status": "started", "pid": child.pid, "reason": ""}
            if child is not None and child.poll() is not None:
                return {"status": "unavailable", "pid": None, "reason": "后台队列在启动期间退出；运行已保存，可重试启动现有队列"}
            time.sleep(0.02)
        return {"status": "unavailable", "pid": None, "reason": "后台队列启动未确认；运行已保存，可重试启动现有队列"}


def _pending() -> bool:
    from .. import workflow

    with db.batch():
        return bool(db.one("SELECT id FROM runs WHERE status IN ('Queued', 'Running') OR delete_requested = 1 LIMIT 1")
                    or db.one("SELECT id FROM tasks WHERE enabled = 1 LIMIT 1") or workflow.needs_monitoring()
                    or _reports_running())


def _reports_running() -> bool:
    from .. import task_reports

    with _report_lock:
        workspace = str(paths.workspace())
        current = _report_children.get(workspace)
        if current is None:
            return False
        child, report_id = current
        code = child.poll()
        if code is None:
            return True
        _report_children.pop(workspace, None)
        saved = task_reports._saved(report_id)
        if saved and saved.get("status") == "generating":
            if saved.get("render_owner_identity"):
                task_reports.fail_interrupted_export(report_id)
            else:
                task_reports._persist_failure(
                    {**task_reports._base("workflow", saved["task_id"], report_id), "source_hash": saved.get("source_hash"), "snapshot_required": True},
                    RuntimeError(f"报告生成进程提前退出（退出码 {code}），请手动重试"), saved.get("generation"))
        return False


def _start_workflow_report(report_ids: list[str]) -> None:
    """Render frozen reports serially outside the state coordination loop."""
    from .. import task_reports

    with _report_lock:
        if _reports_running():
            return
        pending = [report_id for report_id in report_ids if (saved := (task_reports._saved(report_id) or {})).get("status") == "generating"
                   and not saved.get("render_owner_identity")]
        if not pending:
            return
        report_id = pending[0]
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "autoskill.server.jobs", "--workflow-report", report_id], cwd=str(paths.APP),
                env=paths.child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags, start_new_session=os.name != "nt")
            _report_children[str(paths.workspace())] = (child, report_id)
        except OSError as error:
            saved = task_reports._saved(report_id)
            if saved:
                task_reports._persist_failure(
                    {**task_reports._base("workflow", saved["task_id"], report_id), "source_hash": saved.get("source_hash"), "snapshot_required": True},
                    error, saved.get("generation"))


def reconcile_workflows() -> dict:
    """Collect terminal execution into workflow state independently of host polling."""
    from .. import task_reports, workflow

    result = workflow.reconcile_pending()
    pending = workflow.pending_reports()
    for report_id in pending:
        task_reports.fail_interrupted_export(report_id)
    _start_workflow_report([*pending, *workflow.prepare_pending_reports()])
    return result


def _idle_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get("AUTOSKILL_QUEUE_IDLE_SECONDS", "3")))
    except ValueError:
        return 3.0


def _dispatch(token: str) -> None:
    database = paths.workspace() / ".local" / "autoskill.db"
    if not database.is_file():
        return
    with _worker_lock() as free:
        if not free:
            return
        execution_inputs.write_json(_state_file(), {"pid": os.getpid(), "token": token})
        try:
            initialized = False
            idle_since = time.monotonic()
            next_review, observed = 0.0, None
            while True:
                if not database.is_file():
                    return
                try:
                    if not initialized:
                        db.init()
                        initialized = True
                    next_review, observed = _queue_pass(next_review, observed)
                    if _pending():
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since >= _idle_seconds():
                        # Enqueue commits before ensure_worker takes this same lock. Recheck while
                        # holding it, so a caller cannot be told an exiting worker will drain its run.
                        with named_lock("queue-dispatcher", "startup"):
                            if not _pending():
                                _state_file().unlink(missing_ok=True)
                                return
                except Exception:  # noqa: BLE001  # Transient queue or filesystem errors must not abandon already committed work.
                    time.sleep(0.2)
                    continue
                time.sleep(0.2)
        finally:
            if _state().get("token") == token:
                _state_file().unlink(missing_ok=True)
            runner.reap_children()


def alive(pid: int | None) -> bool:
    return runner.process_alive(pid)


def _reap_recorders_and_dispatchers() -> None:
    _reap_dispatchers()
    capture = sys.modules.get("autoskill.recorder.capture")
    if capture is not None:
        capture.reap_children()


def sweep() -> None:
    """A run whose process is gone can never finish; say so instead of showing it as running forever."""
    _reap_recorders_and_dispatchers()
    runner.recover_runs()
    deletion.sweep()


def _checkpoint_if_large(now: float) -> None:
    global _checkpoint_after  # noqa: PLW0603  # One idle maintenance deadline per worker process.
    if now < _checkpoint_after:
        return
    _checkpoint_after = now + 60
    wal = paths.workspace() / ".local" / "autoskill.db-wal"
    with contextlib.suppress(FileNotFoundError):
        if wal.stat().st_size >= 64 * 1024 * 1024:
            # Do not force readers out of their snapshots or wait to truncate a live WAL.
            with db.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()


def _queue_pass(next_review: float, observed: int | None) -> tuple[float, int]:
    # A pass keeps the shared maintenance gate but each transaction commits independently.
    with db.batch():
        scheduler.tick()
        activity = db.one("SELECT revision,"
                          "EXISTS(SELECT 1 FROM runs WHERE status = 'Running') AS running,"
                          "EXISTS(SELECT 1 FROM runs WHERE status = 'Queued') AS queued,"
                          "EXISTS(SELECT 1 FROM runs WHERE delete_requested = 1) AS deleting "
                          "FROM state_revision WHERE id=1")
        _reap_recorders_and_dispatchers()
        if activity["running"]:
            runner.recover_runs()
        else:
            runner.reap_children()
        if activity["deleting"]:
            deletion.sweep()
        now = time.monotonic()
        review_due = activity["revision"] != observed or now >= next_review
        if review_due:
            reconcile_workflows()
            next_review, observed = now + 0.4, activity["revision"]
        # Reconciliation can enqueue work in this pass.
        if activity["queued"] or (review_due and db.one("SELECT 1 FROM runs WHERE status = 'Queued' LIMIT 1")):
            start_queued()
        elif not (activity["running"] or activity["deleting"]):
            _checkpoint_if_large(now)
    sync_import.autoscan()
    return next_review, observed


def start_queued() -> None:
    """Start queued runs oldest first; the shared atomic claim enforces the configurable process budget."""
    from .. import task_reports

    with db.connect() as connection:
        cancelled = connection.execute("UPDATE runs SET status = 'Cancelled', finished_at = ? WHERE status = 'Queued' AND cancel = 1 RETURNING id", (db.now(),)).fetchall()
    for run in cancelled:
        task_reports.for_run(run["id"])

    with db.connect() as connection:
        connection.execute("BEGIN")
        active = connection.execute("SELECT family, project_id, account_lock_key FROM runs WHERE status = 'Running'").fetchall()
        slots = runner.parallel_limit() - len(active)
        if slots <= 0:
            return
        replay_projects = {row["project_id"] for row in active if row["family"] == "replay"}
        replay_accounts = {row["account_lock_key"] for row in active if row["family"] == "replay" and row["account_lock_key"]}
        candidates = []
        for row in connection.execute("SELECT id, family, project_id, account_lock_key FROM runs WHERE status = 'Queued' "
                                      "AND cancel = 0 AND delete_requested = 0 ORDER BY created_at, rowid"):
            if row["family"] == "replay":
                lock_key = row["account_lock_key"] or ""
                if row["project_id"] in replay_projects or (lock_key and lock_key in replay_accounts):
                    continue
                replay_projects.add(row["project_id"])
                if lock_key:
                    replay_accounts.add(lock_key)
            candidates.append(row["id"])
            if len(candidates) == slots:
                break
    # A CLI caller or another dispatcher can take a slot after this snapshot.
    for run_id in candidates:
        runner.spawn(run_id)


def cancel(run_id: str) -> dict:
    from .. import task_reports

    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise ValueError("运行不存在")
    # Judge status at the write, so cancellation cannot overwrite a concurrent claim or final result.
    db.execute(
        "UPDATE runs SET cancel = 1, finished_at = CASE WHEN status = 'Queued' THEN ? ELSE finished_at END, "
        "status = CASE WHEN status = 'Queued' THEN 'Cancelled' ELSE status END "
        "WHERE id = ? AND status IN ('Queued', 'Running')", (db.now(), run_id))
    result = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if result is None:
        raise ValueError("运行已删除")
    return {**result, "test_report": task_reports.for_run(run_id)}


class Worker:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="autoskill-jobs")

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        from .. import workflow

        self.stop.set()
        if self.thread.ident is None:
            return
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        current = _report_children.get(str(paths.workspace()))
        if current is not None:
            child, _report_id = current
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                ensure_worker()
        # Committed frozen receipts remain discoverable by the independent dispatcher.
        if _reports_running() or workflow.pending_reports():
            ensure_worker()

    def loop(self) -> None:
        next_review, observed = 0.0, None
        while not self.stop.is_set():
            with contextlib.suppress(Exception):  # Durable state lets the next pass retry independently.
                next_review, observed = _queue_pass(next_review, observed)
            self.stop.wait(0.2)


if __name__ == "__main__":
    if "--workflow-report" in sys.argv:
        from .. import task_reports

        task_reports.finish_workflow_report(sys.argv[sys.argv.index("--workflow-report") + 1])
    else:
        _dispatch(sys.argv[sys.argv.index("--token") + 1] if "--token" in sys.argv else uuid.uuid4().hex)
