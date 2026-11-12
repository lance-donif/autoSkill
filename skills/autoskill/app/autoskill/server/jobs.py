"""Run queue: start queued runs in their own processes, fire schedules, notice runs that died."""
from __future__ import annotations

import ctypes
import os
import threading

from .. import db, runner
from . import scheduler

MAX_PARALLEL = 2


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def sweep() -> None:
    """A run whose process is gone can never finish; say so instead of showing it as running forever."""
    for run in db.rows("SELECT id, pid FROM runs WHERE status = 'Running'"):
        if not alive(run["pid"]):
            db.update("runs", run["id"], {"status": "Blocked", "current": "", "finished_at": db.now(),
                                          "reason": "运行进程意外结束（可能是电脑休眠或进程被关闭），请重新运行"})


def start_queued() -> None:
    """Start queued runs, oldest first: at most MAX_PARALLEL at once and one per project (they share a login)."""
    running = db.rows("SELECT project_id FROM runs WHERE status = 'Running'")
    busy = {r["project_id"] for r in running}
    slots = MAX_PARALLEL - len(running)
    for run in db.rows("SELECT id, project_id, cancel FROM runs WHERE status = 'Queued' ORDER BY created_at"):
        if run["cancel"]:
            db.update("runs", run["id"], {"status": "Cancelled", "finished_at": db.now()})
            continue
        if slots <= 0 or run["project_id"] in busy:
            continue
        # mark first so the next pass does not start it twice, then record the child's pid for the sweep
        db.update("runs", run["id"], {"status": "Running", "started_at": db.now(), "pid": os.getpid()})
        db.update("runs", run["id"], {"pid": runner.spawn(run["id"])})
        busy.add(run["project_id"])
        slots -= 1


def cancel(run_id: str) -> dict:
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise ValueError("运行不存在")
    if run["status"] == "Queued":
        db.update("runs", run_id, {"status": "Cancelled", "cancel": 1, "finished_at": db.now()})
    elif run["status"] == "Running":
        db.update("runs", run_id, {"cancel": 1})  # the runner stops after the case in progress
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
            except Exception:
                pass  # state is durable; the next pass retries
