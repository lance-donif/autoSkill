"""Machine, data-location and process-birth identities fence copied runtime handles."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import uuid
from functools import lru_cache
from pathlib import Path

from . import paths


@lru_cache(maxsize=1)
def machine_id() -> str:
    value = ""
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                value = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            value = ""
    else:
        for file in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = file.read_text(encoding="ascii").strip()
            except OSError:
                continue
            if value:
                break
    return hashlib.sha256((value or f"{platform.node()}:{uuid.getnode()}").encode()).hexdigest()


def location_id(root: Path | None = None) -> str:
    return hashlib.sha256(os.path.normcase(str((root or paths.workspace()).resolve())).encode()).hexdigest()


def _windows_birth(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return "windows:" + str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def process_birth(pid: int) -> str | None:
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        return _windows_birth(pid)
    if sys.platform.startswith("linux"):
        try:
            values = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if values[0] in ("Z", "X"):
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return "linux:" + boot + ":" + values[19]
        except (OSError, IndexError):
            return None
    return None


def current(pid: int | None = None, root: Path | None = None) -> dict:
    selected = os.getpid() if pid is None else pid
    return {"machine_id": machine_id(), "location_id": location_id(root), "pid": selected, "process_started_at": process_birth(selected)}


def decode(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def is_local(owner, root: Path | None = None) -> bool:
    value = decode(owner)
    return value.get("machine_id") == machine_id() and value.get("location_id") == location_id(root)


def note_new_database(root: Path) -> None:
    """A newly created local database is already attached; later startup must not interrupt its work."""
    from .data_transfer import write_json

    marker = root / ".local/runtime-location.json"
    if not marker.exists():
        write_json(marker, {"machine_id": machine_id(), "location_id": location_id(root)})


def owns(pid: int | None, owner, root: Path | None = None) -> bool:
    value = decode(owner)
    if not is_local(value, root) or value.get("pid") != pid or not value.get("process_started_at"):
        return False
    return process_birth(pid) == value["process_started_at"]


def legacy_matches(pid: int | None, root: Path) -> bool:
    """Recognize a cooperating legacy Linux writer without trusting a copied numeric PID."""
    if not process_birth(pid) or not sys.platform.startswith("linux"):
        return False
    try:
        process = Path(f"/proc/{pid}")
        command = process.joinpath("cmdline").read_bytes().split(b"\0")
        if not any(b"autoskill" in part for part in command):
            return False
        environment = process.joinpath("environ").read_bytes().split(b"\0")
        workspace = next((entry.split(b"=", 1)[1] for entry in environment if entry.startswith(b"AUTOSKILL_WORKSPACE=")), b"")
        return bool(workspace) and Path(os.fsdecode(workspace)).resolve() == root.resolve()
    except OSError:
        return False
