"""Reentrant thread and process locks for shared local files and recording commands."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import maintenance, paths, task_context

_guard = threading.Lock()
_threads: dict[str, threading.RLock] = {}
_held = threading.local()


def _acquire(stream) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(0.05)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _release(stream) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: str | Path):
    """Wait for an advisory lock, keeping the lock file stable across all participants."""
    file = Path(path).resolve()
    key = os.path.normcase(str(file))
    with _guard:
        local_lock = _threads.setdefault(key, threading.RLock())
    with maintenance.access(paths.workspace()), maintenance.access(file.parent if file.name == ".autoskill-plan.lock" else paths.workspace()), local_lock:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if key in held:
            yield
            return
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a+b") as stream:
            _acquire(stream)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                _release(stream)


def named_lock(scope: str, key: str):
    """Use a shared lock identity without putting caller-provided text in a path."""
    identity = json.dumps([scope, key], ensure_ascii=False).encode("utf-8")
    name = hashlib.sha256(identity).hexdigest() + ".lock"
    return file_lock(paths.local() / "locks" / name)


def session_lock():
    return named_lock("recording", task_context.session_key())


def project_lock(project_id: str):
    return named_lock("project", project_id)
