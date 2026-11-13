"""Root-relative data references with explicit, read-only relocation of historical absolute paths."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import paths


class UnmappedPathError(ValueError):
    """An external absolute path has no explicit data-root relocation mapping."""


def _text(value) -> str:
    try:
        value = os.fspath(value)
    except TypeError as error:
        raise ValueError("数据引用必须是文件路径") from error
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("数据引用必须是非空文件路径")
    return value


def _absolute(value: str):
    windows = PureWindowsPath(value)
    if windows.drive or value.startswith("\\"):
        if not windows.is_absolute():
            raise ValueError("数据引用不能使用依赖当前盘符的 Windows 路径")
        return windows
    posix = PurePosixPath(value)
    return posix if posix.is_absolute() else None


def is_absolute(value) -> bool:
    """Recognize historical Windows drives and UNC paths even on a POSIX reader."""
    return _absolute(_text(value)) is not None


def _inside(root: Path, relative) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("数据引用不能离开当前数据根")
    candidate = root.joinpath(*relative.parts)
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("数据引用指向数据根以外的链接目标")
    return candidate


def _manifest(root: Path) -> dict:
    file = root / "data-root.json"
    if not file.is_file():
        return {}
    value = json.loads(file.read_bytes())
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("数据根标识格式无效，无法解析历史路径")
    return value


def _mappings(manifest: dict, root: Path) -> list:
    mappings = manifest.get("path_mappings", [])
    if not isinstance(mappings, list):
        raise ValueError("数据根路径映射格式无效")
    selected = []
    for item in mappings:
        if not isinstance(item, dict):
            raise ValueError("数据根路径映射格式无效")
        source = _absolute(_text(item.get("source")))
        target = _text(item.get("destination"))
        if source is None or _absolute(target) is not None:
            raise ValueError("数据根路径映射需要绝对来源与相对目标")
        destination = PurePosixPath(target)
        _inside(root, destination)
        selected.append((source, destination))
    return sorted(selected, key=lambda item: len(item[0].parts), reverse=True)


def _relocated(absolute, root: Path) -> Path:
    manifest = _manifest(root)
    for source, destination in _mappings(manifest, root):
        if type(source) is not type(absolute):
            continue
        try:
            relative = absolute.relative_to(source)
        except ValueError:
            continue
        return _inside(root, destination.joinpath(*relative.parts))
    aliases = []
    for name in ("legacy_roots", "locations"):
        values = manifest.get(name, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("数据根历史位置格式无效")
        aliases.extend(value for value in (_absolute(value) for value in values) if value is not None)
    for alias in sorted(aliases, key=lambda value: len(value.parts), reverse=True):
        if type(alias) is not type(absolute):
            continue
        try:
            relative = absolute.relative_to(alias)
        except ValueError:
            continue
        return _inside(root, relative)
    raise UnmappedPathError("数据引用属于外部路径，且没有明确的旧数据根映射")


def resolve(value, root: str | Path | None = None) -> Path:
    """Resolve known data references against this copy, never a still-existing original tree."""
    text = _text(value)
    selected = Path(root) if root is not None else paths.workspace()
    current = selected.expanduser().resolve()
    absolute = _absolute(text)
    if absolute is None:
        return _inside(current, PurePosixPath(text))
    # Prefer the selected root before legacy aliases, including when it is nested under an old root.
    for candidate in (str(current), str(selected.absolute())):
        anchor = _absolute(candidate)
        if type(anchor) is not type(absolute):
            continue
        try:
            relative = absolute.relative_to(anchor)
        except ValueError:
            continue
        return _inside(current, relative)
    return _relocated(absolute, current)


def store(path, root: str | Path | None = None) -> str:
    """Store a known internal location as a POSIX path without rewriting the referenced file."""
    current = (Path(root) if root is not None else paths.workspace()).expanduser().resolve()
    return resolve(path, root=current).relative_to(current).as_posix()
