"""Shared data access allows concurrency while whole-directory transfers hold an exclusive gate."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import threading
from functools import lru_cache
from pathlib import Path

_held = threading.local()


def gate_path(workspace: Path) -> Path:
    return _gate_file(Path.home(), os.path.normcase(str(workspace.resolve())))


@lru_cache(maxsize=64)
def _gate_file(home: Path, workspace: str) -> Path:
    """Reuse lock paths without retaining opened files or unbounded workspace history."""
    identity = hashlib.sha256(workspace.encode()).hexdigest()
    return home / ".autoskill" / "maintenance" / (identity + ".lock")


@contextlib.contextmanager
def _shared(stream, *, exclusive: bool = False):
    if os.name == "nt":
        import ctypes
        import msvcrt

        from .win32 import Overlapped, kernel

        handle = msvcrt.get_osfhandle(stream.fileno())
        overlapped = Overlapped()
        if not kernel.LockFileEx(handle, 3 if exclusive else 1, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.WinError(ctypes.get_last_error())
            if error.winerror == 33:
                raise ValueError("数据目录正在使用或迁移，当前操作尚未写入；请待活动任务或数据传输完成后重试") from error
            raise error
        try:
            yield
        finally:
            kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
    else:
        import fcntl

        try:
            fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise ValueError("数据目录正在使用或迁移，当前操作尚未写入；请待活动任务或数据传输完成后重试") from error
            raise
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextlib.contextmanager
def access(workspace: Path):
    file = gate_path(workspace)
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    if file in held:
        yield
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+b") as stream, _shared(stream):
        held.add(file)
        try:
            yield
        finally:
            held.remove(file)


def check(workspace: Path) -> None:
    with access(workspace):
        pass


@contextlib.contextmanager
def exclusive(workspace: Path):
    """Fail before copying if a cooperating writer still owns shared access."""
    file = gate_path(workspace)
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    exclusive_paths = getattr(_held, "exclusive", None)
    if exclusive_paths is None:
        exclusive_paths = _held.exclusive = set()
    if file in exclusive_paths:
        yield
        return
    if file in held:
        raise ValueError("当前操作仍在访问数据目录，不能同时进行整目录传输")
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+b") as stream, _shared(stream, exclusive=True):
        held.add(file)
        exclusive_paths.add(file)
        try:
            yield
        finally:
            exclusive_paths.remove(file)
            held.remove(file)
