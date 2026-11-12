"""Machine-local secret store. Values never enter cases, logs, reports or share packs.

On Windows values are encrypted with DPAPI, so only the same Windows user on the
same machine can read them. Cases refer to a secret by name: ${ENV:NAME}. A
process environment variable of that name is the fallback.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from . import paths

REF = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":  # development fallback only; the product targets Windows
        return base64.b64decode(data) if decrypt else base64.b64encode(data)
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
    return paths.local() / "secrets.json"


def _load() -> dict:
    file = _file()
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}


def set_secret(name: str, value: str) -> None:
    if not NAME.fullmatch(name):
        raise ValueError("凭据名称只能使用字母、数字和下划线，且不能以数字开头")
    data = _load()
    data[name] = seal(value)
    _file().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def delete_secret(name: str) -> None:
    data = _load()
    if data.pop(name, None) is not None:
        _file().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def names() -> list[str]:
    return sorted(_load())


def get_secret(name: str) -> str | None:
    blob = _load().get(name)
    if blob is not None:
        try:
            return unseal(blob)
        except (ValueError, OSError):
            pass  # sealed by another Windows user or machine (a copied folder): treat as not set here
    return os.environ.get(name)


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
