"""Run queue: start queued runs in their own processes, fire schedules, notice runs that died."""
from __future__ import annotations

import threading

from .. import db, runner
from . import scheduler


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
