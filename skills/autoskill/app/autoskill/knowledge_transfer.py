"""Portable knowledge claims with atomic, idempotent local lineage publication."""
from __future__ import annotations

import copy
import uuid

from . import db, domain, knowledge_entries
from .knowledge_entries import KnowledgeError
from .locking import project_lock

SCHEMA_VERSION = 1
_ENTRY_PREFIX = "knowledge/entries/"
_IMPORT_PREFIX = "knowledge/imports/"


def _error(code: str, message: str, **details):
    raise KnowledgeError(code, message, **details)


def _json_hash(value, message: str) -> str:
    try:
        return domain.content_hash(value)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise KnowledgeError("invalid_package", message) from error


def _entry_id(value) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as error:
        raise KnowledgeError("invalid_package", "知识条目标识必须是规范 UUID") from error
    return value


def _revision(value) -> int:
    if type(value) is not int or value < 1:
        _error("invalid_package", "知识版本必须是正整数")
    return value


def _selectors(entries, all_items: bool) -> list[dict] | None:
    if type(all_items) is not bool or (all_items and entries is not None):
        _error("invalid_selector", "请选择明确的知识版本，或导出全部当前有效知识")
    if all_items:
        return None
    if not isinstance(entries, list) or not entries:
        _error("invalid_selector", "请提供非空的知识版本列表，或选择全部导出")
    selected, seen = [], set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"id", "revision"}:
            _error("invalid_selector", "知识版本必须包含 id 和 revision")
        try:
            identity, revision = _entry_id(item["id"]), _revision(item["revision"])
        except KnowledgeError as error:
            raise KnowledgeError("invalid_selector", str(error)) from error
        if identity in seen:
            _error("invalid_selector", "同一知识条目不能重复导出", entry_id=identity)
        seen.add(identity)
        selected.append({"id": identity, "revision": revision})
    return selected


def _content(entry: dict) -> dict:
    return {key: copy.deepcopy(value) for key, value in entry.items() if key != "content_hash"}


def export_package(project_id: str, *, entries: list[dict] | None = None, all_items: bool = False) -> dict:
    """Export selected complete revision chains from one frozen domain generation."""
    knowledge_entries.check_project(project_id)
    selected = _selectors(entries, all_items)
    with project_lock(project_id):
        current = domain.read(project_id)
        if selected is None:
            selected = []
            for path in sorted(current["documents"]):
                if path.startswith(_ENTRY_PREFIX) and path.endswith("/current.json"):
                    pointer = current["documents"][path]
                    entry = knowledge_entries.read_entry(project_id, pointer["id"], current=current)
                    if entry["lifecycle"] == "active":
                        selected.append({"id": entry["id"], "revision": entry["revision"]})
        exported = []
        for item in selected:
            chain = []
            for revision in range(1, item["revision"] + 1):
                entry = knowledge_entries.read_entry(project_id, item["id"], revision=revision, current=current)
                content = _content(entry)
                chain.append({"revision": revision, "parent_content_hash": content["parent_content_hash"],
                              "content": content, "content_hash": domain.content_hash(content)})
            projected = knowledge_entries.get(project_id, item["id"], revision=item["revision"], current=current)
            claimed = {name: copy.deepcopy(projected[name]) for name in ("verification", "evidence_status", "applicability")
                       if name in projected}
            exported.append({"entry_id": item["id"], "selected_revision": item["revision"],
                             "source_chain": chain, "source_verification": claimed})
        package = {"schema_version": SCHEMA_VERSION, "exporter_project_id": project_id, "entries": exported}
        package["package_hash"] = domain.content_hash(package)
        _validate_package(package)
        return package


def _validate_chain(identity: str, selected: int, chain: list) -> dict:
    if not isinstance(chain, list) or len(chain) != selected:
        _error("invalid_package", "知识包必须包含从第一版到所选版本的完整历史", entry_id=identity)
    parent, body = None, None
    for revision, node in enumerate(chain, 1):
        if not isinstance(node, dict) or set(node) != {"revision", "parent_content_hash", "content", "content_hash"}:
            _error("invalid_package", "来源版本链节点格式无效", entry_id=identity)
        content = node["content"]
        if (type(node["revision"]) is not int or node["revision"] != revision or node["parent_content_hash"] != parent
                or not isinstance(content, dict) or "content_hash" in content
                or content.get("id") != identity or type(content.get("revision")) is not int or content["revision"] != revision
                or "parent_content_hash" not in content or content["parent_content_hash"] != parent
                or type(content.get("schema_version")) is not int or content["schema_version"] != SCHEMA_VERSION
                or content.get("lifecycle") not in ("active", "withdrawn")):
            _error("invalid_package", "来源版本链不连续或与条目内容不一致", entry_id=identity, revision=revision)
        digest = _json_hash(content, "来源知识内容必须是有效 JSON")
        if node["content_hash"] != digest:
            _error("invalid_package", "来源知识版本内容哈希不匹配", entry_id=identity, revision=revision)
        try:
            body = knowledge_entries.validate_entry_content(content)
        except (ValueError, TypeError, KeyError) as error:
            raise KnowledgeError("invalid_package", "来源知识内容格式无效", entry_id=identity, revision=revision) from error
        body["lifecycle"] = content["lifecycle"]
        parent = digest
    return body


def _validate_package(package: dict) -> list[dict]:
    """Verify all claims before planning any target-side mutation."""
    expected = {"schema_version", "exporter_project_id", "entries", "package_hash"}
    if not isinstance(package, dict) or set(package) != expected:
        _error("invalid_package", "知识包必须是完整的单一 JSON 对象")
    if type(package["schema_version"]) is not int or package["schema_version"] != SCHEMA_VERSION:
        _error("invalid_package", "不支持的知识包版本")
    if not isinstance(package["exporter_project_id"], str) or not package["exporter_project_id"].strip():
        _error("invalid_package", "知识包缺少来源项目")
    digest = _json_hash({key: value for key, value in package.items() if key != "package_hash"}, "知识包必须是有效 JSON")
    if package["package_hash"] != digest:
        _error("invalid_package", "知识包内容哈希不匹配")
    if not isinstance(package["entries"], list):
        _error("invalid_package", "知识包 entries 必须是列表")
    validated, seen = [], set()
    for item in package["entries"]:
        if not isinstance(item, dict) or set(item) != {"entry_id", "selected_revision", "source_chain", "source_verification"}:
            _error("invalid_package", "知识包条目字段不完整或不受支持")
        identity, selected = _entry_id(item["entry_id"]), _revision(item["selected_revision"])
        if identity in seen:
            _error("invalid_package", "知识包存在重复条目", entry_id=identity)
        seen.add(identity)
        if not isinstance(item["source_verification"], dict):
            _error("invalid_package", "来源验证声明必须是对象")
        body = _validate_chain(identity, selected, item["source_chain"])
        validated.append({"item": item, "body": body})
    return validated


def _request_path(key: str) -> str:
    return f"{_IMPORT_PREFIX}requests/{domain.content_hash(key)}.json"


def _origin_paths(project_id: str, entry_id: str) -> tuple[str, str]:
    key = domain.content_hash([project_id, entry_id])
    return f"{_IMPORT_PREFIX}origins/{key}.json", f"{_IMPORT_PREFIX}origins/{key}/revisions/"


def _lineage_path(origin_project: str, origin_entry: str, lineage_id: str | None) -> str:
    return f"{_IMPORT_PREFIX}lineages/{domain.content_hash([origin_project, origin_entry, lineage_id])}.json"


def _historical_receipt(project_id: str, current: dict, path: str, record: dict) -> dict:
    """Recover the receipt's publication head without a self-referential hash."""
    reference = current["files"][path]
    generation = domain.manifest(project_id, current["head"])
    while generation["parent"] is not None:
        parent = domain.manifest(project_id, generation["parent"])
        if parent["files"].get(path) != reference:
            break
        generation = parent
    return {"domain_head": generation["head"], **copy.deepcopy(record["receipt"])}


def _source_changes(documents: dict, changes: dict, origin_project: str, item: dict) -> None:
    origin_path, revision_prefix = _origin_paths(origin_project, item["entry_id"])
    original = documents.get(origin_path)
    origin = copy.deepcopy(original) if original is not None else {
        "schema_version": SCHEMA_VERSION, "origin_project_id": origin_project,
        "origin_entry_id": item["entry_id"], "revisions": {},
    }
    for node in item["source_chain"]:
        revision = str(node["revision"])
        previous = origin["revisions"].get(revision)
        if previous is not None and previous != node["content_hash"]:
            _error("source_revision_conflict", "同一来源版本的内容发生冲突", origin_project_id=origin_project,
                   origin_entry_id=item["entry_id"], origin_revision=node["revision"])
        if previous is None:
            origin["revisions"][revision] = node["content_hash"]
            changes[f"{revision_prefix}{revision}.json"] = copy.deepcopy(node)
    if origin != original:
        changes[origin_path] = origin


def _import_entry(project_id: str, current: dict, changes: dict, package: dict, validated: dict,
                  lineage_id: str | None, imported_at: str) -> tuple[str, dict]:
    item, body = validated["item"], validated["body"]
    origin_project, origin_entry = package["exporter_project_id"], item["entry_id"]
    source = item["source_chain"][-1]
    selected, source_hash = item["selected_revision"], source["content_hash"]
    path = _lineage_path(origin_project, origin_entry, lineage_id)
    original = current["documents"].get(path)
    lineage = copy.deepcopy(original)
    previous_entry = None
    if lineage is not None:
        previous_entry = knowledge_entries.read_entry(project_id, lineage["entry_id"], current=current)
        if str(selected) in lineage["imported_revisions"]:
            return "unchanged", {"entry_id": previous_entry["id"], "revision": previous_entry["revision"]}
        if (selected <= lineage["latest_origin_revision"] or previous_entry["revision"] != lineage["latest_local_revision"]
                or previous_entry.get("creation_mode") != "import"
                or previous_entry["content_hash"] != lineage["local_revisions"][str(lineage["latest_local_revision"])]["content_hash"]):
            _error("revision_conflict", "目标知识已有本地修订，或来源版本不是更新版本；请使用新谱系导入", entry_id=previous_entry["id"],
                   revision=previous_entry["revision"], origin_revision=selected)
        identity, revision = previous_entry["id"], previous_entry["revision"] + 1
    else:
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "autoskill:knowledge-import:" +
                                 domain.content_hash([project_id, origin_project, origin_entry, lineage_id])))
        if knowledge_entries.pointer_path(identity) in current["documents"]:
            _error("revision_conflict", "目标知识标识已存在，不能覆盖", entry_id=identity)
        revision = 1
        lineage = {"schema_version": SCHEMA_VERSION, "origin_project_id": origin_project, "origin_entry_id": origin_entry,
                   "lineage_id": lineage_id, "entry_id": identity, "imported_revisions": {}, "local_revisions": {}}
    provenance = {"origin_project_id": origin_project, "origin_entry_id": origin_entry, "origin_revision": selected,
                  "origin_content_hash": source_hash, "lineage_id": lineage_id,
                  "source_verification": copy.deepcopy(item["source_verification"])}
    if "provenance" in source["content"]:
        provenance["source_provenance"] = copy.deepcopy(source["content"]["provenance"])
    entry = {**copy.deepcopy(body), "schema_version": SCHEMA_VERSION, "id": identity, "revision": revision,
             "parent_content_hash": previous_entry["content_hash"] if previous_entry is not None else None,
             "creation_mode": "import", "imported_at": imported_at, "created_at": imported_at, "provenance": provenance}
    entry["content_hash"] = knowledge_entries.entry_content_hash(entry)
    changes.update(knowledge_entries.make_changes(entry))
    lineage["latest_origin_revision"], lineage["latest_local_revision"] = selected, revision
    lineage["imported_revisions"][str(selected)] = revision
    lineage["local_revisions"][str(revision)] = {"origin_revision": selected, "origin_content_hash": source_hash, "content_hash": entry["content_hash"]}
    changes[path] = lineage
    return "imported", {"origin_project_id": origin_project, "origin_entry_id": origin_entry, "origin_revision": selected,
                        "entry_id": identity, "revision": revision, "evidence_status": "imported_unverified"}


def import_package(project_id: str, package: dict, *, idempotency_key: str, as_new: bool = False) -> dict:
    """Validate, map and publish one whole package with a durable historical receipt."""
    knowledge_entries.check_project(project_id)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        _error("invalid_package", "导入必须提供非空幂等键")
    if type(as_new) is not bool:
        _error("invalid_package", "as_new 必须是布尔值")
    if not isinstance(package, dict):
        _error("invalid_package", "知识包必须是 JSON 对象")
    request_hash = _json_hash({"project_id": project_id, "package": package, "as_new": as_new}, "知识包必须是有效 JSON")
    path = _request_path(idempotency_key)
    with project_lock(project_id):
        current = domain.read(project_id)
        recorded = current["documents"].get(path)
        if recorded is not None:
            if recorded["request_hash"] != request_hash:
                _error("idempotency_conflict", "该幂等键已用于不同的导入请求")
            return _historical_receipt(project_id, current, path, recorded)
        validated = _validate_package(package)
        changes, receipt = {}, {"imported": [], "unchanged": []}
        imported_at = db.now()
        for value in validated:
            _source_changes(current["documents"], changes, package["exporter_project_id"], value["item"])
            result, item = _import_entry(project_id, current, changes, package, value,
                                         idempotency_key if as_new else None, imported_at)
            receipt[result].append(item)
        changes[path] = {"schema_version": SCHEMA_VERSION, "request_hash": request_hash, "receipt": receipt}
        staged = domain.stage(project_id, changes, current["head"], "导入结构化知识", {"knowledge_import_request": path})
        try:
            published = domain.publish(project_id, staged["stage_id"])
        except domain.ConflictError as error:
            raise KnowledgeError("revision_conflict", "知识导入期间项目版本发生变化，请重新读取后重试",
                                 expected_head=error.expected_head, actual_head=error.actual_head) from error
        return {"domain_head": published["head"], **receipt}
