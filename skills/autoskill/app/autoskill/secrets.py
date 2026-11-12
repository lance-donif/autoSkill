"""Machine-local credentials, kept as plain text in workspace/.local/credentials.json so they can be read and edited.

Cases refer to a credential by name: ${ENV:NAME}, which keeps the value out of cases and share packs.
A process environment variable of that name is the fallback. `seal`/`unseal` remain only to read
what older versions stored encrypted.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
from pathlib import Path

from . import paths
from .core import store
from .locking import named_lock

REF = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        from cryptography.fernet import Fernet, InvalidToken

        key_file = paths.local() / "encryption.key"
        if not key_file.exists() and not decrypt:
            try:
                fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as file:
                    file.write(Fernet.generate_key())
        try:
            cipher = Fernet(key_file.read_bytes())
            return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
        except (InvalidToken, FileNotFoundError) as error:
            raise ValueError("本机密钥库无法读取：缺少 Linux 本地密钥、密文损坏或来自其他平台；请重新设置凭据") from error
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    call = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ValueError("本机密钥库无法读取：不是保存它的 Windows 用户，或文件已损坏")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def seal(text: str) -> str:
    return base64.b64encode(_protect(text.encode("utf-8"))).decode("ascii")


def unseal(blob: str) -> str:
    return _protect(base64.b64decode(blob), decrypt=True).decode("utf-8")


def _file() -> Path:
    return paths.local() / "credentials.json"


def _write(data: dict) -> None:
    store.write(_file(), data)


def _load() -> dict:
    with named_lock("credentials", "store"):
        file = _file()
        if file.is_file():
            return json.loads(file.read_text(encoding="utf-8"))
        data, legacy = {}, paths.local() / "secrets.json"
        if legacy.is_file():  # an older version sealed each value: carry over what this user can still read, once
            for name, blob in json.loads(legacy.read_text(encoding="utf-8")).items():
                # A copied store may contain entries sealed by another user or machine.
                with contextlib.suppress(ValueError, OSError):
                    data[name] = unseal(blob)
            _write(data)
        return data


def set_secret(name: str, value: str) -> None:
    if not NAME.fullmatch(name):
        raise ValueError("凭据名称只能使用字母、数字和下划线，且不能以数字开头")
    with named_lock("credentials", "store"):
        _write({**_load(), name: value})


def delete_secret(name: str) -> None:
    with named_lock("credentials", "store"):
        data = _load()
        if data.pop(name, None) is not None:
            _write(data)


def names() -> list[str]:
    return sorted(_load())


def get_secret(name: str) -> str | None:
    value = _load().get(name)
    return value if value is not None else os.environ.get(name)


def references(value) -> set[str]:
    """Every ${ENV:NAME} mentioned anywhere inside a JSON-like value."""
    if isinstance(value, str):
        return set(REF.findall(value))
    if isinstance(value, dict):
        return set().union(*[references(v) for v in value.values()]) if value else set()
    if isinstance(value, list):
        return set().union(*[references(v) for v in value]) if value else set()
    return set()


def resolve(value):
    """Replace ${ENV:NAME} references with their values; raise naming what is missing."""
    if isinstance(value, str):
        def substitute(match):
            found = get_secret(match[1])
            if found is None:
                raise ValueError(f"缺少凭据 {match[1]}：请运行 autoskill secret set {match[1]}")
            return found
        return REF.sub(substitute, value)
    if isinstance(value, dict):
        return {k: resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v) for v in value]
    return value
