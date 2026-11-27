"""Conflict detection, center protection, split resolution and export-both packs for sync."""
from __future__ import annotations

import copy
import datetime
import importlib
import json
import re
import uuid
import zipfile
from pathlib import Path

from . import db, execution_inputs, paths, projects
from .core import store
from .locking import named_lock, project_lock

CONFLICT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SENSITIVE_KEYS = {"password", "passwd"}


class ConflictNotFoundError(ValueError):
    """Raised when a requested conflict record does not exist."""

    def __init__(self, conflict_id: str):
        super().__init__(f"找不到冲突记录：{conflict_id}")
        self.code = "not_found"


def _platform_accounts_module():
    try:
        return importlib.import_module(".platform_accounts", __package__)
    except ModuleNotFoundError:
        return None


def _platform_accounts_file() -> Path:
    return paths.local() / "platform-accounts.json"


def load_platform_store() -> dict:
    """Read shared platforms and accounts, preferring platform_accounts when installed."""
    module = _platform_accounts_module()
    if module is not None:
        loader = getattr(module, "load", None)
        if callable(loader):
            loaded = loader()
            if isinstance(loaded, dict) and isinstance(loaded.get("platforms"), dict) and isinstance(loaded.get("accounts"), dict):
                return copy.deepcopy(loaded)
        lister = getattr(module, "list_all", None)
        if callable(lister):
            listed = lister()
            if isinstance(listed, dict):
                raw_platforms = listed.get("platforms")
                raw_accounts = listed.get("accounts")
                platforms = ({item["id"]: item for item in raw_platforms if isinstance(item, dict) and item.get("id")}
                             if isinstance(raw_platforms, list) else raw_platforms)
                accounts = ({item["id"]: item for item in raw_accounts if isinstance(item, dict) and item.get("id")}
                            if isinstance(raw_accounts, list) else raw_accounts)
                if isinstance(platforms, dict) and isinstance(accounts, dict):
                    return {"schema": 1, "platforms": copy.deepcopy(platforms), "accounts": copy.deepcopy(accounts)}
    try:
        raw = json.loads(_platform_accounts_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": 1, "platforms": {}, "accounts": {}}
    if not isinstance(raw, dict):
        return {"schema": 1, "platforms": {}, "accounts": {}}
    return {
        "schema": raw.get("schema", 1),
        "platforms": dict(raw.get("platforms") or {}) if isinstance(raw.get("platforms"), dict) else {},
        "accounts": dict(raw.get("accounts") or {}) if isinstance(raw.get("accounts"), dict) else {},
    }


def save_platform_entry(platform_id: str, entry: dict) -> dict:
    """Create or update a platform definition in the shared store."""
    record = copy.deepcopy(entry)
    record["id"] = platform_id
    record.setdefault("updated_at", db.now())
    module = _platform_accounts_module()
    updater = getattr(module, "upsert_platform", None) if module is not None else None
    if callable(updater):
        return updater(platform_id, record)
    with named_lock("platform-accounts", "store"):
        data = load_platform_store()
        data["platforms"][platform_id] = record
        store.write(_platform_accounts_file(), data)
    return record


def save_account_entry(account_id: str, entry: dict) -> dict:
    """Create or update a shared platform account in plaintext."""
    record = copy.deepcopy(entry)
    record["id"] = account_id
    record.setdefault("updated_at", db.now())
    module = _platform_accounts_module()
    updater = getattr(module, "upsert_account", None) if module is not None else None
    if callable(updater):
        return updater(account_id, record)
    with named_lock("platform-accounts", "store"):
        data = load_platform_store()
        data["accounts"][account_id] = record
        store.write(_platform_accounts_file(), data)
    return record


def strip_passwords(value):
    """Remove plaintext password fields recursively for logs and error packs."""
    if isinstance(value, dict):
        return {key: strip_passwords(item) for key, item in value.items() if key.lower() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [strip_passwords(item) for item in value]
    return value


def _conflicts_dir() -> Path:
    folder = paths.local() / "sync" / "conflicts"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _conflict_path(conflict_id: str) -> Path:
    if not isinstance(conflict_id, str) or not CONFLICT_ID.fullmatch(conflict_id):
        raise ConflictNotFoundError(str(conflict_id))
    return _conflicts_dir() / f"{conflict_id}.json"


def save_conflict(record: dict) -> dict:
    """Persist one conflict record atomically."""
    file = _conflict_path(record["id"])
    execution_inputs.write_json(file, record)
    return record


def get_conflict(conflict_id: str) -> dict:
    """Load one complete conflict record by id."""
    file = _conflict_path(conflict_id)
    if not file.is_file():
        raise ConflictNotFoundError(conflict_id)
    try:
        value = json.loads(file.read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError(f"冲突记录已损坏：{conflict_id}") from error
    if not isinstance(value, dict):
        raise ConflictNotFoundError(conflict_id)
    return value


def _conflict_summary(record: dict) -> dict:
    return {
        "id": record["id"],
        "source_file": record.get("source_file", ""),
        "source_type": record.get("source_type", ""),
        "source_sha256": record.get("source_sha256", ""),
        "project_id": record.get("project_id", ""),
        "project_name": record.get("project_name", ""),
        "status": record.get("status", "pending"),
        "created_at": record.get("created_at"),
        "resolved_at": record.get("resolved_at"),
        "counts": {
            "cases": len(record.get("cases") or []),
            "environments": len(record.get("environments") or []),
            "accounts": len(record.get("accounts") or []),
            "auth": 1 if (record.get("auth") or {}).get("changed") else 0,
        },
    }


def list_conflicts(*, status: str | None = None) -> list[dict]:
    """Return conflict summaries ordered newest first."""
    folder = paths.local() / "sync" / "conflicts"
    if not folder.is_dir():
        return []
    items = []
    for file in sorted(folder.glob("*.json")):
        try:
            record = json.loads(file.read_bytes())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or not record.get("id"):
            continue
        if status and record.get("status") != status:
            continue
        items.append(_conflict_summary(record))
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def ensure_internal_id(case: dict, *, fallback_seed: str | None = None) -> str:
    """Return or assign the immutable internal_id stored under case extensions."""
    extensions = case.setdefault("extensions", {})
    existing = str(extensions.get("internal_id") or "").strip()
    if existing:
        return existing
    generated = f"uid-{uuid.uuid5(uuid.NAMESPACE_URL, fallback_seed).hex}" if fallback_seed is not None else f"uid-{uuid.uuid4().hex}"
    extensions["internal_id"] = generated
    return generated


def comparable_case(case: dict) -> dict:
    """Normalize a case for conflict comparison by excluding local revision and sync metadata."""
    cleaned = copy.deepcopy(case)
    cleaned.pop("revision", None)
    extensions = cleaned.get("extensions")
    if isinstance(extensions, dict):
        extensions.pop("sync", None)
        extensions.pop("internal_id", None)
    return cleaned


def case_content_hash(case: dict) -> str:
    """Compute a stable content hash for comparing local and incoming case bodies."""
    return execution_inputs.content_hash(comparable_case(case))


def _comparable_environment(env: dict) -> dict:
    cleaned = copy.deepcopy(env)
    cleaned.pop("updated_at", None)
    return cleaned


def _comparable_account(account: dict) -> dict:
    cleaned = copy.deepcopy(account)
    cleaned.pop("updated_at", None)
    return cleaned


def diff_fields(local: dict, incoming: dict) -> list[str]:
    """List top-level keys (plus nested extension keys) that differ between two objects."""
    changed = []
    for key in sorted(set(local) | set(incoming)):
        left, right = local.get(key), incoming.get(key)
        if left == right:
            continue
        if key == "extensions" and isinstance(left, dict) and isinstance(right, dict):
            sub = [f"extensions.{subkey}" for subkey in sorted(set(left) | set(right)) if left.get(subkey) != right.get(subkey)]
            changed.extend(sub or ["extensions"])
        else:
            changed.append(key)
    return changed


def _reserved_case_ids(project_id: str, local_cases: list[dict]) -> set[str]:
    reserved = {item["id"] for item in local_cases}
    db.init()
    reserved.update(row["case_id"] for row in db.rows("SELECT case_id FROM deleted_cases WHERE project_id = ?", (project_id,)))
    deleted_file = projects.path(project_id) / "deleted-case-ids.json"
    if deleted_file.is_file():
        reserved.update(store.read(deleted_file))
    return reserved


def _allocate_case_id(kind: str, reserved: set[str]) -> str:
    prefix = "TC-UI-" if kind == "ui" else "TC-API-"
    numbers = [int(identity[len(prefix):]) for identity in reserved
               if identity.startswith(prefix) and identity[len(prefix):].isdigit()]
    candidate = f"{prefix}{max(numbers, default=0) + 1:03d}"
    reserved.add(candidate)
    return candidate


def _match_incoming_cases(project_id: str, local_cases: list[dict], incoming_cases: list[dict]) -> tuple[list[tuple[dict | None, dict]], dict[str, str]]:
    """Match incoming cases by internal_id first, renumbering display ID collisions."""
    by_iid = {str(item.get("extensions", {}).get("internal_id") or "").strip(): item
              for item in local_cases if str(item.get("extensions", {}).get("internal_id") or "").strip()}
    by_id = {item["id"]: item for item in local_cases}
    reserved = _reserved_case_ids(project_id, local_cases)
    claimed_batch: set[str] = set()
    id_remap: dict[str, str] = {}
    pairs: list[tuple[dict | None, dict]] = []

    for raw in incoming_cases:
        inc = copy.deepcopy(raw)
        orig_id = inc["id"]
        inc_iid = str((inc.get("extensions") or {}).get("internal_id") or "").strip()
        local = None
        if inc_iid and inc_iid in by_iid:
            local = by_iid[inc_iid]
            if orig_id != local["id"]:
                id_remap[orig_id] = local["id"]
                inc["id"] = local["id"]
            claimed_batch.add(inc["id"])
        elif orig_id in by_id:
            candidate = by_id[orig_id]
            loc_iid = str((candidate.get("extensions") or {}).get("internal_id") or "").strip()
            if inc_iid and loc_iid and inc_iid != loc_iid:
                new_id = _allocate_case_id(inc.get("kind", "api"), reserved)
                id_remap[orig_id] = new_id
                inc["id"] = new_id
                claimed_batch.add(new_id)
            else:
                local = candidate
                if loc_iid and not inc_iid:
                    inc.setdefault("extensions", {})["internal_id"] = loc_iid
                claimed_batch.add(inc["id"])
        else:
            if orig_id in claimed_batch:
                new_id = _allocate_case_id(inc.get("kind", "api"), reserved)
                id_remap[orig_id] = new_id
                inc["id"] = new_id
            else:
                reserved.add(orig_id)
            claimed_batch.add(inc["id"])
        pairs.append((local, inc))

    if id_remap:
        for _local, inc in pairs:
            extensions = inc.get("extensions")
            if isinstance(extensions, dict):
                for list_key in ("depends_on", "postconditions"):
                    if isinstance(extensions.get(list_key), list):
                        extensions[list_key] = [id_remap.get(dep, dep) for dep in extensions[list_key]]
    return pairs, id_remap


def _classify_cases(
    pairs: list[tuple[dict | None, dict]],
    *,
    source_file: str,
    source_sha256: str,
    stamp: str,
    overwrite: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Split matched cases into safe writes (new/fast-forward) and local-modification conflicts."""
    to_save: list[dict] = []
    conflicts: list[dict] = []
    for local, inc in pairs:
        inc.setdefault("lifecycle", "active")
        inc.setdefault("revision", 1)
        ensure_internal_id(inc)
        inc_hash = case_content_hash(inc)
        sync_meta = {"file": source_file, "sha256": source_sha256, "synced_at": stamp, "base_hash": inc_hash}
        if local is None:
            inc.setdefault("extensions", {})["sync"] = sync_meta
            to_save.append(inc)
            continue
        loc_hash = case_content_hash(local)
        if loc_hash == inc_hash:
            continue
        last_sync = (local.get("extensions") or {}).get("sync")
        if overwrite or (isinstance(last_sync, dict) and last_sync.get("base_hash") == loc_hash):
            inc.setdefault("extensions", {})["sync"] = sync_meta
            to_save.append(inc)
            continue
        inc.setdefault("extensions", {})["sync"] = sync_meta
        local_iid = str((local.get("extensions") or {}).get("internal_id") or "").strip()
        inc_iid = str((inc.get("extensions") or {}).get("internal_id") or "").strip()
        conflicts.append({
            "key": local_iid or inc_iid or local["id"],
            "case_id": local["id"],
            "internal_id": local_iid or inc_iid,
            "title": inc.get("title") or local.get("title") or local["id"],
            "local": copy.deepcopy(local),
            "incoming": copy.deepcopy(inc),
            "diff_fields": diff_fields(comparable_case(local), comparable_case(inc)),
        })
    return to_save, conflicts


def _write_project_config(project_id: str, cfg: dict) -> None:
    """Persist qa-config.json preserving extended environment fields and refreshing the index."""
    folder = projects.path(project_id)
    store.write(folder / "qa-config.json", cfg)
    store.sync_skills(folder)
    projects.reindex(project_id)


def _find_matching_env(envs: list[dict], incoming: dict) -> tuple[int, dict] | None:
    inc_id = str(incoming.get("id") or "").strip()
    inc_name = str(incoming.get("name") or "").strip().lower()
    for index, item in enumerate(envs):
        if inc_id and item.get("id") == inc_id:
            return index, item
    if inc_name:
        for index, item in enumerate(envs):
            if str(item.get("name") or "").strip().lower() == inc_name:
                return index, item
    return None


def _merge_environments(project_id: str, local_cfg: dict, incoming_config: dict | None, *, overwrite: bool = False) -> tuple[list[str], list[dict]]:
    """Add missing environments and protect existing center environments by recording conflicts."""
    if not isinstance(incoming_config, dict):
        return [], []
    incoming_envs = [copy.deepcopy(item) for item in incoming_config.get("environments") or []
                     if isinstance(item, dict) and item.get("id")]
    updated_cfg = copy.deepcopy(local_cfg)
    local_envs = [copy.deepcopy(item) for item in updated_cfg.get("environments") or []
                  if isinstance(item, dict) and item.get("id")]
    added: list[str] = []
    conflicts: list[dict] = []
    changed = False

    if not updated_cfg.get("base_url") and incoming_config.get("base_url"):
        updated_cfg["base_url"] = incoming_config["base_url"]
        origins = sorted({incoming_config["base_url"], *(updated_cfg.get("allowed_origins") or [])} - {""})
        updated_cfg["allowed_origins"] = origins
        changed = True

    has_default = any(item.get("is_default") for item in local_envs)
    for inc_env in incoming_envs:
        matched = _find_matching_env(local_envs, inc_env)
        if matched is None:
            if has_default and inc_env.get("is_default"):
                inc_env["is_default"] = False
            if inc_env.get("is_default"):
                has_default = True
            local_envs.append(inc_env)
            added.append(inc_env["id"])
            changed = True
            continue
        index, loc_env = matched
        if _comparable_environment(loc_env) != _comparable_environment(inc_env):
            if overwrite:
                replacement = copy.deepcopy(inc_env)
                replacement["id"] = loc_env["id"]
                if replacement.get("is_default"):
                    for item in local_envs:
                        item["is_default"] = False
                local_envs[index] = replacement
                added.append(loc_env["id"])
                changed = True
            else:
                conflicts.append({
                    "id": loc_env["id"],
                    "incoming_id": inc_env["id"],
                    "name": loc_env.get("name") or inc_env.get("name") or loc_env["id"],
                    "local": copy.deepcopy(loc_env),
                    "incoming": copy.deepcopy(inc_env),
                    "diff_fields": diff_fields(_comparable_environment(loc_env), _comparable_environment(inc_env)),
                })

    if changed:
        updated_cfg["environments"] = local_envs
        active_id = incoming_config.get("active_environment_id")
        if not updated_cfg.get("active_environment_id") and active_id and any(item["id"] == active_id for item in local_envs):
            updated_cfg["active_environment_id"] = active_id
        _write_project_config(project_id, updated_cfg)
    return added, conflicts


def _find_matching_account(local_accounts: dict[str, dict], incoming: dict) -> dict | None:
    inc_id = str(incoming.get("id") or "").strip()
    if inc_id and inc_id in local_accounts:
        return local_accounts[inc_id]
    inc_label = str(incoming.get("label") or "").strip().lower()
    inc_platform = str(incoming.get("platform_id") or "").strip()
    inc_env = str(incoming.get("environment_key") or "").strip()
    if inc_label:
        for item in local_accounts.values():
            if (str(item.get("label") or "").strip().lower() == inc_label
                    and str(item.get("platform_id") or "").strip() == inc_platform
                    and str(item.get("environment_key") or "").strip() == inc_env):
                return item
    return None


def _referenced_account_ids(incoming_config: dict | None, incoming_cases: list[dict]) -> set[str]:
    ids: set[str] = set()
    if isinstance(incoming_config, dict):
        if incoming_config.get("active_account_id"):
            ids.add(str(incoming_config["active_account_id"]))
        for env in incoming_config.get("environments") or []:
            if not isinstance(env, dict):
                continue
            ids.update(str(aid) for aid in (env.get("account_ids") or []) if aid)
            if env.get("default_account_id"):
                ids.add(str(env["default_account_id"]))
    for item in incoming_cases:
        override = (item.get("extensions") or {}).get("auth_override")
        if isinstance(override, dict) and override.get("account_id"):
            ids.add(str(override["account_id"]))
    return ids


def merge_platform_accounts(
    incoming_store: dict | None,
    *,
    referenced_accounts: set[str] | None = None,
    referenced_platforms: set[str] | None = None,
    overwrite: bool = False,
) -> tuple[list[str], list[dict], dict[str, dict]]:
    """Import missing platforms/accounts while preserving differing center accounts as conflicts."""
    if not isinstance(incoming_store, dict):
        return [], [], {}
    inc_platforms = incoming_store.get("platforms") if isinstance(incoming_store.get("platforms"), dict) else {}
    inc_accounts = incoming_store.get("accounts") if isinstance(incoming_store.get("accounts"), dict) else {}
    if not inc_platforms and not inc_accounts:
        return [], [], {}
    local_store = load_platform_store()
    selected_accounts = ({aid: acc for aid, acc in inc_accounts.items() if aid in referenced_accounts}
                         if referenced_accounts else dict(inc_accounts))
    needed_platforms = set(referenced_platforms or ())
    for acc in selected_accounts.values():
        if isinstance(acc, dict) and acc.get("platform_id"):
            needed_platforms.add(str(acc["platform_id"]))
    selected_platforms = ({pid: plat for pid, plat in inc_platforms.items() if pid in needed_platforms}
                          if needed_platforms else dict(inc_platforms))

    for pid, plat in selected_platforms.items():
        if isinstance(plat, dict) and (overwrite or pid not in local_store["platforms"]):
            save_platform_entry(pid, plat)

    added: list[str] = []
    conflicts: list[dict] = []
    for aid, inc_acc in selected_accounts.items():
        if not isinstance(inc_acc, dict):
            continue
        record = copy.deepcopy(inc_acc)
        record.setdefault("id", aid)
        matched = _find_matching_account(local_store["accounts"], record)
        if matched is None:
            save_account_entry(record["id"], record)
            added.append(record["id"])
            continue
        if _comparable_account(matched) != _comparable_account(record):
            if overwrite:
                replacement = copy.deepcopy(record)
                replacement["id"] = matched["id"]
                save_account_entry(matched["id"], replacement)
                added.append(matched["id"])
            else:
                conflicts.append({
                    "id": matched["id"],
                    "incoming_id": record["id"],
                    "label": matched.get("label") or record.get("label") or matched["id"],
                    "platform_id": matched.get("platform_id") or record.get("platform_id") or "",
                    "local": copy.deepcopy(matched),
                    "incoming": copy.deepcopy(record),
                    "diff_fields": diff_fields(_comparable_account(matched), _comparable_account(record)),
                })
    return added, conflicts, selected_platforms


def _collect_prerequisite_closure(seeds: list[dict], pool: dict[str, dict]) -> list[dict]:
    seen = {item["id"] for item in seeds}
    queue = [dep for item in seeds for dep in projects.depends_on(item)]
    extras: list[dict] = []
    while queue:
        dep = queue.pop(0)
        if dep in seen or dep not in pool:
            continue
        seen.add(dep)
        target = pool[dep]
        extras.append(copy.deepcopy(target))
        queue.extend(projects.depends_on(target))
    return extras


def merge_project_sync(
    project_id: str,
    incoming_cases: list[dict],
    *,
    source_file: str,
    source_type: str,
    source_sha256: str,
    incoming_config: dict | None = None,
    incoming_auth: dict | None = None,
    incoming_requirements: list[dict] | None = None,
    incoming_platform_store: dict | None = None,
    include_environments: bool = True,
    include_accounts: bool = True,
    overwrite: bool = False,
) -> dict:
    """Merge one synced project into an existing local project without deleting omitted cases."""
    stamp = db.now()
    with project_lock(project_id):
        local_cases = projects.cases(project_id, active_only=False)
        local_cfg = projects.config(project_id)
        local_auth = projects.auth(project_id)
        local_meta = projects.meta(project_id)
        pairs, id_remap = _match_incoming_cases(project_id, local_cases, incoming_cases)
        to_save, case_conflicts = _classify_cases(pairs, source_file=source_file, source_sha256=source_sha256, stamp=stamp, overwrite=overwrite)
        if to_save:
            projects.save_cases(project_id, to_save, requirement_records=incoming_requirements)

        added_envs, env_conflicts = (_merge_environments(project_id, local_cfg, incoming_config, overwrite=overwrite)
                                     if include_environments else ([], []))

        remapped_auth = copy.deepcopy(incoming_auth) if isinstance(incoming_auth, dict) else {}
        for key in ("ui_case", "api_case", "api_check_case"):
            if remapped_auth.get(key) in id_remap:
                remapped_auth[key] = id_remap[remapped_auth[key]]
        auth_conflict = {"local": copy.deepcopy(local_auth), "incoming": copy.deepcopy(remapped_auth), "changed": False}
        updated_auth = False
        if remapped_auth:
            if not local_auth or (overwrite and local_auth != remapped_auth):
                projects.set_auth(project_id, remapped_auth)
                updated_auth = True
            elif local_auth != remapped_auth:
                auth_conflict["changed"] = True

    ref_accounts = _referenced_account_ids(incoming_config, [inc for _loc, inc in pairs])
    ref_platforms = ({str(env["platform_id"]) for env in (incoming_config or {}).get("environments") or []
                      if isinstance(env, dict) and env.get("platform_id")}
                     if isinstance(incoming_config, dict) else set())
    added_accounts, account_conflicts, incoming_platforms = (
        merge_platform_accounts(incoming_platform_store, referenced_accounts=ref_accounts or None,
                                referenced_platforms=ref_platforms or None, overwrite=overwrite)
        if include_accounts else ([], [], {})
    )

    conflict = None
    if case_conflicts or env_conflicts or account_conflicts or auth_conflict["changed"]:
        incoming_pool = {inc["id"]: inc for _loc, inc in pairs}
        prereqs = _collect_prerequisite_closure([entry["incoming"] for entry in case_conflicts], incoming_pool)
        conflict = save_conflict({
            "id": db.new_id("conflict"),
            "source_file": source_file,
            "source_type": source_type,
            "source_sha256": source_sha256,
            "project_id": project_id,
            "project_name": local_meta.get("name") or project_id,
            "status": "pending",
            "created_at": stamp,
            "resolved_at": None,
            "cases": case_conflicts,
            "environments": env_conflicts,
            "accounts": account_conflicts,
            "auth": auth_conflict,
            "incoming_platforms": incoming_platforms,
            "incoming_prerequisites": prereqs,
        })

    return {
        "project_id": project_id,
        "imported_cases": [item["id"] for item in to_save],
        "renumbered": id_remap,
        "added_environments": added_envs,
        "added_accounts": added_accounts,
        "updated_auth": updated_auth,
        "conflict_id": conflict["id"] if conflict else None,
        "conflict": conflict,
    }


def _parse_section_choice(raw, ids_key: str) -> tuple[str, set[str]]:
    if raw is None:
        return "keep_local", set()
    if isinstance(raw, str):
        if raw not in ("keep_local", "use_incoming"):
            raise ValueError(f"不支持的冲突处理模式：{raw}")
        return raw, set()
    if not isinstance(raw, dict):
        raise ValueError("冲突处理选项必须是对象或模式名称")
    mode = str(raw.get("mode") or "keep_local")
    if mode not in ("keep_local", "use_incoming", "custom"):
        raise ValueError(f"不支持的冲突处理模式：{mode}")
    selected = raw.get(ids_key) or []
    if not isinstance(selected, list):
        raise ValueError(f"{ids_key} 必须是数组")
    return mode, {str(value) for value in selected}


def _apply_case_resolution(project_id: str, conflict: dict, decision: dict) -> list[str]:
    mode, keys = _parse_section_choice(decision.get("cases"), "use_incoming_keys")
    if mode == "keep_local":
        return []
    chosen = []
    for entry in conflict.get("cases") or []:
        matches = mode == "use_incoming" or bool({entry.get("key"), entry.get("case_id"), entry.get("internal_id")} & keys)
        if not matches:
            continue
        item = copy.deepcopy(entry["incoming"])
        item["id"] = entry["case_id"]
        if entry.get("internal_id"):
            item.setdefault("extensions", {})["internal_id"] = entry["internal_id"]
        item.setdefault("extensions", {})["sync"] = {
            "file": conflict.get("source_file", ""),
            "sha256": conflict.get("source_sha256", ""),
            "synced_at": db.now(),
            "base_hash": case_content_hash(item),
        }
        chosen.append(item)
    if chosen:
        projects.save_cases(project_id, chosen)
    return [item["id"] for item in chosen]


def _apply_env_resolution(project_id: str, conflict: dict, decision: dict) -> list[str]:
    mode, ids = _parse_section_choice(decision.get("environments"), "use_incoming_ids")
    if mode == "keep_local":
        return []
    applied: list[str] = []
    with project_lock(project_id):
        cfg = projects.config(project_id)
        envs = [copy.deepcopy(item) for item in cfg.get("environments") or [] if isinstance(item, dict) and item.get("id")]
        for entry in conflict.get("environments") or []:
            if mode != "use_incoming" and entry["id"] not in ids and entry.get("incoming_id") not in ids:
                continue
            replacement = copy.deepcopy(entry["incoming"])
            replacement["id"] = entry["id"]
            index = next((idx for idx, item in enumerate(envs) if item["id"] == entry["id"]), None)
            if replacement.get("is_default"):
                for item in envs:
                    item["is_default"] = False
            if index is None:
                envs.append(replacement)
            else:
                envs[index] = replacement
            applied.append(entry["id"])
        if applied:
            cfg["environments"] = envs
            _write_project_config(project_id, cfg)
    return applied


def _apply_account_resolution(conflict: dict, decision: dict) -> list[str]:
    mode, ids = _parse_section_choice(decision.get("accounts"), "use_incoming_ids")
    if mode == "keep_local":
        return []
    applied: list[str] = []
    incoming_platforms = conflict.get("incoming_platforms") or {}
    local_store = load_platform_store()
    for entry in conflict.get("accounts") or []:
        if mode != "use_incoming" and entry["id"] not in ids and entry.get("incoming_id") not in ids:
            continue
        replacement = copy.deepcopy(entry["incoming"])
        replacement["id"] = entry["id"]
        platform_id = replacement.get("platform_id")
        if platform_id and platform_id not in local_store["platforms"] and platform_id in incoming_platforms:
            save_platform_entry(platform_id, incoming_platforms[platform_id])
        save_account_entry(entry["id"], replacement)
        applied.append(entry["id"])
    return applied


def resolve(conflict_id: str, decision: dict) -> dict:
    """Apply split resolution choices (cases, environments, accounts, auth) and mark resolved."""
    if not isinstance(decision, dict):
        raise ValueError("冲突处理参数必须是对象")
    with named_lock("sync-conflict", conflict_id):
        conflict = get_conflict(conflict_id)
        if conflict.get("status") == "resolved":
            raise ValueError("该冲突记录已处理")
        project_id = conflict["project_id"]
        applied_cases = _apply_case_resolution(project_id, conflict, decision)
        applied_envs = _apply_env_resolution(project_id, conflict, decision)
        applied_accounts = _apply_account_resolution(conflict, decision)
        auth_choice = str(decision.get("auth") or "keep_local")
        if auth_choice not in ("keep_local", "use_incoming"):
            raise ValueError(f"不支持的登录配置处理模式：{auth_choice}")
        applied_auth = False
        if auth_choice == "use_incoming" and (conflict.get("auth") or {}).get("changed"):
            projects.set_auth(project_id, copy.deepcopy(conflict["auth"]["incoming"]))
            applied_auth = True
        conflict["status"] = "resolved"
        conflict["resolved_at"] = db.now()
        conflict["resolution"] = {
            "cases": decision.get("cases", {"mode": "keep_local"}),
            "environments": decision.get("environments", {"mode": "keep_local"}),
            "accounts": decision.get("accounts", {"mode": "keep_local"}),
            "auth": auth_choice,
        }
        save_conflict(conflict)
        return {
            "conflict_id": conflict_id,
            "status": "resolved",
            "applied": {
                "cases": applied_cases,
                "environments": applied_envs,
                "accounts": applied_accounts,
                "auth": applied_auth,
            },
        }


def _diff_markdown(conflict: dict) -> str:
    lines = [
        f"# 同步冲突两版对照：{conflict.get('project_name') or conflict['project_id']}",
        "",
        f"- 冲突编号：{conflict['id']}",
        f"- 项目：{conflict['project_id']}",
        f"- 来源文件：{conflict.get('source_file') or '—'}",
        f"- 创建时间：{conflict.get('created_at') or '—'}",
        "",
    ]
    if conflict.get("cases"):
        lines += ["## 业务用例冲突", ""]
        for item in conflict["cases"]:
            fields = "、".join(item.get("diff_fields") or []) or "内容有差异"
            lines += [
                f"### {item['case_id']} {item.get('title') or ''}".rstrip(),
                f"- 内部标识：{item.get('internal_id') or '—'}",
                f"- 差异字段：{fields}",
                f"- 当前版本：`current/cases/{item['case_id']}.json`",
                f"- 传入版本：`incoming/cases/{item['case_id']}.json`",
                "",
            ]
    if conflict.get("environments"):
        lines += ["## 环境配置冲突", ""]
        for item in conflict["environments"]:
            fields = "、".join(item.get("diff_fields") or []) or "配置有差异"
            lines += [
                f"### 环境 {item.get('name') or item['id']}（{item['id']}）",
                f"- 差异字段：{fields}",
                f"- 当前地址：{(item.get('local') or {}).get('base_url') or '—'}",
                f"- 传入地址：{(item.get('incoming') or {}).get('base_url') or '—'}",
                "",
            ]
    if conflict.get("accounts"):
        lines += ["## 平台账号冲突", ""]
        for item in conflict["accounts"]:
            fields = "、".join(item.get("diff_fields") or []) or "配置有差异"
            lines += [
                f"### 账号 {item.get('label') or item['id']}（{item['id']}）",
                f"- 平台：{item.get('platform_id') or '—'}",
                f"- 差异字段：{fields}",
                f"- 当前用户名：{(item.get('local') or {}).get('username') or '—'}",
                f"- 传入用户名：{(item.get('incoming') or {}).get('username') or '—'}",
                "",
            ]
    if (conflict.get("auth") or {}).get("changed"):
        lines += ["## 登录配置冲突", "", "- 当前与传入 `auth.json` 不同，详见 `current/auth.json` 与 `incoming/auth.json`。", ""]
    return "\n".join(lines).rstrip() + "\n"


def export_both(conflict_id: str) -> Path:
    """Build a single ZIP containing current/, incoming/, conflict.json and diff.md."""
    conflict = get_conflict(conflict_id)
    project_id = conflict["project_id"]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")  # noqa: DTZ005  # Export filenames use local wall clock.
    folder = paths.local() / "sync" / "conflict-exports"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{project_id}-conflict-{stamp}-{conflict_id[-6:]}.zip"
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")

    local_pool: dict[str, dict] = {}
    local_meta = {}
    try:
        local_pool = {item["id"]: item for item in projects.cases(project_id, active_only=False)}
        local_meta = projects.meta(project_id)
    except ValueError:
        pass

    current_cases = [copy.deepcopy(entry["local"]) for entry in conflict.get("cases") or [] if isinstance(entry.get("local"), dict)]
    current_prereqs = _collect_prerequisite_closure(current_cases, local_pool)
    incoming_cases = [copy.deepcopy(entry["incoming"]) for entry in conflict.get("cases") or [] if isinstance(entry.get("incoming"), dict)]
    incoming_prereqs = list(conflict.get("incoming_prerequisites") or [])

    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("conflict.json", json.dumps(conflict, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("diff.md", _diff_markdown(conflict))
            archive.writestr("current/project.json", json.dumps(local_meta, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("current/environments.json",
                             json.dumps([entry["local"] for entry in conflict.get("environments") or []], ensure_ascii=False, indent=2) + "\n")
            archive.writestr("current/accounts.json",
                             json.dumps([entry["local"] for entry in conflict.get("accounts") or []], ensure_ascii=False, indent=2) + "\n")
            archive.writestr("current/auth.json",
                             json.dumps((conflict.get("auth") or {}).get("local") or {}, ensure_ascii=False, indent=2) + "\n")
            for item in [*current_cases, *current_prereqs]:
                archive.writestr(f"current/cases/{item['id']}.json", json.dumps(item, ensure_ascii=False, indent=2) + "\n")

            archive.writestr("incoming/environments.json",
                             json.dumps([entry["incoming"] for entry in conflict.get("environments") or []], ensure_ascii=False, indent=2) + "\n")
            archive.writestr("incoming/accounts.json",
                             json.dumps([entry["incoming"] for entry in conflict.get("accounts") or []], ensure_ascii=False, indent=2) + "\n")
            archive.writestr("incoming/auth.json",
                             json.dumps((conflict.get("auth") or {}).get("incoming") or {}, ensure_ascii=False, indent=2) + "\n")
            for item in [*incoming_cases, *incoming_prereqs]:
                archive.writestr(f"incoming/cases/{item['id']}.json", json.dumps(item, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
