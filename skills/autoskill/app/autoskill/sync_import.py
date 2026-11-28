"""Sync folder scanning, half-written deferral, and conflict-aware single-ZIP imports.

Supports format 3 single-ZIP sync packs, legacy format 1/2 share ZIPs, and JSON bundles.
Only passing cases are synced. Incomplete uploads are deferred as pending until stable.
Local edits and existing center environments/accounts are preserved and recorded as conflicts.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from . import db, execution_inputs, file_changes, knowledge, paths, projects, runtime_identity, share, sync_conflicts
from .core import store
from .locking import named_lock, project_lock

PASS = {"", "pass", "passed", "通过"}
TEMP_SUFFIXES = {".part", ".tmp", ".uploading"}
DEFAULT_STABLE_SECONDS = 2.0
DEFAULT_GRACE_SECONDS = 30.0
_last_attempt = 0.0


def _state_file() -> Path:
    return paths.local() / "sync" / "state.json"


def _pending_file() -> Path:
    return paths.local() / "sync" / "pending.json"


def _read_state() -> dict:
    try:
        value = json.loads(_state_file().read_bytes())
    except (OSError, ValueError):
        return {"files": {}}
    return value if isinstance(value, dict) and isinstance(value.get("files"), dict) else {"files": {}}


def _read_pending() -> dict:
    try:
        value = json.loads(_pending_file().read_bytes())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _stable_seconds() -> float:
    raw = os.environ.get("AUTOSKILL_SYNC_STABLE_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_STABLE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_STABLE_SECONDS


def _grace_seconds(stable: float) -> float:
    raw = os.environ.get("AUTOSKILL_SYNC_GRACE_SECONDS")
    if raw is not None and raw.strip():
        try:
            return max(0.0, float(raw))
        except ValueError:
            return DEFAULT_GRACE_SECONDS
    return DEFAULT_GRACE_SECONDS if stable > 0 else 0.0


def _is_temp_file(file: Path) -> bool:
    name = file.name.lower()
    return name.startswith(".") or file.suffix.lower() in TEMP_SUFFIXES


def status() -> dict:
    """Return configured sync/error folders, file receipts and pending conflicts."""
    folder, error_dir = paths.sync_dir(), paths.error_dir()
    state = _read_state()
    conflicts = sync_conflicts.list_conflicts()
    return {
        "sync_dir": str(folder) if folder else None,
        "error_dir": str(error_dir) if error_dir else None,
        "last_scan": state.get("last_scan"),
        "files": [{"file": name, **row} for name, row in sorted(state["files"].items())],
        "conflicts_count": sum(item.get("status") == "pending" for item in conflicts),
        "conflicts": conflicts,
    }


def _check_ready(file: Path, relative: str, pending: dict, now: float) -> tuple[bool, str | None, str | None]:
    """Ensure a file is stable on disk and structurally complete before importing."""
    stable = _stable_seconds()
    grace = _grace_seconds(stable)
    try:
        info = file.stat()
    except OSError as error:
        return False, "failed", str(error)[:200]
    size, mtime_ns = info.st_size, info.st_mtime_ns
    entry = pending.get(relative) if isinstance(pending.get(relative), dict) else None

    if stable > 0:
        if entry is None:
            if now - info.st_mtime < stable:
                pending[relative] = {"size": size, "mtime_ns": mtime_ns, "first_seen": now, "stable_since": now}
                return False, "pending", "文件仍在传输中，等待稳定后自动导入"
            entry = {"size": size, "mtime_ns": mtime_ns, "first_seen": min(now, info.st_mtime), "stable_since": info.st_mtime}
        elif entry.get("size") != size or entry.get("mtime_ns") != mtime_ns:
            entry["size"] = size
            entry["mtime_ns"] = mtime_ns
            entry["stable_since"] = now
            pending[relative] = entry
            return False, "pending", "文件仍在写入中，等待稳定后自动导入"
        elif now - float(entry.get("stable_since", now)) < stable and now - info.st_mtime < stable:
            return False, "pending", "文件等待稳定窗口结束"

    first_seen = float(entry.get("first_seen", min(now, info.st_mtime))) if entry else min(now, info.st_mtime)
    if file.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(file) as archive:
                bad = archive.testzip()
        except (zipfile.BadZipFile, OSError):
            bad = "corrupt"
        if bad is not None:
            if grace > 0 and now - first_seen < grace:
                pending[relative] = {"size": size, "mtime_ns": mtime_ns, "first_seen": first_seen, "stable_since": now}
                return False, "pending", "同步包尚未传输完整，稍后自动重试"
            pending.pop(relative, None)
            return False, "failed", "同步包已损坏或不是有效的 ZIP 文件"
    elif file.suffix.lower() == ".json":
        try:
            json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            if grace > 0 and now - first_seen < grace:
                pending[relative] = {"size": size, "mtime_ns": mtime_ns, "first_seen": first_seen, "stable_since": now}
                return False, "pending", "JSON 文件尚未传输完整，稍后自动重试"
            pending.pop(relative, None)
            return False, "failed", "不是有效的 JSON 文件"
    return True, None, None


def _stamp_new_project_cases(project_id: str, relative: str, digest: str) -> list[str]:
    """Attach sync metadata and internal_id to cases imported into a brand-new project."""
    stamp = db.now()
    with project_lock(project_id):
        current = projects.cases(project_id, active_only=False)
        if not current:
            return []
        updated = []
        for item in current:
            copy_item = copy.deepcopy(item)
            sync_conflicts.ensure_internal_id(copy_item)
            copy_item.setdefault("extensions", {})["sync"] = {
                "file": relative,
                "sha256": digest,
                "synced_at": stamp,
                "base_hash": sync_conflicts.case_content_hash(copy_item),
            }
            updated.append(copy_item)
        projects.save_cases(project_id, updated)
        return [item["id"] for item in updated]


def _file_digest(file: Path) -> str:
    with file.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _cleanup_staging() -> None:
    """Retire only our interrupted extraction directories, under the scan lock."""
    from .runner import process_alive

    root = paths.workspace()
    staging = root / ".local" / "import"
    for work in staging.glob("sync-*"):
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            if work.is_symlink() or not work.is_dir() or work.resolve().parent != staging.resolve():
                continue
            marker = work / ".autoskill-extraction.json"
            if marker.is_symlink() or time.time() - marker.stat().st_mtime < 3600:
                continue
            value = json.loads(marker.read_bytes())
            owner = value.get("owner") if isinstance(value, dict) and value.get("folder") == work.name else None
            if (isinstance(owner, dict) and runtime_identity.is_local(owner, root)
                    and type(owner.get("pid")) is int and owner["pid"] > 0 and not process_alive(owner["pid"])):
                shutil.rmtree(work)


@contextlib.contextmanager
def _read_staged_project(archive: zipfile.ZipFile, project_id: str, members: list[zipfile.ZipInfo]):
    """Extract and validate one project from a ZIP into a temporary staging folder."""
    folder = paths.local() / "import"
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"sync-{project_id}-", dir=folder))
    staging = work / "incoming"
    try:
        execution_inputs.write_json(work / ".autoskill-extraction.json", {"folder": work.name, "owner": runtime_identity.current()})
        for member in members:
            destination = staging / Path(*member.filename.split("/")[2:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
        share._validate_import(staging)
        cfg = store.read(staging / "qa-config.json")
        auth_file = staging / "auth.json"
        auth = store.read(auth_file) if auth_file.is_file() else {}
        req_file = staging / "analysis" / "requirements.json"
        requirements = store.read(req_file) if req_file.is_file() else None
        cases = store.load_cases(staging)
        yield {"config": cfg, "auth": auth, "requirements": requirements, "cases": cases, "knowledge": staging / "knowledge"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _merge_knowledge(project_id: str, incoming: Path, *, overwrite: bool, versioned: bool) -> tuple[bool, list[str]]:
    files = [(source, source.relative_to(incoming).as_posix()) for source in sorted(incoming.rglob("*")) if source.is_file()]
    updates, alternatives = [], []
    for source, name in files:
        target = knowledge.file_path(project_id, name)
        if target.is_dir():
            raise ValueError(f"知识同步路径是目录：{name}")
        incoming_hash = _file_digest(source)
        if target.is_file():
            local_hash = _file_digest(target)
            if local_hash == incoming_hash:
                continue
            starter = knowledge.STARTER_FILES.get(name)
            starter_hash = hashlib.sha256(starter.encode("utf-8")).hexdigest() if starter else None
            if versioned and incoming_hash == starter_hash:
                continue
            if not overwrite and (not versioned or local_hash != starter_hash):
                suffix = f"-同步-{incoming_hash[:12]}"
                for number in range(1, 1000):
                    serial = f"-{number}" if number > 1 else ""
                    alternative = target.with_name(target.stem + suffix + serial + target.suffix)
                    alternative_name = alternative.relative_to(projects.path(project_id) / "knowledge").as_posix()
                    alternative = knowledge.file_path(project_id, alternative_name)
                    if not alternative.exists() or (alternative.is_file() and _file_digest(alternative) == incoming_hash):
                        target = alternative
                        break
                else:
                    raise ValueError(f"无法为有冲突的知识文件保留同步副本：{name}")
                alternatives.append(alternative_name)
                if target.exists():
                    continue
        updates.append((source, target))
    for source, target in updates:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    return bool(updates), alternatives


def _import_new_project_accounts(
    project_id: str,
    platform_store: dict | None,
    relative: str,
    digest: str,
    source_type: str,
    *,
    overwrite: bool = False,
) -> dict | None:
    if not isinstance(platform_store, dict):
        return None
    cfg = projects.config(project_id)
    cases = projects.cases(project_id, active_only=False)
    ref_accounts = sync_conflicts._referenced_account_ids(cfg, cases)
    ref_platforms = {str(env["platform_id"]) for env in cfg.get("environments") or [] if isinstance(env, dict) and env.get("platform_id")}
    _added, acc_conflicts, inc_platforms = sync_conflicts.merge_platform_accounts(
        platform_store, referenced_accounts=ref_accounts or None, referenced_platforms=ref_platforms or None, overwrite=overwrite
    )
    if not acc_conflicts:
        return None
    meta = projects.meta(project_id)
    return sync_conflicts.save_conflict({
        "id": db.new_id("conflict"),
        "source_file": relative,
        "source_type": source_type,
        "source_sha256": digest,
        "project_id": project_id,
        "project_name": meta.get("name") or project_id,
        "status": "pending",
        "created_at": db.now(),
        "resolved_at": None,
        "cases": [],
        "environments": [],
        "accounts": acc_conflicts,
        "auth": {"local": {}, "incoming": {}, "changed": False},
        "incoming_platforms": inc_platforms,
        "incoming_prerequisites": [],
    })


def _zip_error(reason: str, *, status_value: str = "failed") -> dict:
    return {"status": status_value, "reason": reason, "projects": [], "cases": [], "imported_cases": []}


def _validate_zip_pack(archive: zipfile.ZipFile) -> tuple[dict | None, dict | None, dict | None, dict | None]:
    """Validate ZIP manifest, status, members and optional platform-accounts payload."""
    if share.MANIFEST not in archive.namelist():
        return None, None, None, _zip_error("这不是 autoSkill 分享或同步包")
    try:
        manifest = json.loads(archive.read(share.MANIFEST).decode("utf-8"))
    except ValueError as error:
        return None, None, None, _zip_error("同步包清单无效：" + str(error)[:200])
    if not isinstance(manifest, dict):
        return None, None, None, _zip_error("同步包清单必须是对象")
    marked = str(manifest.get("status") or "").strip().lower()
    if marked not in PASS:
        return None, None, None, _zip_error(f"同步包状态是「{marked}」，只同步通过状态的用例", status_value="skipped")
    try:
        grouped = share._safe_members(archive)
        errors = share._manifest_errors(archive, manifest, grouped)
    except ValueError as error:
        return None, None, None, _zip_error(str(error)[:500])
    if errors:
        reason = "；".join(f"{pid}: {msg}" for pid, msg in sorted(errors.items()))[:500]
        return None, None, None, _zip_error(reason)
    platform_store = None
    if share.PLATFORM_ACCOUNTS in archive.namelist():
        try:
            platform_store = json.loads(archive.read(share.PLATFORM_ACCOUNTS).decode("utf-8"))
        except ValueError as error:
            return None, None, None, _zip_error("平台账号清单无效：" + str(error)[:200])
    return manifest, grouped, platform_store, None


def _import_one_zip_project(
    archive: zipfile.ZipFile,
    project_id: str,
    members: list[zipfile.ZipInfo],
    *,
    relative: str,
    digest: str,
    source_type: str,
    platform_store: dict | None,
    include_envs: bool,
    include_accounts: bool,
    overwrite: bool = False,
) -> tuple[bool, list[str], dict[str, str], dict | None, list[str]]:
    exists = (paths.projects_dir() / project_id / "project.json").is_file()
    if not exists:
        share._import_project(archive, project_id, members, overwrite=False)
        case_ids = _stamp_new_project_cases(project_id, relative, digest)
        conflict = (
            _import_new_project_accounts(project_id, platform_store, relative, digest, source_type, overwrite=overwrite)
            if (include_accounts and platform_store) else None
        )
        return True, case_ids, {}, conflict, []
    with project_lock(project_id), _read_staged_project(archive, project_id, members) as staged:
        merged = sync_conflicts.merge_project_sync(
            project_id,
            staged["cases"],
            source_file=relative,
            source_type=source_type,
            source_sha256=digest,
            incoming_config=staged["config"],
            incoming_auth=staged["auth"],
            incoming_requirements=staged["requirements"],
            incoming_platform_store=platform_store,
            include_environments=include_envs,
            include_accounts=include_accounts,
            overwrite=overwrite,
        )
        knowledge_changed, alternatives = _merge_knowledge(project_id, staged["knowledge"], overwrite=overwrite, versioned=source_type == "sync_zip")
    changed = bool(knowledge_changed or merged["imported_cases"] or merged["added_environments"] or merged["added_accounts"] or merged.get("updated_auth"))
    return changed, merged["imported_cases"], merged["renumbered"], merged["conflict"], alternatives


def _import_zip(file: Path, relative: str, digest: str, *, overwrite: bool = False) -> dict:
    try:
        archive = zipfile.ZipFile(file)
    except (zipfile.BadZipFile, OSError):
        return _zip_error("这不是有效的 autoSkill 同步包")
    with archive:
        manifest, grouped, platform_store, invalid = _validate_zip_pack(archive)
        if invalid is not None or manifest is None or grouped is None:
            return invalid or _zip_error("同步包校验失败")
        include_envs = bool(manifest.get("includes_environments", True))
        include_accounts = bool(manifest.get("includes_platform_accounts", True))
        source_type = "sync_zip" if manifest.get("format") == 3 or manifest.get("pack_type") == "sync" else "share_zip"
        imported_projects: list[str] = []
        imported_cases: list[str] = []
        conflicts: list[dict] = []
        knowledge_copies: dict[str, list[str]] = {}
        renumbered: dict[str, str] = {}
        reasons: list[str] = []

        for project_id, members in grouped.items():
            try:
                changed, case_ids, remapped, conflict, alternatives = _import_one_zip_project(
                    archive,
                    project_id,
                    members,
                    relative=relative,
                    digest=digest,
                    source_type=source_type,
                    platform_store=platform_store,
                    include_envs=include_envs,
                    include_accounts=include_accounts,
                    overwrite=overwrite,
                )
            except (ValueError, KeyError, OSError, TypeError) as error:
                reasons.append(f"{project_id}: {error}")
                continue
            if changed:
                imported_projects.append(project_id)
            imported_cases.extend(case_ids)
            renumbered.update(remapped)
            if conflict is not None:
                conflicts.append(conflict)
            if alternatives:
                knowledge_copies[project_id] = alternatives

        if reasons and not imported_projects and not conflicts:
            return _zip_error("；".join(reasons)[:500])
        status_value = "conflict" if conflicts else ("imported" if imported_projects else "skipped")
        if status_value == "conflict":
            reasons.append(f"发现 {len(conflicts)} 项冲突，已保留本地配置与修改，请前往处理")
        elif status_value == "skipped" and not reasons:
            reasons.append("没有需要更新的新内容")
        if knowledge_copies:
            reasons.append("同名知识文件内容不同，已保留本地内容与同步副本")
        result = {
            "status": status_value,
            "projects": list(dict.fromkeys(imported_projects)),
            "cases": imported_cases,
            "imported_cases": imported_cases,
            "renumbered": renumbered,
            "conflict_id": conflicts[0]["id"] if conflicts else None,
            "conflict_ids": [item["id"] for item in conflicts],
            "knowledge_copies": knowledge_copies,
            "reason": "；".join(reasons)[:500],
        }
        if len(grouped) == 1:
            result["project_id"] = next(iter(grouped))
        if len(conflicts) == 1:
            result["conflict"] = conflicts[0]
        return result


def _bundle(file: Path, relative: str, digest: str, *, overwrite: bool = False) -> dict:
    try:
        value = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return {"status": "failed", "reason": "不是有效的 JSON：" + str(error)[:200], "cases": [], "imported_cases": []}
    if not isinstance(value, dict) or not isinstance(value.get("project_id"), str) or not isinstance(value.get("cases"), list):
        return {"status": "failed", "reason": "JSON 用例文件需要是对象：{project_id, cases, status?}", "cases": [], "imported_cases": []}
    project_id, marked = value["project_id"], str(value.get("status") or "").strip().lower()
    if marked not in PASS:
        return {"status": "skipped", "project_id": project_id, "cases": [], "imported_cases": [],
                "reason": f"文件状态是「{marked}」，只同步通过状态的用例"}
    try:
        projects.path(project_id)
    except ValueError as error:
        return {"status": "failed", "project_id": project_id, "reason": str(error), "cases": [], "imported_cases": []}
    incoming = []
    for case in value["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            return {"status": "failed", "project_id": project_id, "reason": "每条用例都需要 id", "cases": [], "imported_cases": []}
        case_status = str(case.get("status") or (case.get("extensions") or {}).get("status") or "").strip().lower()
        if case_status not in PASS:
            continue
        incoming.append(copy.deepcopy(case))
    if not incoming:
        return {"status": "skipped", "project_id": project_id, "cases": [], "imported_cases": [], "reason": "文件中没有通过状态的用例"}
    incoming_config = {"environments": value["environments"]} if isinstance(value.get("environments"), list) else None
    incoming_auth = value.get("auth") if isinstance(value.get("auth"), dict) else None
    incoming_platform_store = value.get("platform_accounts") if isinstance(value.get("platform_accounts"), dict) else None
    try:
        merged = sync_conflicts.merge_project_sync(
            project_id,
            incoming,
            source_file=relative,
            source_type="json_bundle",
            source_sha256=digest,
            incoming_config=incoming_config,
            incoming_auth=incoming_auth,
            incoming_platform_store=incoming_platform_store,
            overwrite=overwrite,
        )
    except (ValueError, KeyError, OSError, TypeError) as error:
        return {"status": "failed", "project_id": project_id, "reason": str(error)[:500], "cases": [], "imported_cases": []}
    conflict = merged["conflict"]
    has_changes = bool(merged["imported_cases"] or merged["added_environments"] or merged["added_accounts"] or merged.get("updated_auth"))
    status_value = "conflict" if conflict else ("imported" if has_changes else "skipped")
    reason = "发现冲突，已保留本地配置与修改，请前往处理" if conflict else ("" if status_value == "imported" else "用例内容与本机一致，已跳过")
    result = {
        "status": status_value,
        "project_id": project_id,
        "projects": [project_id] if status_value in ("imported", "conflict") else [],
        "cases": merged["imported_cases"],
        "imported_cases": merged["imported_cases"],
        "renumbered": merged["renumbered"],
        "conflict_id": conflict["id"] if conflict else None,
        "conflict_ids": [conflict["id"]] if conflict else [],
        "reason": reason,
    }
    if conflict is not None:
        result["conflict"] = conflict
    return result


def import_file(file: str | Path, *, source_name: str | None = None, overwrite: bool = False) -> dict:
    """Manually import a single sync ZIP or JSON bundle through the shared conflict-aware pipeline."""
    path = Path(file).expanduser()
    if not path.is_file():
        raise ValueError(f"找不到要导入的文件：{file}")
    suffix = path.suffix.lower()
    if suffix not in (".zip", ".json"):
        raise ValueError("仅支持导入 .zip 同步包或 .json 用例文件")
    label = source_name or path.name
    digest = _file_digest(path)
    with named_lock("sync", "scan"):
        _cleanup_staging()
        receipt = _import_zip(path, label, digest, overwrite=overwrite) if suffix == ".zip" else _bundle(path, label, digest, overwrite=overwrite)
        receipt.update(file=label, sha256=digest, at=db.now())
        state = _read_state()
        stored = {key: value for key, value in receipt.items() if key != "conflict"}
        state["files"][f"manual/{label}"] = stored
        execution_inputs.write_json(_state_file(), state)
        return receipt


def _duplicate_hash(state: dict, relative: str, digest: str) -> bool:
    return any(
        name != relative and row.get("sha256") == digest and row.get("status") in ("imported", "conflict", "skipped")
        for name, row in state["files"].items()
    )


def scan() -> dict:
    """Import new or changed files once; unstable or half-written files wait as pending."""
    folder = paths.sync_dir()
    if folder is None:
        return {"status": "not_configured", "sync_dir": None, "files": [],
                "hint": "还没有配置同步目录：设置环境变量 AUTOSKILL_SYNC_DIR，或在数据目录设置里填写 sync_dir"}
    if not folder.is_dir():
        return {"status": "missing", "sync_dir": str(folder), "files": [], "hint": "同步目录不存在，等待开发侧同步后重试"}
    with named_lock("sync", "scan"):
        _cleanup_staging()
        state = _read_state()
        old_stamps = state.get("source_stamps")
        old_stamps = old_stamps if isinstance(old_stamps, dict) else {}
        stamps = {}
        pending = _read_pending()
        now = time.time()
        results = []
        seen_relatives: set[str] = set()
        for file in sorted(folder.rglob("*")):
            if not file.is_file() or _is_temp_file(file) or file.suffix.lower() not in (".json", ".zip"):
                continue
            relative = file.relative_to(folder).as_posix()
            seen_relatives.add(relative)
            previous = state["files"].get(relative)
            stamp = file_changes.file_stamp(file)
            if (previous and previous.get("sha256") and relative not in pending
                    and file_changes.reusable(stamp) and old_stamps.get(relative) == list(stamp)):
                stamps[relative] = stamp
                results.append({**previous, "unchanged": True})
                continue
            ready, override_status, override_reason = _check_ready(file, relative, pending, now)
            if not ready:
                receipt = {"file": relative, "status": override_status, "reason": override_reason or "", "at": db.now(), "cases": [], "imported_cases": []}
                state["files"][relative] = receipt
                results.append(receipt)
                continue
            try:
                digest = _file_digest(file)
            except OSError as error:
                results.append({"file": relative, "status": "failed", "reason": str(error)[:200]})
                continue
            pending.pop(relative, None)
            if file_changes.reusable(stamp) and file_changes.file_stamp(file) == stamp:
                stamps[relative] = stamp
            if previous and previous.get("status") != "pending" and previous.get("sha256") == digest:
                results.append({**previous, "unchanged": True})
                continue
            if _duplicate_hash(state, relative, digest):
                receipt = {"file": relative, "status": "skipped", "duplicate": True, "sha256": digest,
                           "reason": "已导入过相同内容的同步包，已跳过", "at": db.now(), "cases": [], "imported_cases": []}
                state["files"][relative] = receipt
                results.append(receipt)
                continue
            receipt = _import_zip(file, relative, digest) if file.suffix.lower() == ".zip" else _bundle(file, relative, digest)
            receipt.update(file=relative, sha256=digest, at=db.now())
            stored = {key: value for key, value in receipt.items() if key != "conflict"}
            state["files"][relative] = stored
            results.append(stored)
        for stale in list(pending):
            if stale not in seen_relatives:
                pending.pop(stale, None)
        state["source_stamps"] = stamps
        state["last_scan"] = {
            "at": db.now(),
            "path": str(folder),
            "files": len(results),
            "imported": sum(row.get("status") == "imported" for row in results),
            "conflict": sum(row.get("status") == "conflict" for row in results),
            "pending": sum(row.get("status") == "pending" for row in results),
            "failed": sum(row.get("status") == "failed" for row in results),
        }
        execution_inputs.write_json(_state_file(), state)
        execution_inputs.write_json(_pending_file(), pending)
        return {"status": "ok", **state["last_scan"], "files": results}


def autoscan() -> None:
    """Run a scan at most once per interval; the queue loop calls this without waiting for a user."""
    global _last_attempt  # noqa: PLW0603  # One interval gate per process keeps the queue loop cheap.
    try:
        interval = max(5.0, float(os.environ.get("AUTOSKILL_SYNC_INTERVAL", "60")))
    except ValueError:
        interval = 60.0
    if time.monotonic() - _last_attempt < interval:
        return
    _last_attempt = time.monotonic()
    if paths.sync_dir() is None:
        return
    with contextlib.suppress(Exception):  # A broken folder must not stop the queue; the next interval retries.
        scan()
