"""Share projects: pack what you maintain into one file, import what someone gave you.

Project packs carry cases, knowledge and login definitions, with optional
passphrase-encrypted credentials. Full skill packs intentionally carry portable
credentials and saved logins. Neither kind carries run history or evidence.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import zipfile
from pathlib import Path

from . import __version__, db, domain, paths, projects, secrets
from .locking import project_lock

MANIFEST = "autoskill-pack.json"
SECRETS = "secrets.sealed.json"
SKIP_DIRS = {"execution", "output", "staging", "__pycache__"}
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
def _files(folder: Path):
    for file in sorted(folder.rglob("*")):
        relative = file.relative_to(folder)
        if file.is_file() and relative.parts[0] not in SKIP_DIRS:
            if not file.resolve().is_relative_to(folder.resolve()):
                raise ValueError(f"项目附件指向项目目录以外：{relative.as_posix()}")
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


def _archive_project(archive: zipfile.ZipFile, project_id: str, entries: dict, needed: set) -> dict:
    with project_lock(project_id):
        folder, info = projects.path(project_id), projects.get(project_id)
        domain.validate_project(folder)
        for relative, file in _files(folder):
            if file.resolve() == Path(archive.filename).resolve():
                continue
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
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        listed.extend(_archive_project(archive, project_id, entries, needed) for project_id in dict.fromkeys(chosen))
        included = []
        if passphrase is not None:
            values = {name: secrets.get_secret(name) for name in sorted(needed) if secrets.get_secret(name) is not None}
            included = sorted(values)
            archive.writestr(SECRETS, json.dumps(encrypt(json.dumps(values, ensure_ascii=False).encode("utf-8"), passphrase)))
        manifest = {"format": 2, "autoskill": __version__, "created_at": db.now(), "projects": listed, "files": entries,
                    "secrets_needed": sorted(needed), "secrets_included": included,
                    "history_origin": "imported", "includes_local_verification": False}
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    missing = sorted(needed - set(included))
    return {"file": str(target), "bytes": target.stat().st_size, "projects": listed,
            "secrets_included": included, "secrets_not_included": missing,
            "note": ("凭据已用口令加密随包带走；把口令通过另一个渠道告诉对方" if included else
                     "分享包不含任何凭据、登录态和运行记录" + ("；对方首次使用时需要设置：" + "、".join(missing) if missing else ""))}


def pack_full(out: str | None = None) -> dict:
    """Zip the whole skill (runtime, projects, knowledge) so the receiver only has to unzip it.

    Include portable credentials and saved logins, but leave run history, evidence and process files behind.
    """
    from . import sessions
    from .locking import named_lock

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")  # noqa: DTZ005  # Pack filenames use the machine's local wall clock.
    target = Path(out) if out else paths.SKILL.parent / f"autoskill-full-{stamp}.zip"
    if target.suffix.lower() != ".zip":  # a folder, existing or not
        target = target / f"autoskill-full-{stamp}.zip"
    if target.resolve().is_relative_to(paths.SKILL.resolve()):
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
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(paths.SKILL.rglob("*")):
            if not file.is_file() or "__pycache__" in file.parts or file.suffix in (".pyc", ".pyo"):
                continue
            if any(file.resolve().is_relative_to(folder) for folder in excluded):
                continue  # actual data is added below at the standard unpacked workspace location
            archive.write(file, (Path("autoskill") / file.relative_to(paths.SKILL)).as_posix())
            count += 1
        for project in listed:
            with project_lock(project["id"]):
                folder = projects.path(project["id"])
                domain.validate_project(folder)
                for relative, file in _files(folder):
                    archive.write(file, f"autoskill/workspace/projects/{project['id']}/{relative.as_posix()}")
                    count += 1
        archive.writestr("autoskill/workspace/.local/credentials.json", json.dumps(values, ensure_ascii=False, indent=2) + "\n")
        count += 1
        for project_id, state in saved.items():
            archive.writestr(f"autoskill/workspace/.local/sessions/{project_id}.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            count += 1
    missing = sorted(needed - set(values))
    return {"file": str(target), "bytes": target.stat().st_size, "files": count, "projects": listed,
            "includes_runtime": paths.VENDOR.is_dir(), "secrets_included": sorted(values), "sessions_included": sorted(saved), "secrets_not_included": missing,
            "note": ("对方解压后即可使用，不需要安装任何东西。" if paths.VENDOR.is_dir() else
                     "这份 Skill 目录里没有随包运行时（不是从完整离线包运行的），对方需要自备运行环境。")
                    + "完整包包含已保存的凭据和登录态；不包含运行历史、截图和临时进程文件。"
                    + ("仍需设置缺少的凭据：" + "、".join(missing) if missing else "")}


# --- import ---
def _safe_members(archive: zipfile.ZipFile) -> dict[str, list[zipfile.ZipInfo]]:
    """Group pack entries by project, refusing anything that could escape the project folder."""
    grouped: dict[str, list[zipfile.ZipInfo]] = {}
    seen = set()
    for member in archive.infolist():
        name = member.filename
        if name in seen:
            raise ValueError(f"分享包里有重复路径，已拒绝导入：{name[:80]}")
        seen.add(name)
        if member.is_dir() or name in (MANIFEST, SECRETS):
            continue
        parts = name.split("/")
        if (len(parts) < 3 or parts[0] != "projects" or not projects.ID.fullmatch(parts[1]) or "\\" in name
                or any(part in ("", ".", "..") or ":" in part for part in parts)):
            raise ValueError(f"分享包里有不安全的路径，已拒绝导入：{name[:80]}")
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
    if version != 2 or not isinstance(manifest.get("files"), dict):
        raise ValueError("分享包文件清单格式不受支持")
    expected = manifest["files"]
    actual = {member.filename for members in grouped.values() for member in members}
    errors = {}
    for name in set(expected) | actual:
        parts = name.split("/")
        if len(parts) < 3 or parts[0] != "projects" or not projects.ID.fullmatch(parts[1]):
            raise ValueError("分享包文件清单包含无效路径")
        if name not in expected or name not in actual:
            errors[parts[1]] = f"分享包文件清单与内容不一致：{name}"
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
    import shutil

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
    import shutil

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


def _import_project(archive: zipfile.ZipFile, project_id: str, members: list[zipfile.ZipInfo], overwrite: bool) -> dict | None:
    """Use a private staging directory so failures cannot remove another import's files."""
    import shutil
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
            return _install_import(project_id, staging, backup)
    finally:
        if not backup.exists():
            shutil.rmtree(work, ignore_errors=True)


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
            return {"imported": [], "skipped": [{"id": project_id, "reason": reason} for project_id, reason in sorted(errors.items())],
                    "secrets_restored": [], "missing_secrets": [], "next": "分享包未通过完整性校验，请重新导出后重试"}
        restored = _restore_pack_secrets(archive, passphrase)
        imported, skipped = [], []
        for project_id, members in grouped.items():
            try:
                info = _import_project(archive, project_id, members, overwrite)
                if info is None:
                    skipped.append({"id": project_id, "reason": "本机已有同名项目；加 --overwrite 用分享包里的版本替换"})
                else:
                    imported.append(info)
            except (ValueError, KeyError, OSError, TypeError, AttributeError, sqlite3.Error) as error:
                skipped.append({"id": project_id, "reason": "内容没有通过校验：" + str(error)[:200]})
    missing = sorted({name for info in imported for name in info["missing_secrets"]})
    hint = ""
    if missing:
        hint = ("分享包里带有加密的凭据：加上 --passphrase 重新导入即可恢复" if manifest.get("secrets_included") and not passphrase
                else "还需要设置凭据：" + "；".join(f"autoskill secret set {name}" for name in missing))
    return {"imported": imported, "skipped": skipped,
            "secrets_restored": sorted(restored), "missing_secrets": missing, "next": hint or "可以直接运行：autoskill run <项目>"}
