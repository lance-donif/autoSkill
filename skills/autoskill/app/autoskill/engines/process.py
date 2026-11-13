"""Native subprocess supervision, durable events and cancellation of the complete process tree."""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .. import paths, runtime_identity
from .process_job import ProcessJob


def stamp() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def event(folder: Path, kind: str, **data) -> dict:
    entry = {"type": kind, "at": stamp(), **data}
    with (folder / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def terminate(child: subprocess.Popen) -> None:
    """Stop descendants as well as their launcher, then reap the launcher."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], check=False,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=2)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=15)


def _monitor(child: subprocess.Popen, spec: dict, folder: Path) -> str:
    from ..runner import process_alive

    start, heartbeat = time.monotonic(), 0.0
    while child.poll() is None:
        now = time.monotonic()
        cause = ("cancelled" if (folder / "cancel").exists() else "owner_exited" if not process_alive(spec["owner_pid"])
                 or not runtime_identity.owns(spec["owner_pid"], spec.get("owner_identity"))
                 else "timeout" if spec.get("timeout") and now - start >= spec["timeout"] else "")
        if cause:
            terminate(child)
            return cause
        if now - heartbeat >= 1:
            write_json(folder / "heartbeat.json", {"at": stamp(), "pid": child.pid, "owner_identity": runtime_identity.current(child.pid), "elapsed_seconds": now - start})
            heartbeat = now
        time.sleep(0.1)
    return "completed"


def worker(spec_file: str) -> None:
    """A separate supervisor also terminates a native runtime after its autoSkill owner dies."""
    spec = json.loads(Path(spec_file).read_text(encoding="utf-8"))
    if not runtime_identity.owns(spec.get("owner_pid"), spec.get("owner_identity")):
        return
    folder = Path(spec["evidence"])
    write_json(folder / "supervisor.json", {"pid": os.getpid(), "owner_identity": runtime_identity.current()})
    child = None
    job = None
    started = time.monotonic()
    result = {"state": "blocked", "exit_code": None}
    try:
        job = ProcessJob()
        argv = spec["argv"]
        if os.name == "nt":
            spec["launcher_owner_pid"] = os.getpid()
            spec["launcher_owner_identity"] = runtime_identity.current()
            write_json(Path(spec_file), spec)
            argv = [sys.executable, "-m", "autoskill.engines.process", "--windows-launch", spec_file]
        with (folder / "stdout.log").open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
            child = subprocess.Popen(argv, cwd=spec["cwd"], env={**os.environ, **spec.get("env", {})},
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     start_new_session=os.name != "nt", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            job.assign(child.pid)
            if os.name == "nt":
                (folder / "launch-ready").touch()
            event(folder, "started", pid=child.pid)
            state = _monitor(child, spec, folder)
            result = {"state": state, "exit_code": child.returncode}
            if state in ("timeout", "owner_exited"):
                result["reason"] = "原生执行超过配置的整体时限" if state == "timeout" else "autoSkill 运行进程已退出，原生进程树已终止"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["reason"] = f"原生进程无法完成：{type(error).__name__}: {error}"
    finally:
        if child and child.poll() is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                terminate(child)
        if child and os.name != "nt":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        if job:
            job.close()
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        write_json(folder / "process-result.json", result)
        event(folder, "finished", **result)


class Process:
    def __init__(self, prepared: dict, emit=None):
        self.folder = Path(prepared["evidence"])
        self.folder.mkdir(parents=True, exist_ok=True)
        self.emit = emit or (lambda _value: None)
        spec = {**prepared, "owner_pid": os.getpid(), "owner_identity": runtime_identity.current()}
        spec_file = self.folder / "process-spec.json"
        write_json(spec_file, spec)
        self.child = subprocess.Popen([sys.executable, "-m", "autoskill.engines.process", str(spec_file)],
                                      env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(paths.APP), os.environ.get("PYTHONPATH")]))},
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        write_json(self.folder / "supervisor.json", {"pid": self.child.pid, "owner_identity": runtime_identity.current(self.child.pid)})
        self.emit({"type": "supervisor_started", "pid": self.child.pid})

    def events(self) -> list[dict]:
        file = self.folder / "events.jsonl"
        if not file.is_file():
            return []
        entries = []
        for line in file.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                entries.append(json.loads(line))
        return entries

    def heartbeat(self) -> dict:
        file = self.folder / "heartbeat.json"
        return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}

    def cancel(self) -> None:
        (self.folder / "cancel").touch()

    def wait(self, cancelled=None) -> dict:
        seen = 0
        while self.child.poll() is None:
            if cancelled and cancelled():
                self.cancel()
            entries = self.events()
            for entry in entries[seen:]:
                self.emit(entry)
            seen = len(entries)
            time.sleep(0.1)
        self.child.wait()
        result = self.folder / "process-result.json"
        if not result.is_file():
            return {"state": "blocked", "exit_code": self.child.returncode, "reason": "原生进程监督器退出但没有产生回执"}
        return json.loads(result.read_text(encoding="utf-8"))

    def cleanup(self) -> None:
        if self.child.poll() is None:
            self.cancel()
            self.child.wait(timeout=30)


def windows_launch(spec_file: str) -> int:
    """Wait for job assignment before any user executable can create descendants."""
    from ..runner import process_alive

    spec = json.loads(Path(spec_file).read_text(encoding="utf-8"))
    while True:
        if (not process_alive(spec["launcher_owner_pid"])
                or not runtime_identity.owns(spec["launcher_owner_pid"], spec.get("launcher_owner_identity"))):
            return 2
        if (Path(spec["evidence"]) / "launch-ready").exists():
            break
        time.sleep(0.05)
    return subprocess.call(spec["argv"], stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    if sys.argv[1] == "--windows-launch":
        sys.exit(windows_launch(sys.argv[2]))
    worker(sys.argv[1])
