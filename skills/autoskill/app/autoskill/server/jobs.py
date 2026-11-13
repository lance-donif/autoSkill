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

from .. import db, execution_inputs, paths, runner
from ..locking import named_lock
from . import scheduler

_dispatchers: dict[int, subprocess.Popen] = {}


def _state_file():
    return paths.local() / "queue-worker.json"


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
    _dispatchers[child.pid] = child
    return child


def ensure_worker() -> dict:
    """Start one independent queue drainer after enqueue commits; startup failure never loses the run ID."""
    for pid, child in list(_dispatchers.items()):
        if child.poll() is not None:
            _dispatchers.pop(pid, None)
    with named_lock("queue-dispatcher", "startup"):
        deadline, child, token = time.monotonic() + 10, None, uuid.uuid4().hex
        while time.monotonic() < deadline:
            with _worker_lock() as free:
                state = _state()
                if not free and runner.process_alive(state.get("pid")):
                    status = "started" if state.get("token") == token else "running"
                    return {"status": status, "pid": state["pid"], "reason": ""}
                if free and child is None:
                    _state_file().unlink(missing_ok=True)
            if free and child is None:
                try:
                    child = _launch_dispatcher(token)
                except OSError as error:
                    return {"status": "unavailable", "pid": None, "reason": "后台队列无法启动：" + type(error).__name__}
            if child is not None and child.poll() is not None:
                return {"status": "unavailable", "pid": None, "reason": "后台队列在启动期间退出；运行已保存，可重试启动现有队列"}
            time.sleep(0.02)
        return {"status": "unavailable", "pid": None, "reason": "后台队列启动未确认；运行已保存，可重试启动现有队列"}


def _pending() -> bool:
    return bool(db.one("SELECT id FROM runs WHERE status IN ('Queued', 'Running') LIMIT 1")
                or db.one("SELECT id FROM tasks WHERE enabled = 1 LIMIT 1"))


def _idle_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get("AUTOSKILL_QUEUE_IDLE_SECONDS", "3")))
    except ValueError:
        return 3.0


def _dispatch(token: str) -> None:
    with _worker_lock() as free:
        if not free:
            return
        execution_inputs.write_json(_state_file(), {"pid": os.getpid(), "token": token})
        try:
            db.init()
            idle_since = time.monotonic()
            while True:
                try:
                    scheduler.tick()
                    sweep()
                    start_queued()
                except Exception:  # noqa: BLE001  # Transient queue or filesystem errors must not abandon already committed work.
                    time.sleep(0.2)
                    continue
                if _pending():
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= _idle_seconds():
                    # Enqueue commits before ensure_worker takes this same lock. Recheck while
                    # holding it, so a caller cannot be told an exiting worker will drain its run.
                    with named_lock("queue-dispatcher", "startup"):
                        if not _pending():
                            _state_file().unlink(missing_ok=True)
                            return
                time.sleep(0.2)
        finally:
            if _state().get("token") == token:
                _state_file().unlink(missing_ok=True)
            runner.reap_children()


def alive(pid: int | None) -> bool:
    return runner.process_alive(pid)


def sweep() -> None:
    """A run whose process is gone can never finish; say so instead of showing it as running forever."""
    runner.recover_runs()


def start_queued() -> None:
    """Start queued runs oldest first; the shared atomic claim enforces the configurable process budget."""
    db.execute("UPDATE runs SET status = 'Cancelled', finished_at = ? WHERE status = 'Queued' AND cancel = 1", (db.now(),))
    for run in db.rows("SELECT id FROM runs WHERE status = 'Queued' ORDER BY created_at"):
        runner.spawn(run["id"])


def cancel(run_id: str) -> dict:
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise ValueError("运行不存在")
    # Judge status at the write, so cancellation cannot overwrite a concurrent claim or final result.
    db.execute(
        "UPDATE runs SET cancel = 1, finished_at = CASE WHEN status = 'Queued' THEN ? ELSE finished_at END, "
        "status = CASE WHEN status = 'Queued' THEN 'Cancelled' ELSE status END "
        "WHERE id = ? AND status IN ('Queued', 'Running')", (db.now(), run_id))
    return db.one("SELECT * FROM runs WHERE id = ?", (run_id,))


class Worker:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="autoskill-jobs")

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()

    def loop(self) -> None:
        while not self.stop.wait(1.0):
            try:
                scheduler.tick()
                sweep()
                start_queued()
            except Exception:  # noqa: BLE001, S110  # Durable queue state lets the worker retry any failed pass without losing its thread.
                pass  # state is durable; the next pass retries


if __name__ == "__main__":
    _dispatch(sys.argv[sys.argv.index("--token") + 1] if "--token" in sys.argv else uuid.uuid4().hex)
