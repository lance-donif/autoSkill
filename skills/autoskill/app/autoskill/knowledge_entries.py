"""Immutable project knowledge, local evidence qualification and snapshot search."""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from . import data_refs, db, domain, execution_inputs, paging, paths, projects
from .locking import project_lock

SETTINGS_PATH = "knowledge/settings.json"
KINDS = {"observation", "procedure", "root_cause_hypothesis"}
NAMESPACE = uuid.UUID("d063eff7-61c1-5138-9ab4-78eb78c0d829")
ENTRY_FIELDS = {"title", "text", "kind", "scope", "evidence_refs", "validation_refs", "supersedes"}


class KnowledgeError(ValueError):
    """A stable service error shared by workflow, CLI and HTTP adapters."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


def check_project(project_id: str) -> None:
    if not isinstance(project_id, str) or not projects.ID.fullmatch(project_id):
        raise KnowledgeError("validation_error", "项目标识只能使用小写字母、数字和短横线")
    try:
        projects.path(project_id)
    except ValueError as error:
        raise KnowledgeError("not_found", "知识项目不存在", project_id=project_id) from error


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError("validation_error", f"{name} 必须是非空文本")
    return value.strip()


def _identity(value) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError("invalid UUID")
    except (ValueError, AttributeError) as error:
        raise KnowledgeError("validation_error", "知识条目标识必须是规范 UUID") from error
    return value


def _revision(value) -> int:
    if type(value) is not int or value < 1:
        raise KnowledgeError("validation_error", "知识版本必须是正整数")
    return value


def _hash(value, name="content_hash") -> str:
    if not isinstance(value, str) or not domain.HASH.fullmatch(value):
        raise KnowledgeError("validation_error", f"{name} 必须是 SHA256 内容哈希")
    return value


def revision_path(entry_id: str, revision: int) -> str:
    return f"knowledge/entries/{_identity(entry_id)}/revisions/{_revision(revision)}.json"


def pointer_path(entry_id: str) -> str:
    return f"knowledge/entries/{_identity(entry_id)}/current.json"


def binding_path(entry_id: str, revision: int) -> str:
    return f"knowledge/entries/{_identity(entry_id)}/bindings/{_revision(revision)}.json"


def entry_content_hash(entry: dict) -> str:
    return domain.content_hash({key: value for key, value in entry.items() if key != "content_hash"})


def make_changes(entry: dict) -> dict:
    """Build one immutable revision and its current pointer; do not publish."""
    entry = copy.deepcopy(entry)
    entry["content_hash"] = entry_content_hash(entry)
    pointer = {key: entry[key] for key in ("id", "revision", "content_hash")}
    return {revision_path(entry["id"], entry["revision"]): entry,
            pointer_path(entry["id"]): {"schema_version": 1, **pointer}}


def read_entry(project_id: str, entry_id: str, *, revision: int | None = None, current: dict | None = None) -> dict:
    """Read a canonical revision without projecting mutable evidence state."""
    check_project(project_id)
    current = current if current is not None else domain.read(project_id)
    pointer = current["documents"].get(pointer_path(entry_id))
    if revision is None:
        if not pointer:
            raise KnowledgeError("not_found", "知识条目不存在", id=entry_id)
        revision = pointer.get("revision")
    entry = current["documents"].get(revision_path(entry_id, revision))
    if not entry:
        raise KnowledgeError("not_found", "知识条目版本不存在", id=entry_id, revision=revision)
    if (entry.get("id") != entry_id or entry.get("revision") != revision
            or entry.get("content_hash") != entry_content_hash(entry)):
        raise KnowledgeError("validation_error", "知识条目内容哈希校验失败", id=entry_id, revision=revision)
    if pointer and pointer.get("revision") == revision and pointer.get("content_hash") != entry["content_hash"]:
        raise KnowledgeError("validation_error", "知识当前版本指针与内容不一致", id=entry_id)
    return copy.deepcopy(entry)


def _settings(current: dict) -> dict:
    value = current["documents"].get(SETTINGS_PATH)
    return {"settings": value or {"schema_version": 1, "team_id": None, "business_scope": None},
            "content_hash": domain.content_hash(value) if value is not None else None, "domain_head": current["head"]}


def _setting_values(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"expected_hash", "team_id", "business_scope"}:
        raise KnowledgeError("validation_error", "设置需要 expected_hash、team_id 和 business_scope")
    pair = [payload["team_id"], payload["business_scope"]]
    if pair != [None, None] and not all(isinstance(value, str) and value.strip() and value == value.strip() for value in pair):
        raise KnowledgeError("validation_error", "团队和业务范围须同时为空，或同时为无首尾空白的非空文本")
    if payload["expected_hash"] is not None:
        _hash(payload["expected_hash"], "expected_hash")
    return {"schema_version": 1, "team_id": pair[0], "business_scope": pair[1]}


def settings(project_id: str, payload: dict | None = None) -> dict:
    """Read grouping metadata or replace it using a content-hash CAS."""
    check_project(project_id)
    with project_lock(project_id):
        current = domain.read(project_id)
        before = _settings(current)
        if payload is None:
            return before
        value = _setting_values(payload)
        if payload["expected_hash"] != before["content_hash"]:
            raise KnowledgeError("revision_conflict", "知识分组设置已变化，请读取后重新提交", actual_hash=before["content_hash"])
        staged = domain.stage(project_id, {SETTINGS_PATH: value}, current["head"], reason="knowledge_settings")
        published = domain.publish(project_id, staged["stage_id"])
        return {"settings": value, "content_hash": domain.content_hash(value), "domain_head": published["head"]}


def _scope(value, project_id: str | None) -> dict:
    if not isinstance(value, dict) or set(value) != {"team_id", "business_scope", "project_id", "source_version_hashes"}:
        raise KnowledgeError("validation_error", "scope 需要团队、业务范围、项目和来源版本哈希")
    identity = _text(value["project_id"], "scope.project_id")
    if not projects.ID.fullmatch(identity) or identity != value["project_id"] or (project_id is not None and identity != project_id):
        raise KnowledgeError("validation_error", "新知识的 scope.project_id 必须属于当前项目")
    for key in ("team_id", "business_scope"):
        if value[key] is not None:
            _text(value[key], "scope." + key)
    hashes = value["source_version_hashes"]
    if not isinstance(hashes, dict):
        raise KnowledgeError("validation_error", "source_version_hashes 必须是来源路径到 SHA256 的对象")
    for name, digest in hashes.items():
        _text(name, "来源路径")
        if "\\" in name or "\x00" in name or ":" in name or name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
            raise KnowledgeError("validation_error", "来源路径必须是项目内相对路径")
        _hash(digest, "来源哈希")
    return copy.deepcopy(value)


def validate_entry_content(entry: dict, *, project_id: str | None = None) -> dict:
    """Validate public body fields, ignoring separately validated imported kernel fields."""
    if not isinstance(entry, dict) or not ENTRY_FIELDS - {"supersedes"} <= entry.keys():
        raise KnowledgeError("validation_error", "知识正文缺少 title、text、kind、scope 或证据引用列表")
    body = {key: copy.deepcopy(entry[key]) for key in ENTRY_FIELDS if key in entry}
    body.update(title=_text(body["title"], "title"), text=_text(body["text"], "text"))
    if not isinstance(body["kind"], str) or body["kind"] not in KINDS:
        raise KnowledgeError("validation_error", "知识类型必须是 observation、procedure 或 root_cause_hypothesis")
    body["scope"] = _scope(body["scope"], project_id)
    for name in ("evidence_refs", "validation_refs"):
        if not isinstance(body[name], list) or any(not isinstance(ref, dict) for ref in body[name]):
            raise KnowledgeError("validation_error", f"{name} 必须是引用对象列表")
    if "supersedes" in body:
        _identity(body["supersedes"])
    return body


def _source_hash(current: dict, project_id: str, name: str) -> str | None:
    logical = name.removeprefix("domain/")
    if logical.startswith("knowledge/"):
        return None
    ref = current["files"].get(logical)
    if ref:
        return ref["sha256"]
    relative = Path(name)
    folder = projects.path(project_id).resolve()
    source = (folder / relative).resolve()
    if not source.is_relative_to(folder) or not source.is_file() or relative.parts[0] != "analysis":
        return None
    return domain.content_hash(source.read_bytes())


def _applicability(project_id: str, entry: dict, current: dict) -> str:
    expected = entry["scope"]["source_version_hashes"]
    if not expected or entry["scope"]["project_id"] != project_id:
        return "unknown"
    actual = {name: _source_hash(current, project_id, name) for name in expected}
    if any(actual[name] is not None and actual[name] != digest for name, digest in expected.items()):
        return "changed"
    return "match" if all(actual.values()) else "unknown"


def _scope_matches(entry: dict, manifest: dict) -> bool:
    if entry["scope"]["project_id"] != manifest["project_id"]:
        return False
    files = manifest["sources"]["files"]
    return all((files.get(name) or files.get("domain/" + name) or {}).get("sha256") == digest
               for name, digest in entry["scope"]["source_version_hashes"].items())


def _receipt(ref: dict, run: dict, result: dict) -> tuple[str, dict | None]:
    detail = json.loads(result["detail"]) if isinstance(result["detail"], str) else result["detail"]
    if not isinstance(detail, dict) or not isinstance(detail.get("attempts"), list):
        return "tampered", None
    identity = ref.get("execution_attempt_id") or ref.get("attempt_id") or ref.get("id")
    candidate = next((item for item in detail.get("attempts", []) if item["id"] == identity), None)
    if not candidate:
        return "missing", None
    wanted = ref.get("receipt_sha256", ref.get("sha256", ref.get("content_hash")))
    if wanted != candidate["sha256"]:
        return "tampered", None
    file = data_refs.resolve(candidate["path"])
    folder = (paths.local() / "runs" / run["id"] / "attempts").resolve()
    if not file.resolve().is_relative_to(folder):
        return "tampered", None
    receipt = json.loads(file.read_bytes())
    if not isinstance(receipt, dict) or domain.content_hash(receipt) != wanted or receipt.get("state") != "completed":
        return "tampered", None
    if not isinstance(receipt.get("outcome"), dict):
        return "tampered", None
    if not _receipt_pointer(receipt, ref.get("pointer", "")):
        return "tampered", None
    artifact_status = _receipt_artifacts(receipt, folder.parent)
    if artifact_status != "valid":
        return artifact_status, None
    expected = {"input_hash": run["input_hash"], "case_scope_hash": detail.get("case_scope_hash"), "case_id": result["case_id"]}
    if any(name in ref and ref[name] != value for name, value in expected.items()):
        return "tampered", None
    return "valid", {"run_id": run["id"], "result_id": result["id"], "execution_attempt_id": identity,
                     "sha256": wanted, "fact_checked": _fact_checked(receipt, ref.get("pointer", "")), **expected}


def _fact_checked(receipt: dict, pointer: str) -> bool:
    checks = receipt.get("outcome", {}).get("assertions", [])
    if not isinstance(checks, list):
        return False
    selected = checks
    if pointer not in ("", "/outcome", "/outcome/assertions"):
        parts = pointer.split("/")
        if len(parts) < 4 or parts[1:3] != ["outcome", "assertions"] or not parts[3].isdigit():
            return False
        index = int(parts[3])
        selected = checks[index:index + 1]
    return any(isinstance(check, dict) and check.get("name") != "全部动作可以完成" and type(check.get("passed")) is bool
               and "actual" in check and "expected" in check for check in selected)


def _receipt_artifacts(receipt: dict, folder: Path) -> str:
    for ref in receipt.get("artifacts", []):
        file = (folder / ref["path"]).resolve()
        if not file.is_relative_to(folder):
            return "tampered"
        if not file.is_file():
            return "missing"
        if domain.content_hash(file.read_bytes()) != ref["sha256"]:
            return "tampered"
    return "valid"


def _receipt_pointer(receipt: dict, pointer) -> bool:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        return False
    value = receipt
    try:
        for part in pointer.split("/")[1:]:
            token = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                    return False
                value = value[int(token)]
            else:
                value = value[token]
    except (KeyError, IndexError, TypeError):
        return False
    return True


def _evidence_result(ref: dict) -> dict | None:
    if type(ref.get("result_id")) is int:
        return db.one("SELECT * FROM results WHERE id = ?", (ref["result_id"],))
    if not isinstance(ref.get("case_id"), str):
        return None
    results = db.rows("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (ref["run_id"], ref["case_id"]))
    return results[0] if len(results) == 1 else None


def _evidence_ref(project_id: str, entry: dict, ref: dict) -> tuple[str, dict | None]:
    if not isinstance(ref.get("run_id"), str):
        return "missing", None
    run = db.one("SELECT * FROM runs WHERE id = ?", (ref["run_id"],))
    result = _evidence_result(ref)
    if not run or not result:
        return "missing", None
    if run["project_id"] != project_id or result["project_id"] != project_id or result["run_id"] != run["id"]:
        return "tampered", None
    if entry.get("imported_at") and (run.get("started_at") or "") <= entry["imported_at"]:
        return "missing", None
    manifest = execution_inputs.load(run)
    if not _scope_matches(entry, manifest):
        return "tampered", None
    status, bound = _receipt(ref, run, result)
    if status == "valid" and not execution_inputs.result_evidence(run, result):
        return "tampered", None
    return status, bound


def _evidence(project_id: str, entry: dict) -> tuple[str, list[dict]]:
    statuses, bound = [], []
    for ref in entry["evidence_refs"]:
        try:
            status, evidence = _evidence_ref(project_id, entry, ref)
        except FileNotFoundError:
            status, evidence = "missing", None
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            status, evidence = ("missing" if isinstance(error.__cause__, FileNotFoundError) else "tampered"), None
        statuses.append(status)
        if evidence:
            bound.append(evidence)
    state = "tampered" if "tampered" in statuses else "missing" if not statuses or "missing" in statuses else "valid"
    return state, bound


def _workflow_executions(workflow_id: str, validation_id: str) -> list[dict]:
    if not db.one("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workflow_items'"):
        return []
    records = db.rows("SELECT * FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' ORDER BY sequence", (workflow_id,))
    selected = []
    for record in records:
        context = json.loads(record["context"])
        if isinstance(context, dict) and context.get("validation_id") == validation_id:
            selected.append({**record, "context": context})
    return selected


def _run_binding(project_id: str, entry: dict, item: dict) -> dict | None:
    run = db.one("SELECT * FROM runs WHERE id = ?", (item["run_id"],))
    if not run or run["project_id"] != project_id or run["status"] != "Pass" or not run.get("finished_at"):
        return None
    manifest = execution_inputs.load(run)
    context = item["context"]
    if not _scope_matches(entry, manifest) or context.get("validation_scope") != manifest["scope_hashes"]:
        return None
    if context["materialization"]["case_hashes"] != {key: manifest["case_hashes"][key] for key in context["materialization"]["case_ids"]}:
        return None
    results = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run["id"],))
    by_case = {result["case_id"]: result for result in results}
    if not set(manifest["cases"]) <= by_case.keys():
        return None
    attempts = []
    for identity in manifest["cases"]:
        result = by_case[identity]
        detail = json.loads(result["detail"])
        if (not isinstance(detail, dict) or result["project_id"] != project_id or result["status"] != "Pass" or detail.get("business_status") != "Pass"
                or detail.get("business_verified") is not True or not execution_inputs.result_evidence(run, result)):
            return None
        attempts.extend({"run_id": run["id"], "result_id": result["id"], "execution_attempt_id": ref["id"], "sha256": ref["sha256"]}
                        for ref in detail["attempts"])
    frozen = domain.read(project_id, manifest["sources"]["head"])
    root = "workflows/" + item["workflow_id"] + "/"
    definition = frozen["documents"].get(root + "definition.json", {})
    if definition.get("validation", {}).get("repetitions") != context.get("repetitions"):
        return None
    oracles = frozen["documents"].get(root + "oracles.json", [])
    sources = frozen["documents"].get(root + "sources.json", [])
    if not sources or not oracles:
        return None
    return {"run_id": run["id"], "input_hash": run["input_hash"], "case_hashes": manifest["case_hashes"],
            "scope_hashes": manifest["scope_hashes"], "source_hashes": manifest["sources"]["scope"],
            "oracle_hashes": {oracle["id"]: domain.content_hash(oracle) for oracle in oracles}, "attempts": attempts}


def _validation_binding(project_id: str, entry: dict) -> dict | None:
    refs = entry["validation_refs"]
    if len(refs) != 1 or not all(isinstance(refs[0].get(key), str) and refs[0][key] for key in ("workflow_id", "validation_id")):
        return None
    ref = refs[0]
    items = _workflow_executions(ref["workflow_id"], ref["validation_id"])
    if len(items) < 3 or any(item["state"] != "completed" for item in items):
        return None
    count = items[0]["context"].get("repetitions")
    if type(count) is not int or count < 3 or len(items) != count or len({item["run_id"] for item in items}) != count:
        return None
    if ({item["context"].get("round") for item in items} != set(range(1, count + 1))
            or any(item["context"].get("repetitions") != count for item in items)
            or len({item["cycle_id"] for item in items}) != 1):
        return None
    if entry.get("imported_at") and items[0]["created_at"] <= entry["imported_at"]:
        return None
    bound = [_run_binding(project_id, entry, item) for item in items]
    if any(record is None for record in bound):
        return None
    first = bound[0]
    keys = ("case_hashes", "scope_hashes", "source_hashes", "oracle_hashes")
    if any(any(record[key] != first[key] for key in keys) for record in bound):
        return None
    return {"workflow_id": ref["workflow_id"], "validation_id": ref["validation_id"], "repetitions": count,
            "case_bundle_hash": domain.content_hash(first["case_hashes"]), "source_hashes": first["source_hashes"],
            "oracle_hashes": first["oracle_hashes"], "case_scope_hashes": first["scope_hashes"],
            "run_ids": [record["run_id"] for record in bound], "input_hashes": {record["run_id"]: record["input_hash"] for record in bound},
            "execution_attempt_ids": [attempt["execution_attempt_id"] for record in bound for attempt in record["attempts"]],
            "evidence_refs": [attempt for record in bound for attempt in record["attempts"]]}


def _try_binding(project_id: str, entry: dict) -> dict | None:
    try:
        return _validation_binding(project_id, entry)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _qualification(project_id: str, entry: dict, current: dict) -> tuple[str, str]:
    mode = entry.get("creation_mode")
    if mode == "import":
        return "hypothesis", "imported_unverified"
    if mode != "workflow_v2":
        return "hypothesis", "missing"
    evidence_status, facts = _evidence(project_id, entry)
    if entry["kind"] == "observation":
        observed = evidence_status == "valid" and all(fact["fact_checked"] for fact in facts)
        return ("observed" if observed else "hypothesis"), evidence_status
    if entry["kind"] != "procedure":
        return "hypothesis", evidence_status
    retained = current["documents"].get(binding_path(entry["id"], entry["revision"]))
    if retained is not None and not isinstance(retained, dict):
        return "hypothesis", "tampered"
    binding = _try_binding(project_id, entry) if retained else None
    if binding and retained == {"schema_version": 1, "id": entry["id"], "revision": entry["revision"], "content_hash": entry["content_hash"], **binding}:
        return "execution_reproduced", "valid"
    if retained:
        retained_status, _ = _evidence(project_id, {**entry, "evidence_refs": retained.get("evidence_refs", [])})
        return "hypothesis", "tampered" if retained_status == "valid" else retained_status
    return "hypothesis", evidence_status


def project_entry(project_id: str, entry: dict, *, current: dict | None = None, target_project_id: str | None = None) -> dict:
    """Keep verification, current applicability and lifecycle as independent axes."""
    current = current if current is not None else domain.read(project_id)
    verification, evidence_status = _qualification(project_id, entry, current)
    cross_project = target_project_id is not None and target_project_id != project_id
    source_verification = verification
    if entry.get("creation_mode") == "import":
        source_verification = entry.get("provenance", {}).get("source_verification", "hypothesis")
        if isinstance(source_verification, dict):
            source_verification = source_verification.get("verification", "hypothesis")
        if not isinstance(source_verification, str) or source_verification not in ("hypothesis", "observed", "execution_reproduced"):
            source_verification = "hypothesis"
    return {**copy.deepcopy(entry), "owner_project_id": project_id, "source_verification": source_verification,
            "verification": "hypothesis" if cross_project else verification,
            "applicability": "unknown" if cross_project else _applicability(project_id, entry, current),
            "evidence_status": evidence_status, "evidence_available": evidence_status == "valid"}


def get(project_id: str, entry_id: str, *, revision: int | None = None, current: dict | None = None) -> dict:
    check_project(project_id)
    with project_lock(project_id):
        current = current if current is not None else domain.read(project_id)
        return project_entry(project_id, read_entry(project_id, entry_id, revision=revision, current=current), current=current)


def _legacy_mutations(project_id: str, workflow_id: str, payload: dict, current: dict) -> list[dict]:
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise KnowledgeError("validation_error", "entries 必须是非空知识正文列表")
    source = "workflows/" + workflow_id + "/sources.json"
    hashes = {"domain/" + source: current["files"][source]["sha256"]} if source in current["files"] else {}
    config = _settings(current)["settings"]
    mutations = []
    for old in entries:
        if not isinstance(old, dict):
            raise KnowledgeError("validation_error", "旧知识条目必须是对象")
        text = _text(old.get("text"), "entries.text")
        mutations.append({"op": "create", "client_key": _text(old.get("id"), "entries.id"), "legacy_metadata": copy.deepcopy(old),
                          "entry": {"title": text.splitlines()[0][:80], "text": text, "kind": "observation",
                                    "scope": {"team_id": config["team_id"], "business_scope": config["business_scope"],
                                              "project_id": project_id, "source_version_hashes": hashes}, "evidence_refs": [], "validation_refs": []}})
    return mutations


def _mutations(project_id: str, workflow_id: str, payload: dict, current: dict) -> tuple[list[dict], str]:
    if not isinstance(payload, dict):
        raise KnowledgeError("validation_error", "知识提交必须是对象")
    if "schema_version" not in payload and "mutations" not in payload:
        return _legacy_mutations(project_id, workflow_id, payload, current), "legacy_conversion"
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
        raise KnowledgeError("validation_error", "知识修改需要 schema_version=2")
    value = payload.get("mutations")
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise KnowledgeError("validation_error", "mutations 必须是非空修改对象列表")
    return value, "workflow_v2"


def _existing(project_id: str, mutation: dict, current: dict) -> dict:
    entry = read_entry(project_id, _identity(mutation.get("entry_id")), current=current)
    if (_revision(mutation.get("expected_revision")) != entry["revision"]
            or _hash(mutation.get("expected_content_hash")) != entry["content_hash"]):
        raise KnowledgeError("revision_conflict", "知识版本已变化，请读取当前正文后重新提交", id=entry["id"], actual_revision=entry["revision"])
    return entry


def _mutation_entry(project_id: str, workflow_id: str, cycle_id: str, mutation: dict, mode: str, current: dict) -> dict:
    operation = mutation.get("op")
    key = _text(mutation.get("client_key"), "client_key")
    if operation not in ("create", "revise", "withdraw"):
        raise KnowledgeError("validation_error", "知识修改仅支持 create、revise、withdraw")
    previous = _existing(project_id, mutation, current) if operation != "create" else None
    identity = previous["id"] if previous else str(uuid.uuid5(NAMESPACE, json.dumps([project_id, workflow_id, cycle_id, key], ensure_ascii=False)))
    if operation == "withdraw":
        if "entry" in mutation:
            raise KnowledgeError("validation_error", "withdraw 不能替换正文")
        entry = {**previous, "lifecycle": "withdrawn", "withdrawal_reason": _text(mutation.get("reason"), "reason")}
        entry.pop("content_hash", None)
    else:
        proposed = mutation.get("entry")
        if not isinstance(proposed, dict) or set(proposed) - ENTRY_FIELDS:
            raise KnowledgeError("validation_error", "知识正文包含内核保留字段或格式无效")
        entry = {**validate_entry_content(proposed, project_id=project_id), "lifecycle": "active"}
        if previous:
            entry.update({field: copy.deepcopy(previous[field]) for field in ("provenance", "imported_at") if field in previous})
    entry.update(schema_version=1, id=identity, revision=previous["revision"] + 1 if previous else 1,
                 parent_content_hash=previous["content_hash"] if previous else None, creation_mode=mode,
                 workflow_ref={"workflow_id": workflow_id, "cycle_id": cycle_id, "client_key": key})
    if operation == "withdraw":
        entry["creation_mode"] = previous["creation_mode"]
    if mode == "legacy_conversion":
        entry["legacy_metadata"] = mutation["legacy_metadata"]
    entry["content_hash"] = entry_content_hash(entry)
    return entry


def prepare_mutations(project_id: str, workflow_id: str, cycle_id: str, payload: dict, *, current: dict | None = None) -> dict:
    """Validate the complete batch and return a patch for the caller's atomic publication."""
    check_project(project_id)
    _text(workflow_id, "workflow_id")
    _text(cycle_id, "cycle_id")
    current = current if current is not None else domain.read(project_id)
    mutations, mode = _mutations(project_id, workflow_id, payload, current)
    changes, entries, keys, identities = {}, [], set(), set()
    for mutation in mutations:
        key = _text(mutation.get("client_key"), "client_key")
        if key in keys:
            raise KnowledgeError("validation_error", "同一批知识修改的 client_key 不能重复")
        keys.add(key)
        entry = _mutation_entry(project_id, workflow_id, cycle_id, mutation, mode, current)
        if entry["id"] in identities:
            raise KnowledgeError("validation_error", "同一批修改不能重复修改同一知识条目")
        identities.add(entry["id"])
        revision = revision_path(entry["id"], entry["revision"])
        prior = current["documents"].get(revision)
        if prior is not None and prior != entry:
            raise KnowledgeError("revision_conflict", "知识创建标识已用于其他正文", id=entry["id"])
        pointer = current["documents"].get(pointer_path(entry["id"]))
        if prior is not None and pointer and pointer.get("revision") != entry["revision"]:
            raise KnowledgeError("revision_conflict", "知识已有更新版本，不能重置当前指针", id=entry["id"])
        changes.update(make_changes(entry))
        if entry["creation_mode"] == "workflow_v2" and entry["kind"] == "procedure":
            binding = _try_binding(project_id, entry)
            if binding:
                changes[binding_path(entry["id"], entry["revision"])] = {"schema_version": 1, "id": entry["id"], "revision": entry["revision"],
                                                                         "content_hash": entry["content_hash"], **binding}
        entries.append({"owner_project_id": project_id, "id": entry["id"], "revision": entry["revision"], "content_hash": entry["content_hash"]})
    return {"changes": changes, "entries": entries}


def validate_knowledge_used(project_id: str, refs: list) -> list[dict]:
    """Validate declared exact uses without inferring that they caused a benefit."""
    check_project(project_id)
    if not isinstance(refs, list):
        raise KnowledgeError("validation_error", "knowledge_used 必须是知识版本引用列表")
    checked = []
    for ref in refs:
        fields = {"owner_project_id", "id", "revision", "content_hash", "purpose"}
        if not isinstance(ref, dict) or set(ref) != fields:
            raise KnowledgeError("validation_error", "knowledge_used 需要项目、条目、版本、哈希和使用目的")
        owner = _text(ref["owner_project_id"], "owner_project_id")
        purpose = _text(ref["purpose"], "purpose")
        entry = read_entry(owner, ref["id"], revision=_revision(ref["revision"]))
        if _hash(ref["content_hash"]) != entry["content_hash"]:
            raise KnowledgeError("revision_conflict", "使用的知识版本内容哈希不符", id=entry["id"])
        checked.append({**ref, "purpose": purpose})
    return checked


def _members(project_id: str, team: bool) -> tuple[dict, str, dict]:
    current = domain.read(project_id)
    selected = {project_id: current}
    config = _settings(current)
    settings_hashes = {project_id: config["content_hash"]}
    pair = (config["settings"]["team_id"], config["settings"]["business_scope"])
    if not team or not all(pair):
        return selected, "project_only_unconfigured" if team and not all(pair) else "project_only", settings_hashes
    for folder in sorted(paths.projects_dir().iterdir()):
        if folder.name == project_id or not (folder / "project.json").is_file() or not projects.ID.fullmatch(folder.name):
            continue
        snapshot = domain.read(folder.name)
        other = _settings(snapshot)
        settings_hashes[folder.name] = other["content_hash"]
        if (other["settings"]["team_id"], other["settings"]["business_scope"]) == pair:
            selected[folder.name] = snapshot
    return selected, "team", settings_hashes


def _legacy_entries(project_id: str) -> tuple[list[dict], dict]:
    folder = projects.path(project_id) / "knowledge"
    entries, hashes = [], {}
    for file in sorted(folder.rglob("*")):
        if not file.is_file() or not file.resolve().is_relative_to(folder.resolve()):
            continue
        name = "knowledge/" + file.relative_to(folder).as_posix()
        raw = file.read_bytes()
        digest = domain.content_hash(raw)
        hashes[name] = digest
        if file.suffix.lower() not in (".md", ".txt", ".json", ".csv"):
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if "待补充；未提供不代表不存在" in text and len(text) < 120:
            continue
        entries.append({"owner_project_id": project_id, "id": "legacy:" + name, "revision": None, "content_hash": digest,
                        "title": file.name, "text": text, "kind": "observation", "lifecycle": "active", "legacy": True,
                        "file": name, "verification": "hypothesis", "source_verification": "hypothesis",
                        "applicability": "unknown", "evidence_status": "missing", "evidence_available": False})
    return entries, hashes


def _search_entries(project_id: str, snapshots: dict) -> tuple[list[dict], dict]:
    entries, legacy_hashes = [], {}
    for owner, snapshot in snapshots.items():
        for name, pointer in snapshot["documents"].items():
            if not name.startswith("knowledge/entries/") or not name.endswith("/current.json"):
                continue
            entry = read_entry(owner, pointer["id"], current=snapshot)
            entries.append(project_entry(owner, entry, current=snapshot, target_project_id=project_id))
        legacy, hashes = _legacy_entries(owner)
        entries.extend(legacy)
        legacy_hashes[owner] = hashes
    return entries, legacy_hashes


def _search_summary(entry: dict) -> dict:
    fields = ("owner_project_id", "id", "revision", "content_hash", "kind", "lifecycle", "verification", "source_verification",
              "applicability", "evidence_status", "evidence_available", "legacy", "file")
    return {**{key: entry[key] for key in fields if key in entry}, "title": entry["title"][:160], "title_truncated": len(entry["title"]) > 160,
            "preview": entry["text"][:400], "truncated": len(entry["text"]) > 400, "total_characters": len(entry["text"])}


def search(project_id: str, *, query: str = "", team: bool = False, limit: int = 50, cursor: str | None = None) -> dict:
    """Search stable snapshots with authoritative project grouping and lexical matching."""
    check_project(project_id)
    if not isinstance(query, str) or type(team) is not bool:
        raise KnowledgeError("validation_error", "query 必须是文本，team 必须是布尔值")
    snapshots, scope_note, settings_hashes = _members(project_id, team)
    entries, legacy_hashes = _search_entries(project_id, snapshots)
    needle = query.casefold()
    matching = [entry for entry in entries if needle in entry["title"].casefold() or needle in entry["text"].casefold()]
    matching.sort(key=lambda entry: (not (entry["lifecycle"] == "active" and entry["applicability"] == "match"
                                         and entry["verification"] in ("observed", "execution_reproduced")),
                                     entry["lifecycle"] == "withdrawn", entry["title"].casefold(), entry["owner_project_id"], entry["id"]))
    heads = {owner: snapshot["head"] for owner, snapshot in snapshots.items()}
    snapshot_hash = domain.content_hash({"heads": heads, "legacy": legacy_hashes, "settings": settings_hashes,
                                         "states": [{key: entry[key] for key in ("id", "owner_project_id", "verification", "applicability", "evidence_status")}
                                                    for entry in entries]})
    selected, page = paging.select(matching, content_revision=snapshot_hash, query={"project": project_id, "query": query, "team": team}, cursor=cursor, limit=limit)
    return {"entries": [_search_summary(entry) for entry in selected], "page": page, "pagination": page, "scope_note": scope_note, "domain_heads": heads}
