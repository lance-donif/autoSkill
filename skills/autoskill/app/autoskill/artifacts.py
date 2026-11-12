"""Portable project inputs and machine-private browser evidence.

Cases contain project-relative upload names. Captures and downloads live under
``.local``; their metadata never includes the source machine's absolute paths.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

from . import paths


def relative_name(value: str) -> str:
    """Validate the same portable relative spelling on Linux and Windows."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("文件名必须是项目内的相对路径，使用 / 分隔目录")
    item = PurePosixPath(value)
    if (item.is_absolute() or PureWindowsPath(value).drive or any(p in ("", ".", "..") for p in value.split("/"))
            or "${" in value or "{{" in value or any(":" in p for p in item.parts)):
        raise ValueError("文件名必须是项目内的相对路径，不能包含盘符、.. 或变量")
    return item.as_posix()


def _contained(root: Path, name: str) -> Path:
    base = root.resolve()
    target = (base / relative_name(name)).resolve()
    if not target.is_relative_to(base):
        raise ValueError("文件路径不能通过符号链接离开指定目录")
    return target


def sha256(file: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(file).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_dir(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    return folder


def resolve_output(output_root: Path | str, filename: str) -> Path:
    """Resolve a replay output without permitting traversal or symlink escapes."""
    root = _private_dir(Path(output_root).resolve())
    target = _contained(root, filename)
    _private_dir(target.parent)
    return target


def private_root(session: str, rec: dict | None = None) -> Path:
    """Use a separate evidence directory for each recording or named session."""
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(session)).strip("-")[:64] or "browser"
    if rec is not None:
        # An explicit token is stable across the recorder's short-lived processes.
        token = rec.setdefault("artifact_id", uuid.uuid4().hex[:12])
        label += "-" + re.sub(r"[^A-Za-z0-9_-]", "", str(token))[:32]
    local = paths.local().resolve()
    root = _contained(local, "artifacts/" + label)
    return _private_dir(root)


def private_output(session: str, requested: str | None = None, *, category: str = "captures", suffix: str = "",
                   rec: dict | None = None) -> Path:
    """Allocate a private output, accepting absolute paths only within .local.

    Relative requests are names beneath this session's category, rather than
    paths relative to the caller's working directory.
    """
    root = private_root(session, rec)
    category = relative_name(category)
    folder = _contained(root, category)
    _private_dir(folder)
    if requested:
        if "\x00" in requested:
            raise ValueError("输出路径无效")
        requested_path = Path(requested).expanduser()
        if requested_path.is_absolute():
            target = requested_path.resolve()
            local = paths.local().resolve()
            if not target.is_relative_to(local):
                raise ValueError("浏览器输出必须保存在 .local 私有目录；使用相对文件名或省略路径")
        else:
            if "\\" in requested:
                raise ValueError("相对输出路径使用 / 分隔目录")
            target = _contained(folder, requested)
    else:
        target = folder / (uuid.uuid4().hex[:16] + suffix)
    if suffix and not target.suffix:
        target = target.with_suffix(suffix)
    _private_dir(target.parent)
    return target


def metadata(file: Path | str, root: Path | str | None = None, *, kind: str = "") -> dict:
    """Hash an existing artifact; persist only its name relative to its root."""
    target = Path(file).resolve()
    base = Path(root).resolve() if root is not None else paths.local().resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise ValueError("证据文件不在指定目录内，或尚未生成")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    item = {"path": target.relative_to(base).as_posix(), "sha256": sha256(target), "bytes": target.stat().st_size}
    mime = mimetypes.guess_type(target.name)[0]
    if mime:
        item["mime"] = mime
    if kind:
        item["kind"] = kind
    return item


def _safe_basename(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "upload"
    # Keep a usable extension even when a user's basename is very long.
    suffix = Path(clean).suffix[:20]
    return clean[:max(1, 100 - len(suffix))].removesuffix(suffix) + suffix


def stage_upload(project_id: str, files: Iterable[str | Path]) -> list[str]:
    """Copy user inputs into project files and return portable case operands."""
    from . import projects

    root = projects.path(project_id).resolve()
    folder = _contained(root, "files/uploads")
    folder.mkdir(parents=True, exist_ok=True)
    manifest_file = _contained(root, "files/uploads/manifest.json")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {"version": 1, "assets": {}}
    assets = manifest.setdefault("assets", {})
    names = []
    for value in files:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() and str(value).startswith("files/"):
            project_input = _contained(root, str(value))
            if project_input.is_file():
                candidate = project_input
        source = candidate.resolve()
        if not source.is_file():
            raise ValueError(f"上传文件不存在或不是普通文件：{candidate.name}")
        digest = sha256(source)
        existing = source.relative_to(root).as_posix() if source.is_relative_to(root) else ""
        name = existing if assets.get(existing, {}).get("sha256") == digest else "files/uploads/" + digest + "-" + _safe_basename(source.name)
        target = _contained(root, name)
        if source != target and (not target.is_file() or sha256(target) != digest):
            temporary = folder / ("." + uuid.uuid4().hex + ".tmp")
            try:
                shutil.copyfile(source, temporary)
                # Detect a source that changed while being copied.
                if sha256(temporary) != digest:
                    raise ValueError("上传文件在复制期间发生变化；请重试")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        assets[name] = {"sha256": digest, "bytes": target.stat().st_size, "name": _safe_basename(source.name)}
        names.append(name)
    if not names:
        raise ValueError("upload 至少需要一个文件")
    temporary = manifest_file.with_name(".manifest-" + uuid.uuid4().hex + ".json")
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, manifest_file)
    finally:
        temporary.unlink(missing_ok=True)
    return names


def resolve_upload_assets(asset_root: Path | str, files: Iterable[str]) -> list[str]:
    """Resolve staged case inputs just before calling a browser driver."""
    if asset_root is None:
        raise ValueError("upload 需要配置项目文件目录 asset_root")
    root = Path(asset_root).resolve()
    names = list(files)
    if not names:
        raise ValueError("upload 至少需要一个项目文件")
    found = []
    for name in names:
        file = _contained(root, name)
        if not file.is_file():
            raise ValueError(f"项目上传文件不存在：{name}")
        found.append(str(file))
    return found

