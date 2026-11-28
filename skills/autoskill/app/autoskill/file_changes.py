"""File identities used to invalidate evidence that was actually read and hashed."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def _ntfs_change_time(file: Path) -> int | None:
    """Query the NTFS change time without reserving the file against writes or rename."""
    import ctypes

    try:
        from .win32 import FileBasicInfo, kernel
    except (AttributeError, OSError):
        return None
    # FILE_READ_ATTRIBUTES, share read/write/delete, OPEN_EXISTING.
    handle = kernel.CreateFileW(str(file), 0x80, 7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        filesystem = ctypes.create_unicode_buffer(32)
        if not kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, filesystem, len(filesystem)) or filesystem.value.upper() != "NTFS":
            return None
        info = FileBasicInfo()
        if kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)) and info.change > 0:
            return info.change
        return None
    finally:
        kernel.CloseHandle(handle)


def file_stamp(file: Path) -> tuple:
    try:
        info = file.stat()
        change = None
        if info.st_ino and stat.S_ISREG(info.st_mode):
            # Coarse POSIX timestamps can alias multiple in-place writes from one request.
            if os.name == "posix" and info.st_ctime_ns % 1_000:
                change = info.st_ctime_ns
            elif os.name == "nt":
                # Python 3.12 st_ctime on Windows describes creation, not a content change.
                change = _ntfs_change_time(file)
        return str(file), info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino, change
    except OSError:
        return str(file), None


def reusable(stamp: tuple) -> bool:
    return len(stamp) == 6 and stamp[-1] is not None
