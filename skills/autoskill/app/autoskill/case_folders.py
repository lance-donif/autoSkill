"""Versioned project folders organize cases without changing executable definitions."""
from __future__ import annotations

import hashlib
import unicodedata
import uuid

from . import domain, projects
from .locking import project_lock

DOCUMENT = "case-folders.json"


class FolderConflictError(ValueError):
    """A folder operation was based on a stale project organization or case definition."""

    code = "folder_conflict"

    def __init__(self, expected: str | None, actual: str):
        self.details = {"expected_revision": expected, "actual_revision": actual}
        super().__init__("文件夹或用例已被其他任务修改，请刷新后重试")


def _name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写文件夹名称")
    result = unicodedata.normalize("NFC", value.strip())
    if any(unicodedata.category(char) == "Cc" for char in result):
        raise ValueError("文件夹名称不能包含控制字符")
    return result


def _reference(value, label: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(label + "必须是文件夹标识或 null")
    return value


def _check_tree(folders: list[dict]) -> dict:
    nodes = {}
    siblings = set()
    for folder in folders:
        if not isinstance(folder, dict) or set(folder) != {"id", "name", "parent_id"}:
            raise ValueError("文件夹定义需要 id、name 和 parent_id")
        identity = _reference(folder["id"], "文件夹标识")
        if identity is None or identity in nodes:
            raise ValueError("文件夹标识为空或重复")
        if not isinstance(folder["name"], str) or not folder["name"]:
            raise ValueError("文件夹名称不能为空")
        parent = _reference(folder["parent_id"], "父文件夹")
        sibling = (parent, folder["name"])
        if sibling in siblings:
            raise ValueError("同一文件夹下已存在同名文件夹：" + folder["name"])
        siblings.add(sibling)
        nodes[identity] = folder
    for folder in folders:
        visited = {folder["id"]}
        parent = folder["parent_id"]
        while parent is not None:
            if parent not in nodes:
                raise ValueError("父文件夹不存在")
            if parent in visited:
                raise ValueError("不能将文件夹移入自身或其子文件夹")
            visited.add(parent)
            parent = nodes[parent]["parent_id"]
    return nodes


def _document(project_id: str, head: str | None, files: dict) -> dict:
    value = domain.read_document(project_id, DOCUMENT, head=head) if DOCUMENT in files else {"schema": 1, "folders": [], "assignments": {}}
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("文件夹数据格式无效")
    folders, assignments = value.get("folders"), value.get("assignments")
    if not isinstance(folders, list) or not isinstance(assignments, dict):
        raise ValueError("文件夹列表和用例归属格式无效")
    nodes = _check_tree(folders)
    for case_id, folder_id in assignments.items():
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("文件夹归属中的用例标识无效")
        if _reference(folder_id, "用例文件夹") is not None and folder_id not in nodes:
            raise ValueError("用例归属的文件夹不存在")
    return value


def _legacy_folder(name: str, folders: list[dict], nodes: dict) -> str:
    identity = "legacy-" + hashlib.sha256(name.encode("utf-8")).hexdigest()
    if identity in nodes:
        return identity
    existing = next((folder["id"] for folder in folders if folder["parent_id"] is None and folder["name"] == name), None)
    if existing:
        return existing
    folder = {"id": identity, "name": name, "parent_id": None}
    folders.append(folder)
    nodes[identity] = folder
    return identity


def _snapshot(project_id: str) -> tuple[dict, str | None]:
    projects.path(project_id)
    manifest = domain.manifest(project_id)
    document = _document(project_id, manifest["head"], manifest["files"])
    active = projects.cases(project_id)
    folders = [dict(folder) for folder in document["folders"]]
    nodes = {folder["id"]: folder for folder in folders}
    assignments = {}
    for case in active:
        if case["id"] in document["assignments"]:
            assignments[case["id"]] = document["assignments"][case["id"]]
        else:
            module = case.get("module") or ""
            assignments[case["id"]] = _legacy_folder(module, folders, nodes) if module else None
    content = {"folders": folders, "assignments": assignments}
    revision = domain.content_hash({**content, "cases": {case["id"]: domain.content_hash(case) for case in active}})
    return {"project_id": project_id, "revision": revision, **content}, manifest["head"]


def list_folders(project_id: str) -> dict:
    """Read all folders and active case assignments, preserving each legacy module as one label."""
    with project_lock(project_id):
        return _snapshot(project_id)[0]


def _current(project_id: str, expected_revision: str | None) -> tuple[dict, str | None]:
    current, head = _snapshot(project_id)
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError("请先读取文件夹列表，并提供 expected_revision")
    if expected_revision != current["revision"]:
        raise FolderConflictError(expected_revision, current["revision"])
    return current, head


def _publish(project_id: str, current: dict, head: str | None, reason: str) -> dict:
    _check_tree(current["folders"])
    document = {"schema": 1, "folders": current["folders"], "assignments": current["assignments"]}
    domain.commit(project_id, {DOCUMENT: document}, head, reason=reason)
    return _snapshot(project_id)[0]


def create(project_id: str, name: str, *, parent_id: str | None = None, expected_revision: str | None = None) -> dict:
    """Create a persistent folder, including an empty folder beneath another folder."""
    label, parent = _name(name), _reference(parent_id, "父文件夹")
    with project_lock(project_id):
        current, head = _current(project_id, expected_revision)
        if parent is not None and parent not in {folder["id"] for folder in current["folders"]}:
            raise ValueError("父文件夹不存在")
        current["folders"].append({"id": "folder-" + uuid.uuid4().hex, "name": label, "parent_id": parent})
        return _publish(project_id, current, head, "创建用例文件夹")


def update(project_id: str, folder_id: str, changes: dict, *, expected_revision: str | None = None) -> dict:
    """Rename or move a folder using identities rather than interpreting its label as a path."""
    if not isinstance(changes, dict) or not changes or set(changes) - {"name", "parent_id"}:
        raise ValueError("文件夹更新需要 name 或 parent_id")
    with project_lock(project_id):
        current, head = _current(project_id, expected_revision)
        folder = next((item for item in current["folders"] if item["id"] == folder_id), None)
        if folder is None:
            raise ValueError("文件夹不存在")
        if "name" in changes:
            folder["name"] = _name(changes["name"])
        if "parent_id" in changes:
            folder["parent_id"] = _reference(changes["parent_id"], "父文件夹")
        return _publish(project_id, current, head, "更新用例文件夹")


def move_cases(project_id: str, case_ids: list[str], folder_id: str | None, *, expected_revision: str | None = None) -> dict:
    """Move an entire selected batch atomically without rewriting cases or their revisions."""
    target = _reference(folder_id, "目标文件夹")
    if not isinstance(case_ids, list) or not case_ids or any(not isinstance(identity, str) or not identity for identity in case_ids):
        raise ValueError("case_ids 必须是非空的用例标识数组")
    with project_lock(project_id):
        current, head = _current(project_id, expected_revision)
        if target is not None and target not in {folder["id"] for folder in current["folders"]}:
            raise ValueError("目标文件夹不存在")
        missing = [identity for identity in case_ids if identity not in current["assignments"]]
        if missing:
            raise ValueError("这些用例不存在或已归档：" + "、".join(missing))
        current["assignments"].update(dict.fromkeys(case_ids, target))
        return _publish(project_id, current, head, "移动用例到文件夹")


def selection(project_id: str, folder_id: str | None = None, *, recursive: bool = True, expected_revision: str | None = None) -> dict:
    """Select only actual folder members; dependency cases outside the subtree stay outside exports."""
    with project_lock(project_id):
        current = _current(project_id, expected_revision)[0] if expected_revision is not None else _snapshot(project_id)[0]
        target = None if folder_id in (None, "root") else _reference(folder_id, "文件夹")
        nodes = {folder["id"]: folder for folder in current["folders"]}
        if target is not None and target not in nodes:
            raise ValueError("文件夹不存在")
        chosen = {target}
        if recursive:
            pending = [target]
            while pending:
                parent = pending.pop()
                children = [folder["id"] for folder in current["folders"] if folder["parent_id"] == parent]
                chosen.update(children)
                pending.extend(children)
        identities = [identity for identity, parent in current["assignments"].items() if parent in chosen]
        return {**current, "folder_id": target, "folder_name": nodes[target]["name"] if target else "项目根目录", "case_ids": identities, "recursive": recursive}
