"""Shared workspace access fences upgrades without serializing ordinary callers."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import threading
from pathlib import Path

_held = threading.local()


def gate_path(workspace: Path) -> Path:
    identity = hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()
    return Path.home() / ".autoskill" / "maintenance" / (identity + ".lock")


@contextlib.contextmanager
def _shared(stream):
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.LockFileEx.restype = wintypes.BOOL
        kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.UnlockFileEx.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        overlapped = Overlapped()
        if not kernel.LockFileEx(handle, 1, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.WinError(ctypes.get_last_error())
            if error.winerror == 33:
                raise ValueError("工作区正在升级或恢复，当前操作尚未写入；请先查询原升级记录，完成后重试") from error
            raise error
        try:
            yield
        finally:
            kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
    else:
        import fcntl

        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise ValueError("工作区正在升级或恢复，当前操作尚未写入；请先查询原升级记录，完成后重试") from error
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
