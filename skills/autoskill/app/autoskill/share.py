"""Share projects: pack what you maintain into one file, import what someone gave you.

Project packs carry cases, knowledge, report attachments and login definitions,
with optional passphrase-encrypted credentials. Full skill packs intentionally
carry portable credentials and saved logins. Local run indexes and independent
execution evidence stay in the complete data export.
"""
from __future__ import annotations

import base64
import copy
import datetime
import errno
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import __version__, db, domain, paths, projects, report_locations, secrets
from .locking import named_lock, project_lock

MANIFEST = "autoskill-pack.json"
SECRETS = "secrets.sealed.json"
PLATFORM_ACCOUNTS = "platform-accounts.json"
PASS_STATUSES = {"", "pass", "passed", "通过"}
SKIP_DIRS = {"execution", "output", "staging", "__pycache__"}
PACK_TEMP = re.compile(r"\.autoskill-pack-[0-9a-f]{32}\.tmp")
ROUNDS = 240_000


# --- passphrase encryption (standard library only: PBKDF2 key, HMAC-SHA256 keystream, encrypt-then-MAC) ---
def _keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, ROUNDS, dklen=64)
    return material[:32], material[32:]


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks, counter = [], 0
    while sum(len(b) for b in blocks) < length:
        blocks.append(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(blocks)[:length]


def encrypt(plain: bytes, passphrase: str) -> dict:
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _keys(passphrase, salt)
    cipher = bytes(a ^ b for a, b in zip(plain, _stream(enc_key, nonce, len(plain))))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return {"v": 1, "kdf": f"pbkdf2-sha256:{ROUNDS}", **{k: base64.b64encode(v).decode() for k, v in
                                                        (("salt", salt), ("nonce", nonce), ("data", cipher), ("mac", tag))}}


def decrypt(box: dict, passphrase: str) -> bytes:
    salt, nonce, cipher, tag = (base64.b64decode(box[k]) for k in ("salt", "nonce", "data", "mac"))
    enc_key, mac_key = _keys(passphrase, salt)
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()):
        raise ValueError("口令不正确，或分享包里的凭据已损坏")
    return bytes(a ^ b for a, b in zip(cipher, _stream(enc_key, nonce, len(cipher))))


# --- pack ---
def _copy_windows_dacl(source: Path, target: Path) -> None:
    """Apply the delivered file's DACL to the staging file before it contains any data."""
    import ctypes
    from ctypes import wintypes

    from .win32 import security

    dacl = 0x4  # DACL_SECURITY_INFORMATION
    get = security.GetFileSecurityW
    needed = wintypes.DWORD()
    get(str(source), dacl, None, 0, ctypes.byref(needed))
    if not needed.value:
        error = ctypes.get_last_error()
        if error in (1, 50):  # The destination volume does not support ACLs.
            return
        raise ctypes.WinError(error)
    descriptor = ctypes.create_string_buffer(needed.value)
    if not get(str(source), dacl, descriptor, len(descriptor), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    control, revision = wintypes.WORD(), wintypes.DWORD()
    if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    inherited = 0x20000000 if not control.value & 0x1000 else 0x80000000  # Preserve SE_DACL_PROTECTED.
    set_security = security.SetFileSecurityW
    if not set_security(str(target), dacl | inherited, descriptor):
        error = ctypes.get_last_error()
        if error not in (1, 50):
            raise ctypes.WinError(error)


def _copy_pack_permissions(source: Path, temporary: Path, metadata: os.stat_result) -> None:
    if os.name == "nt":
        _copy_windows_dacl(source, temporary)
        return
    current = temporary.stat()
    if (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
    temporary.chmod(metadata.st_mode & 0o777)
    if hasattr(os, "listxattr"):
        # Linux ACLs can distinguish users with the same group-class mode bits.
        acl = "system.posix_acl_access"
        try:
            existing = os.listxattr(source)
        except OSError as error:
            if error.errno == errno.ENOTSUP:
                return
            raise
        if acl in existing:
            os.setxattr(temporary, acl, os.getxattr(source, acl))
        elif acl in os.listxattr(temporary):
            os.removexattr(temporary, acl)


@contextmanager
def _pack_output(target: Path):
    """Publish a closed archive on the same filesystem, preserving an existing output on failure."""
    try:
        metadata = target.stat()
    except FileNotFoundError:
        metadata = None
    else:
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"无法写入分享包：{target}")
        # Directory replacement rights alone do not authorize writing the existing file.
        os.close(os.open(target, os.O_WRONLY))
    temporary = target.with_name(f".autoskill-pack-{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o666 if metadata is None else 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            if metadata is not None:
                _copy_pack_permissions(target, temporary, metadata)
            yield output
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _pack_artifact(file: Path, target: Path) -> bool:
    """Keep the output and concurrent exporters' staging files out of project attachments."""
    return file == target or PACK_TEMP.fullmatch(file.name) is not None


def _files(folder: Path, target: Path):
    root = folder.resolve()
    for file in sorted(folder.rglob("*")):
        relative = file.relative_to(folder)
        if file.is_file() and relative.parts[0] not in SKIP_DIRS:
            resolved = file.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError(f"项目附件指向项目目录以外：{relative.as_posix()}")
            if not _pack_artifact(resolved, target):
                yield relative, file


def _archive_file(archive: zipfile.ZipFile, file: Path, name: str) -> dict:
    """Hash exactly the bytes written, including large and extensionless assets."""
    digest, size = hashlib.sha256(), 0
    info = zipfile.ZipInfo.from_file(file, name)
    info.compress_type = zipfile.ZIP_DEFLATED
    with file.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size, "mode": file.stat().st_mode & 0o777}


def _archive_project(archive: zipfile.ZipFile, project_id: str, entries: dict, needed: set, target: Path) -> dict:
    with project_lock(project_id):
        folder, info = projects.path(project_id), projects.get(project_id)
        domain.validate_project(folder)
        for relative, file in _files(folder, target):
            name = f"projects/{project_id}/{relative.as_posix()}"
            entries[name] = _archive_file(archive, file, name)
        for case in projects.cases(project_id):
            needed.update(secrets.references(case))
        return {"id": project_id, "name": info["name"], "category": info["category"], "cases": info["cases"]}


def pack(project_ids: list[str] | None = None, out: str | None = None, passphrase: str | None = None) -> dict:
    """Write a share pack. With a passphrase, the secrets the cases refer to travel along, encrypted."""
    chosen = project_ids or [p["id"] for p in projects.list_all() if not p.get("broken")]
    if not chosen:
        raise ValueError("还没有可以分享的项目")
    if passphrase is not None and len(passphrase) < 8:
        raise ValueError("口令至少 8 个字符")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")  # noqa: DTZ005  # Pack filenames use the machine's local wall clock.
    target = Path(out) if out else paths.workspace() / f"autoskill-share-{stamp}.zip"
    if target.suffix.lower() != ".zip":  # a folder, existing or not
        target = target / f"autoskill-share-{stamp}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    listed, needed, entries = [], set(), {}
    destination = target.resolve()
    with _pack_output(destination) as output:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            listed.extend(_archive_project(archive, project_id, entries, needed, destination) for project_id in dict.fromkeys(chosen))
            included = []
            if passphrase is not None:
                values = {name: secrets.get_secret(name) for name in sorted(needed) if secrets.get_secret(name) is not None}
                included = sorted(values)
                archive.writestr(SECRETS, json.dumps(encrypt(json.dumps(values, ensure_ascii=False).encode("utf-8"), passphrase)))
            manifest = {"format": 2, "autoskill": __version__, "created_at": db.now(), "projects": listed, "files": entries,
                        "secrets_needed": sorted(needed), "secrets_included": included,
                        "history_origin": "imported", "includes_local_verification": False}
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        size = output.tell()
    missing = sorted(needed - set(included))
    return {"file": str(target), "bytes": size, "projects": listed,
            "secrets_included": included, "secrets_not_included": missing,
            "note": "分享包包含项目及原始来源、报告附件；来源文件中的认证信息按原文保留；不另外导出保存的登录态、本机运行索引和独立执行证据。"
                    + ("单独导出的本机凭据已用口令加密；把口令通过另一个渠道告诉对方" if included else
                       "未单独导出本机凭据库" + ("；对方首次使用时需要设置：" + "、".join(missing) if missing else ""))}


def pack_full(out: str | None = None) -> dict:
    """Zip the whole skill (runtime, projects, knowledge) so the receiver only has to unzip it.

    Include report attachments, portable credentials and saved logins, leaving local execution indexes and evidence behind.
    """
    from . import sessions
    from .locking import named_lock

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")  # noqa: DTZ005  # Pack filenames use the machine's local wall clock.
    target = Path(out) if out else paths.SKILL.parent / f"autoskill-full-{stamp}.zip"
    if target.suffix.lower() != ".zip":  # a folder, existing or not
        target = target / f"autoskill-full-{stamp}.zip"
    destination = target.resolve()
    if destination.is_relative_to(paths.SKILL.resolve()):
        raise ValueError("完整包不能输出到 Skill 目录内部，请用 --out 指定其他位置")
    target.parent.mkdir(parents=True, exist_ok=True)
    workspace = paths.workspace()
    excluded = [(paths.SKILL / "workspace").resolve()]
    if workspace != paths.SKILL.resolve() and workspace.is_relative_to(paths.SKILL.resolve()):
        excluded.append(workspace)
    else:
        excluded.extend((workspace / "projects", workspace / ".local"))
    count = 0
    listed = [{"id": p["id"], "name": p["name"], "cases": p["cases"]} for p in projects.list_all() if not p.get("broken")]
    needed = {name for p in listed for case in projects.cases(p["id"]) for name in secrets.references(case)}
    with named_lock("credentials", "store"):
        values = {name: value for name in sorted(set(secrets.names()) | needed) if (value := secrets.get_secret(name)) is not None}
    saved = {file.stem: state for file in sorted((workspace / ".local" / "sessions").glob("*.json"))
             if (state := sessions.load(file.stem))}
    with _pack_output(destination) as output:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file in sorted(paths.SKILL.rglob("*")):
                if not file.is_file() or "__pycache__" in file.parts or file.suffix in (".pyc", ".pyo"):
                    continue
                resolved = file.resolve()
                if _pack_artifact(resolved, destination) or any(resolved.is_relative_to(folder) for folder in excluded):
                    continue  # actual data is added below at the standard unpacked workspace location
                archive.write(file, (Path("autoskill") / file.relative_to(paths.SKILL)).as_posix())
                count += 1
            for project in listed:
                with project_lock(project["id"]):
                    folder = projects.path(project["id"])
                    domain.validate_project(folder)
                    for relative, file in _files(folder, destination):
                        archive.write(file, f"autoskill/workspace/projects/{project['id']}/{relative.as_posix()}")
                        count += 1
            archive.writestr("autoskill/workspace/.local/credentials.json", json.dumps(values, ensure_ascii=False, indent=2) + "\n")
            count += 1
            for project_id, state in saved.items():
                archive.writestr(f"autoskill/workspace/.local/sessions/{project_id}.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                count += 1
        size = output.tell()
    missing = sorted(needed - set(values))
    return {"file": str(target), "bytes": size, "files": count, "projects": listed,
            "includes_runtime": paths.VENDOR.is_dir(), "secrets_included": sorted(values), "sessions_included": sorted(saved), "secrets_not_included": missing,
            "note": ("对方解压后即可使用，不需要安装任何东西。" if paths.VENDOR.is_dir() else
                     "这份 Skill 目录里没有随包运行时（不是从完整离线包运行的），对方需要自备运行环境。")
                    + "完整包包含项目报告附件、已保存的凭据和登录态；不包含本机运行索引、独立执行证据、截图和临时进程文件。"
                    + ("仍需设置缺少的凭据：" + "、".join(missing) if missing else "")}


# --- sync pack (format 3) ---
def _latest_case_statuses(project_id: str) -> dict[str, str]:
    """Return each case's latest non-prerequisite replay status from SQLite, if any."""
    db.init()
    rows = db.rows(
        "SELECT r.case_id, r.status FROM results r JOIN runs u ON u.id = r.run_id "
        "WHERE r.project_id = ? AND r.prerequisite = 0 AND u.trigger != 'login' AND u.family != 'load' "
        "ORDER BY r.id DESC",
        (project_id,),
    )
    latest: dict[str, str] = {}
    for row in rows:
        latest.setdefault(row["case_id"], row["status"])
    return latest


def _case_is_passing(case: dict, latest_statuses: dict[str, str]) -> bool:
    if case.get("lifecycle") != "active":
        return False
    declared = str(case.get("status") or (case.get("extensions") or {}).get("status") or "").strip().lower()
    if declared not in PASS_STATUSES:
        return False
    latest = latest_statuses.get(case["id"])
    return latest is None or latest == "Pass"


def _sync_closure(selected_ids: list[str], all_cases: dict[str, dict], auth: dict) -> list[str]:
    """Expand selected cases with transitive depends_on, postconditions and auth login cases."""
    ordered: list[str] = []
    seen: set[str] = set()
    seeds = list(selected_ids)
    for key in ("ui_case", "api_case", "api_check_case"):
        ref = auth.get(key)
        if isinstance(ref, str) and ref in all_cases and ref not in seeds:
            seeds.append(ref)

    def visit(case_id: str) -> None:
        if case_id in seen or case_id not in all_cases:
            return
        seen.add(case_id)
        item = all_cases[case_id]
        for dep in projects.depends_on(item):
            visit(dep)
        for post in (item.get("extensions") or {}).get("postconditions") or []:
            visit(post)
        ordered.append(case_id)

    for case_id in seeds:
        visit(case_id)
    return ordered


def _workspace_seed_prefix() -> str:
    root = paths.workspace()
    try:
        meta = json.loads((root / "data-root.json").read_text(encoding="utf-8"))
        if isinstance(meta, dict) and meta.get("data_id"):
            return str(meta["data_id"])
    except (OSError, ValueError):
        pass
    return str(root)


def _write_archive_json(archive: zipfile.ZipFile, name: str, payload) -> dict:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    archive.writestr(name, raw)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def _select_sync_cases(project_id: str, all_list: list[dict], all_cases: dict[str, dict], selected_ids: list[str] | None, only_passing: bool) -> tuple[list[str], list[str]]:
    latest_statuses = _latest_case_statuses(project_id) if only_passing else {}
    if selected_ids is not None:
        missing = [cid for cid in selected_ids if cid not in all_cases]
        if missing:
            raise ValueError("要导出的用例不存在：" + "、".join(missing))
        candidates = list(dict.fromkeys(selected_ids))
    else:
        candidates = [item["id"] for item in all_list if item.get("lifecycle") == "active"]
    if not only_passing:
        return candidates, []
    eligible = [cid for cid in candidates if _case_is_passing(all_cases[cid], latest_statuses)]
    excluded = [cid for cid in candidates if cid not in eligible]
    return eligible, excluded


def _collect_env_references(cfg: dict, include_environments: bool, referenced_accounts: set[str], referenced_platforms: set[str]) -> None:
    if not include_environments:
        for key in ("environments", "active_environment_id", "active_account_id"):
            cfg.pop(key, None)
        return
    if cfg.get("active_account_id"):
        referenced_accounts.add(str(cfg["active_account_id"]))
    for env in cfg.get("environments") or []:
        if not isinstance(env, dict):
            continue
        if env.get("platform_id"):
            referenced_platforms.add(str(env["platform_id"]))
        if env.get("default_account_id"):
            referenced_accounts.add(str(env["default_account_id"]))
        referenced_accounts.update(str(aid) for aid in (env.get("account_ids") or []) if aid)


def _sync_project_entry(
    archive: zipfile.ZipFile,
    project_id: str,
    selected_ids: list[str] | None,
    *,
    include_environments: bool,
    only_passing: bool,
    entries: dict,
    needed_secrets: set[str],
    referenced_accounts: set[str],
    referenced_platforms: set[str],
    target: Path,
) -> dict | None:
    from . import knowledge, sync_conflicts
    from .core import store

    with project_lock(project_id):
        folder = projects.path(project_id)
        info = projects.get(project_id)
        meta = projects.meta(project_id)
        cfg = copy.deepcopy(projects.config(project_id))
        auth = projects.auth(project_id)
        all_list = projects.cases(project_id, active_only=False)
        all_cases = {item["id"]: item for item in all_list}
        documents = [(name, file) for name, file in knowledge.files(project_id)
                     if (file.suffix.lower() == ".md" or name.startswith("raw/")) and not _pack_artifact(file.resolve(), target)]
        eligible, excluded = _select_sync_cases(project_id, all_list, all_cases, selected_ids, only_passing)
        if not eligible:
            if selected_ids is not None:
                raise ValueError(f"项目 {project_id} 中所选用例没有处于通过状态的用例")
            if not any(name not in knowledge.STARTER_FILES or file.read_bytes() != knowledge.STARTER_FILES[name].encode("utf-8")
                       for name, file in documents):
                return None

        closure_ids = _sync_closure(eligible, all_cases, auth) if eligible else []
        if not closure_ids:
            auth = {}
        seed_prefix = _workspace_seed_prefix()
        ui_cases: list[dict] = []
        api_cases: list[dict] = []
        for cid in closure_ids:
            item = copy.deepcopy(all_cases[cid])
            sync_conflicts.ensure_internal_id(item, fallback_seed=f"{seed_prefix}:{project_id}:{cid}")
            needed_secrets.update(secrets.references(item))
            override = (item.get("extensions") or {}).get("auth_override")
            if isinstance(override, dict) and override.get("account_id"):
                referenced_accounts.add(str(override["account_id"]))
            (ui_cases if item["kind"] == "ui" else api_cases).append(item)

        _collect_env_references(cfg, include_environments, referenced_accounts, referenced_platforms)
        req_file = folder / "analysis" / "requirements.json"
        requirements = store.read(req_file) if req_file.is_file() else []

        prefix = f"projects/{project_id}"
        entries[f"{prefix}/project.json"] = _write_archive_json(archive, f"{prefix}/project.json", meta)
        entries[f"{prefix}/qa-config.json"] = _write_archive_json(archive, f"{prefix}/qa-config.json", cfg)
        entries[f"{prefix}/auth.json"] = _write_archive_json(archive, f"{prefix}/auth.json", auth)
        entries[f"{prefix}/analysis/requirements.json"] = _write_archive_json(archive, f"{prefix}/analysis/requirements.json", requirements)
        entries[f"{prefix}/testcases/functional-cases.json"] = _write_archive_json(archive, f"{prefix}/testcases/functional-cases.json", ui_cases)
        entries[f"{prefix}/testcases/api-cases.json"] = _write_archive_json(archive, f"{prefix}/testcases/api-cases.json", api_cases)
        for name, file in documents:
            knowledge.relative_path(name)
            member = f"{prefix}/knowledge/{name}"
            entries[member] = _archive_file(archive, file, member)

        return {
            "id": project_id,
            "name": info["name"],
            "category": info["category"],
            "cases": {"total": len(closure_ids), "ui": len(ui_cases), "api": len(api_cases)},
            "selected_case_ids": eligible,
            "included_case_ids": closure_ids,
            "excluded_case_ids": excluded,
        }


def _resolve_sync_case_ids(chosen: list[str], case_ids: dict[str, list[str]] | list[str] | None) -> dict[str, list[str] | None]:
    if case_ids is None:
        return dict.fromkeys(chosen)
    if isinstance(case_ids, list):
        if len(chosen) != 1:
            raise ValueError("指定多个项目导出时，case_ids 需要按项目分组")
        return {chosen[0]: list(case_ids)}
    if isinstance(case_ids, dict):
        return {pid: list(case_ids[pid]) if pid in case_ids and case_ids[pid] is not None else None for pid in chosen}
    raise ValueError("case_ids 格式无效")


def pack_sync(
    project_ids: list[str] | None = None,
    *,
    case_ids: dict[str, list[str]] | list[str] | None = None,
    out: str | None = None,
    include_environments: bool = True,
    include_accounts: bool = True,
    only_passing: bool = True,
) -> dict:
    """Export one self-contained format-3 sync ZIP with passing cases, closures, envs and plaintext accounts."""
    from . import sync_conflicts

    chosen = list(dict.fromkeys(project_ids or [p["id"] for p in projects.list_all() if not p.get("broken")]))
    if not chosen:
        raise ValueError("还没有可以导出的项目")
    per_project_cases = _resolve_sync_case_ids(chosen, case_ids)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")  # noqa: DTZ005  # Sync filenames use the machine's local wall clock.
    name_scope = chosen[0] if len(chosen) == 1 else "multi"
    filename = f"autoskill-sync-{name_scope}-{stamp}-{uuid.uuid4().hex[:4]}.zip"
    if out:
        target = Path(out)
        if target.suffix.lower() != ".zip":
            target = target / filename
    else:
        target = paths.local() / "sync" / "exports" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = target.resolve()

    listed: list[dict] = []
    entries: dict[str, dict] = {}
    needed_secrets: set[str] = set()
    ref_accounts: set[str] = set()
    ref_platforms: set[str] = set()
    included_platform_accounts = False

    with _pack_output(destination) as output:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for pid in chosen:
                project_summary = _sync_project_entry(
                    archive,
                    pid,
                    per_project_cases.get(pid),
                    include_environments=include_environments,
                    only_passing=only_passing,
                    entries=entries,
                    needed_secrets=needed_secrets,
                    referenced_accounts=ref_accounts,
                    referenced_platforms=ref_platforms,
                    target=destination,
                )
                if project_summary is not None:
                    listed.append(project_summary)
            if not listed:
                raise ValueError("没有可同步的通过状态用例")

            if include_accounts and (ref_accounts or ref_platforms):
                store_data = sync_conflicts.load_platform_store()
                exported_accounts = {aid: copy.deepcopy(store_data["accounts"][aid])
                                     for aid in sorted(ref_accounts) if aid in store_data["accounts"]}
                for acc in exported_accounts.values():
                    if isinstance(acc, dict) and acc.get("platform_id"):
                        ref_platforms.add(str(acc["platform_id"]))
                exported_platforms = {pid: copy.deepcopy(store_data["platforms"][pid])
                                      for pid in sorted(ref_platforms) if pid in store_data["platforms"]}
                if exported_platforms or exported_accounts:
                    payload = {"schema": 1, "platforms": exported_platforms, "accounts": exported_accounts}
                    entries[PLATFORM_ACCOUNTS] = _write_archive_json(archive, PLATFORM_ACCOUNTS, payload)
                    included_platform_accounts = True

            manifest = {
                "format": 3,
                "pack_type": "sync",
                "status": "pass",
                "autoskill": __version__,
                "created_at": db.now(),
                "projects": listed,
                "files": entries,
                "includes_environments": include_environments,
                "includes_platform_accounts": included_platform_accounts,
                "secrets_needed": sorted(needed_secrets),
                "secrets_included": [],
                "history_origin": "imported",
                "includes_local_verification": False,
            }
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        size = output.tell()

    total_cases = sum(item["cases"]["total"] for item in listed)
    return {
        "file": str(target),
        "filename": target.name,
        "bytes": size,
        "projects": listed,
        "case_count": total_cases,
        "includes_environments": include_environments,
        "includes_platform_accounts": included_platform_accounts,
        "secrets_needed": sorted(needed_secrets),
    }


# --- import ---
def _safe_members(archive: zipfile.ZipFile) -> dict[str, list[zipfile.ZipInfo]]:
    """Group pack entries by project, refusing anything that could escape the project folder."""
    from . import knowledge

    grouped: dict[str, list[zipfile.ZipInfo]] = {}
    seen, knowledge_paths = set(), set()
    for member in archive.infolist():
        name = member.filename
        if name in seen:
            raise ValueError(f"分享包里有重复路径，已拒绝导入：{name[:80]}")
        seen.add(name)
        if member.is_dir() or name in (MANIFEST, SECRETS, PLATFORM_ACCOUNTS):
            continue
        parts = name.split("/")
        if (len(parts) < 3 or parts[0] != "projects" or not projects.ID.fullmatch(parts[1]) or "\\" in name
                or any(part in ("", ".", "..") or ":" in part for part in parts)):
            raise ValueError(f"分享包里有不安全的路径，已拒绝导入：{name[:80]}")
        if parts[2] == "knowledge":
            knowledge.relative_path("/".join(parts[3:]))
            identity = name.casefold()
            if identity in knowledge_paths:
                raise ValueError(f"分享包里有重复知识路径：{name[:80]}")
            knowledge_paths.add(identity)
        grouped.setdefault(parts[1], []).append(member)
    return grouped


def _verify_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, expected: dict) -> None:
    if (not isinstance(expected, dict) or not isinstance(expected.get("sha256"), str)
            or not domain.HASH.fullmatch(expected["sha256"]) or expected.get("size") != member.file_size):
        raise ValueError(f"分享包文件清单无效：{member.filename}")
    digest = hashlib.sha256()
    with archive.open(member) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected["sha256"]:
        raise ValueError(f"分享包文件哈希校验失败：{member.filename}")


def _manifest_errors(archive: zipfile.ZipFile, manifest: dict, grouped: dict) -> dict[str, str]:
    """Validate modern packs before touching project files or restoring credentials."""
    version = manifest.get("format", 1)
    if version == 1:
        return {}
    if version not in (2, 3) or not isinstance(manifest.get("files"), dict):
        raise ValueError("分享包文件清单格式不受支持")
    expected = manifest["files"]
    actual = {member.filename for members in grouped.values() for member in members}
    has_platform = version == 3 and PLATFORM_ACCOUNTS in archive.namelist()
    if has_platform:
        actual.add(PLATFORM_ACCOUNTS)
    errors = {}
    for name in set(expected) | actual:
        if version == 3 and name == PLATFORM_ACCOUNTS:
            if name not in expected or name not in actual:
                errors["_platform_accounts"] = f"分享包文件清单与内容不一致：{name}"
            continue
        parts = name.split("/")
        if len(parts) < 3 or parts[0] != "projects" or not projects.ID.fullmatch(parts[1]):
            raise ValueError("分享包文件清单包含无效路径")
        if name not in expected or name not in actual:
            errors[parts[1]] = f"分享包文件清单与内容不一致：{name}"
    if has_platform and "_platform_accounts" not in errors:
        try:
            _verify_member(archive, archive.getinfo(PLATFORM_ACCOUNTS), expected[PLATFORM_ACCOUNTS])
        except (ValueError, KeyError, zipfile.BadZipFile) as error:
            errors["_platform_accounts"] = str(error)
    for project_id, members in grouped.items():
        if project_id in errors:
            continue
        try:
            for member in members:
                _verify_member(archive, member, expected[member.filename])
        except (ValueError, zipfile.BadZipFile) as error:
            errors[project_id] = str(error)
    return errors


def _restore_pack_secrets(archive: zipfile.ZipFile, passphrase: str | None) -> list[str]:
    """Restore the same valid credential entries while the archive is open."""
    restored = []
    if SECRETS in archive.namelist() and passphrase:
        values = json.loads(decrypt(json.loads(archive.read(SECRETS).decode("utf-8")), passphrase).decode("utf-8"))
        if not isinstance(values, dict):
            raise ValueError("分享包中的凭据必须是对象")
        for name, value in values.items():
            if secrets.NAME.fullmatch(name) and isinstance(value, str):
                secrets.set_secret(name, value)
                restored.append(name)
    return restored


def _validate_import(staging: Path) -> None:
    """Check the complete project before moving its current directory aside."""
    from .core import store

    store.require_valid(staging, check_results=False)
    for name in ("qa-config.json", "auth.json"):
        file = staging / name
        if name == "auth.json" and not file.is_file():
            continue
        if not file.is_file():
            raise ValueError(f"缺少 {name}")
        if not isinstance(store.read(file), dict):
            raise ValueError(f"{name} 必须是对象")
    domain.validate_project(staging)


def _rollback_import(target: Path, backup: Path, installed: bool) -> None:
    """Keep the original backup available if the filesystem also refuses rollback."""
    try:
        if installed:
            shutil.rmtree(target)
        if backup.exists():
            backup.replace(target)
    except OSError as error:
        saved = backup if backup.exists() else target
        raise OSError(f"导入回滚未完成；数据保留在 {saved}，请恢复后重试") from error


def _install_import(project_id: str, staging: Path, backup: Path) -> dict:
    """Replace files reversibly; reindex commits its own transaction only after every row succeeds."""
    target = paths.projects_dir() / project_id
    installed, committed = False, False
    try:
        if target.exists():
            target.replace(backup)
        staging.replace(target)
        installed = True
        info = projects.get(project_id)
        projects.reindex(project_id)
        committed = True
    finally:
        if not committed:
            _rollback_import(target, backup, installed)
    shutil.rmtree(backup, ignore_errors=True)
    return info


def _link_or_copy_report(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


@contextmanager
def _keep_local_reports(target: Path, staging: Path):
    """Preserve this workspace's indexed task files through the reversible project swap."""
    current, incoming = target / "reports", staging / "reports"
    names = {entry.name for folder in (current, incoming) if folder.is_dir() for entry in folder.iterdir()}
    indexes = paths.local() / "task-report-locations"
    with ExitStack() as locks:
        for name in sorted(names):
            original, replacement = current / name, incoming / name
            if not (indexes / (name + ".json")).is_file() or report_locations.folder(name) != original:
                continue
            # Other exports and run deletion already hold the project lock; workflows render separately.
            if name.startswith("workflow_"):
                locks.enter_context(named_lock("test-report", name))
            if replacement.is_dir():
                shutil.rmtree(replacement)
            else:
                replacement.unlink(missing_ok=True)
            if original.is_symlink() or original.is_file():
                incoming.mkdir(exist_ok=True)
                shutil.copy2(original, replacement, follow_symlinks=False)
            elif original.is_dir():
                incoming.mkdir(exist_ok=True)
                shutil.copytree(original, replacement, symlinks=True, copy_function=_link_or_copy_report)
        yield


def _import_project(archive: zipfile.ZipFile, project_id: str, members: list[zipfile.ZipInfo], overwrite: bool) -> dict | None:
    """Use a private staging directory so failures cannot remove another import's files."""
    import tempfile

    target = paths.projects_dir() / project_id
    with project_lock(project_id):
        if target.exists() and not overwrite:
            return None
    folder = paths.local() / "import"
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{project_id}-", dir=folder))
    staging, backup = work / "incoming", work / "previous"
    try:
        for member in members:
            destination = staging / Path(*member.filename.split("/")[2:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            mode = (member.external_attr >> 16) & 0o777
            if mode:
                destination.chmod(mode)
        _validate_import(staging)
        (staging / "knowledge").mkdir(exist_ok=True)
        with project_lock(project_id):
            if target.exists() and not overwrite:
                return None
            with _keep_local_reports(target, staging):
                return _install_import(project_id, staging, backup)
    finally:
        if not backup.exists():
            shutil.rmtree(work, ignore_errors=True)


def _restore_pack_platform_accounts(archive: zipfile.ZipFile, manifest: dict, overwrite: bool) -> None:
    if manifest.get("format") != 3 or PLATFORM_ACCOUNTS not in archive.namelist():
        return
    from . import sync_conflicts

    platform_data = json.loads(archive.read(PLATFORM_ACCOUNTS).decode("utf-8"))
    if not isinstance(platform_data, dict):
        return
    local_store = sync_conflicts.load_platform_store()
    for pid, plat in (platform_data.get("platforms") or {}).items():
        if isinstance(plat, dict) and (overwrite or pid not in local_store["platforms"]):
            sync_conflicts.save_platform_entry(pid, plat)
    for aid, acc in (platform_data.get("accounts") or {}).items():
        if isinstance(acc, dict) and (overwrite or aid not in local_store["accounts"]):
            sync_conflicts.save_account_entry(aid, acc)


def import_pack(file: str, passphrase: str | None = None, overwrite: bool = False) -> dict:
    """Import projects from a pack. Existing projects are kept unless `overwrite` is set."""
    import sqlite3

    source = Path(file)
    if not source.is_file():
        raise ValueError(f"找不到分享包：{file}")
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise ValueError("这不是 autoSkill 分享包") from None
    with archive:
        if MANIFEST not in archive.namelist():
            raise ValueError("这不是 autoSkill 分享包")
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("分享包清单必须是对象")
        grouped = _safe_members(archive)
        errors = _manifest_errors(archive, manifest, grouped)
        if errors:
            failed = [{"id": project_id, "reason": reason} for project_id, reason in sorted(errors.items())]
            return {"imported": [], "skipped": failed, "errors": failed,
                    "secrets_restored": [], "missing_secrets": [], "next": "分享包未通过完整性校验，请重新导出后重试"}
        restored = _restore_pack_secrets(archive, passphrase)
        _restore_pack_platform_accounts(archive, manifest, overwrite)
        imported, skipped, failed = [], [], []
        for project_id, members in grouped.items():
            try:
                info = _import_project(archive, project_id, members, overwrite)
                if info is None:
                    skipped.append({"id": project_id, "reason": "本机已有同名项目；加 --overwrite 用分享包里的版本替换"})
                else:
                    imported.append(info)
            except (ValueError, KeyError, OSError, TypeError, AttributeError, sqlite3.Error) as error:
                issue = {"id": project_id, "reason": "项目导入失败，" + str(error)}
                failed.append(issue)
                skipped.append(issue)
    missing = sorted({name for info in imported for name in info["missing_secrets"]})
    hint = ""
    if missing:
        hint = ("分享包里带有加密的凭据：加上 --passphrase 重新导入即可恢复" if manifest.get("secrets_included") and not passphrase
                else "还需要设置凭据：" + "；".join(f"autoskill secret set {name}" for name in missing))
    if failed:
        hint = "请处理失败项目的错误后重试。" + hint
    elif skipped and not imported:
        hint = "已保留本机同名项目。如需替换，请明确选择覆盖后重新导入。"
    return {"imported": imported, "skipped": skipped, "errors": failed,
            "secrets_restored": sorted(restored), "missing_secrets": missing, "next": hint or "可以直接运行：autoskill run <项目>"}
