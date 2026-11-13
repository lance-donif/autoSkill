"""Versioned project documents and binary assets with one atomic publication point.

The immutable generation manifests and content-addressed objects are canonical.
HEAD publishes a generation; neither SQLite indexes nor legacy project files are
part of this transaction. Staged patches live locally until a service publishes
them with the same project lock and compare-and-swap check.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path

from . import db, paths, projects
from .locking import project_lock

HASH = re.compile(r"[0-9a-f]{64}")
STAGE_ID = re.compile(r"[0-9a-f]{32}")


class ConflictError(ValueError):
    """The caller's base generation no longer matches the published generation."""

    def __init__(self, expected_head: str | None, actual_head: str | None):
        self.expected_head, self.actual_head = expected_head, actual_head
        super().__init__("项目资产已被其他任务更新，请读取最新版本后重新合并")


def canonical_bytes(value) -> bytes:
    """Use the same canonical JSON representation for files and execution hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical_bytes(value)).hexdigest()


def _logical_path(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or any(part in ("", ".", "..") or ":" in part for part in value.split("/"))):
        raise ValueError("资产路径必须是项目内的相对路径")
    return value


def _head(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or not HASH.fullmatch(value)):
        raise ValueError("资产版本标识无效")
    return value


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _immutable_write(target: Path, data: bytes) -> None:
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError(f"资产内容哈希校验失败：{target.name}")
        return
    _atomic_write(target, data)


def _ref(path: str, data: bytes, kind: str) -> dict:
    return {"path": path, "sha256": content_hash(data), "size": len(data), "kind": kind}


def _validate_ref(path: str, ref: dict) -> None:
    _logical_path(path)
    if (not isinstance(ref, dict) or ref.get("path") != path or ref.get("kind") not in ("json", "bytes")
            or type(ref.get("size")) is not int or ref["size"] < 0):
        raise ValueError(f"资产清单条目无效：{path}")
    if _head(ref.get("sha256")) is None:
        raise ValueError(f"资产清单缺少内容哈希：{path}")


def _object(root: Path, ref: dict) -> bytes:
    data = (root / "objects" / ref["sha256"]).read_bytes()
    if len(data) != ref["size"] or content_hash(data) != ref["sha256"]:
        raise ValueError(f"资产内容哈希校验失败：{ref['path']}")
    return data


def _validate_manifest(value: dict) -> None:
    if (not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("files"), dict)
            or not isinstance(value.get("reason"), str) or not isinstance(value.get("metadata"), dict)
            or not isinstance(value.get("created_at"), str) or not isinstance(value.get("changed"), list)
            or "parent" not in value):
        raise ValueError("资产版本清单格式无效")
    _head(value["parent"])
    for path in value["changed"]:
        _logical_path(path)
    for path, ref in value["files"].items():
        _validate_ref(path, ref)


def _manifest(root: Path, head: str | None = None) -> dict:
    if head is None:
        pointer = root / "HEAD.json"
        if not pointer.exists():
            return {"head": None, "files": {}, "parent": None, "reason": "", "metadata": {}, "created_at": None, "changed": []}
        value = json.loads(pointer.read_bytes())
        if not isinstance(value, dict) or "head" not in value:
            raise ValueError("资产 HEAD 格式无效")
        head = value["head"]
        if head is None:
            raise ValueError("资产 HEAD 不能是空版本")
    _head(head)
    data = (root / "generations" / head / "manifest.json").read_bytes()
    if content_hash(data) != head:
        raise ValueError("资产版本清单哈希校验失败")
    value = json.loads(data)
    _validate_manifest(value)
    return {**value, "head": head}


def manifest(project_id: str, head: str | None = None) -> dict:
    """Read a generation's small reference map without opening its documents."""
    with project_lock(project_id):
        return _manifest(projects.path(project_id) / "domain", head)


def read(project_id: str, head: str | None = None) -> dict:
    """Read JSON documents and references from one generation; binary data stays lazy."""
    with project_lock(project_id):
        root = projects.path(project_id) / "domain"
        value = _manifest(root, head)
        documents = {path: json.loads(_object(root, ref)) for path, ref in value["files"].items() if ref["kind"] == "json"}
        return {"head": value["head"], "documents": documents, "files": value["files"]}


def file_ref(project_id: str, path: str, head: str | None = None) -> dict:
    return manifest(project_id, head)["files"][_logical_path(path)]


def read_file(project_id: str, path: str, head: str | None = None) -> bytes:
    with project_lock(project_id):
        root = projects.path(project_id) / "domain"
        return _object(root, _manifest(root, head)["files"][_logical_path(path)])


def read_document(project_id: str, path: str, head: str | None = None):
    with project_lock(project_id):
        root = projects.path(project_id) / "domain"
        ref = _manifest(root, head)["files"][_logical_path(path)]
        if ref["kind"] != "json":
            raise ValueError("该资产是二进制文件，请读取文件内容")
        return json.loads(_object(root, ref))


def _prepare(changes: dict, remove: Iterable[str]) -> tuple[dict, list[str]]:
    if not isinstance(changes, dict):
        raise ValueError("资产修改必须是路径到内容的对象")
    prepared = {}
    for path, value in changes.items():
        _logical_path(path)
        binary = isinstance(value, bytes)
        data = value if binary else canonical_bytes(value)
        prepared[path] = (_ref(path, data, "bytes" if binary else "json"), data)
    deleted = sorted({_logical_path(path) for path in remove})
    if set(deleted) & set(prepared):
        raise ValueError("同一资产不能同时修改和删除")
    return prepared, deleted


def _metadata(reason: str, metadata: dict | None) -> dict:
    if not isinstance(reason, str) or (metadata is not None and not isinstance(metadata, dict)):
        raise ValueError("资产变更说明必须是文本，变更元数据必须是对象")
    return json.loads(canonical_bytes({} if metadata is None else metadata))


def _commit(root: Path, prepared: dict, deleted: list[str], expected_head: str | None,
            reason: str, metadata: dict, operation_id: str | None = None) -> dict:
    previous = _manifest(root)
    if previous["head"] != expected_head:
        raise ConflictError(expected_head, previous["head"])
    files = dict(previous["files"])
    for path in deleted:
        files.pop(path, None)
    for path, (ref, _data) in prepared.items():
        files[path] = ref
    changed = sorted(path for path in set(files) | set(previous["files"]) if files.get(path) != previous["files"].get(path))
    if not changed and operation_id is None:
        return {"head": expected_head, "previous_head": expected_head, "changed": []}
    value = {"format": 1, "parent": expected_head, "files": files, "changed": changed,
             "reason": reason, "metadata": metadata, "created_at": db.now(), "operation_id": operation_id}
    data = canonical_bytes(value)
    head = content_hash(data)
    for ref, blob in prepared.values():
        _immutable_write(root / "objects" / ref["sha256"], blob)
    _immutable_write(root / "generations" / head / "manifest.json", data)
    _atomic_write(root / "HEAD.json", canonical_bytes({"head": head}))
    return {"head": head, "previous_head": expected_head, "changed": changed}


def commit(project_id: str, changes: dict, expected_head: str | None, reason: str = "", metadata: dict | None = None, *, remove: Iterable[str] = ()) -> dict:
    """Publish all changes at once, rejecting a stale base without modifying HEAD."""
    _head(expected_head)
    prepared, deleted = _prepare(changes, remove)
    metadata = _metadata(reason, metadata)
    with project_lock(project_id):
        return _commit(projects.path(project_id) / "domain", prepared, deleted, expected_head, reason, metadata)


def attach(project_id: str, path: str, data: bytes, expected_head: str | None, reason: str = "", metadata: dict | None = None) -> dict:
    if not isinstance(data, bytes):
        raise ValueError("附件内容必须是字节")
    return commit(project_id, {path: data}, expected_head, reason, metadata)


def import_file(project_id: str, path: str, source: str | Path, expected_head: str | None, reason: str = "", metadata: dict | None = None) -> dict:
    return attach(project_id, path, Path(source).read_bytes(), expected_head, reason, metadata)


def _stage_folder(project_id: str, stage_id: str) -> Path:
    projects.path(project_id)
    if not isinstance(stage_id, str) or not STAGE_ID.fullmatch(stage_id):
        raise ValueError("暂存修改标识无效")
    return paths.local() / "domain-staging" / project_id / stage_id


def stage(project_id: str, changes: dict, expected_head: str | None, reason: str = "", metadata: dict | None = None, *, remove: Iterable[str] = ()) -> dict:
    """Persist a candidate patch without changing any published project documents."""
    _head(expected_head)
    prepared, deleted = _prepare(changes, remove)
    metadata = _metadata(reason, metadata)
    stage_id = uuid.uuid4().hex
    folder = _stage_folder(project_id, stage_id)
    value = {"format": 1, "expected_head": expected_head, "files": {path: ref for path, (ref, _) in prepared.items()},
             "remove": deleted, "reason": reason, "metadata": metadata}
    data = canonical_bytes(value)
    for ref, blob in prepared.values():
        _immutable_write(folder / "objects" / ref["sha256"], blob)
    _atomic_write(folder / "patch.json", data)
    return {"stage_id": stage_id, "expected_head": expected_head}


def _published(root: Path, operation_id: str) -> dict | None:
    value = _manifest(root)
    while value["head"] is not None:
        if value.get("operation_id") == operation_id:
            return {"head": value["head"], "previous_head": value["parent"], "changed": value["changed"]}
        if value["parent"] is None:
            break
        value = _manifest(root, value["parent"])
    return None


def publish(project_id: str, stage_id: str) -> dict:
    """Publish a durable patch with CAS; retrying a published patch is idempotent."""
    with project_lock(project_id):
        folder = _stage_folder(project_id, stage_id)
        root = projects.path(project_id) / "domain"
        patch = json.loads((folder / "patch.json").read_bytes())
        if not isinstance(patch, dict) or patch.get("format") != 1:
            raise ValueError("暂存修改格式无效")
        operation_id = f"stage:{stage_id}"
        prior = _published(root, operation_id)
        if prior is not None:
            return prior
        prepared = {}
        for path, ref in patch["files"].items():
            _validate_ref(path, ref)
            prepared[path] = (ref, _object(folder, ref))
        deleted = [_logical_path(path) for path in patch["remove"]]
        metadata = _metadata(patch["reason"], patch["metadata"])
        if set(deleted) & set(prepared):
            raise ValueError("同一资产不能同时修改和删除")
        return _commit(root, prepared, deleted, _head(patch["expected_head"]), patch["reason"], metadata, operation_id)


def validate_project(folder: Path) -> None:
    """Verify every portable generation and object before installing an import."""
    root = folder / "domain"
    if not root.exists():
        return
    current = _manifest(root)
    generations = {item.name for item in (root / "generations").iterdir()} if (root / "generations").exists() else set()
    for head in generations:
        value = _manifest(root, head)
        if value["parent"] is not None and value["parent"] not in generations:
            raise ValueError("资产历史缺少父版本")
        for ref in value["files"].values():
            data = _object(root, ref)
            if ref["kind"] == "json":
                json.loads(data)
    if current["head"] is not None and current["head"] not in generations:
        raise ValueError("资产 HEAD 指向不存在的版本")
