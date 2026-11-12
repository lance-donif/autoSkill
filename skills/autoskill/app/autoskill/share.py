"""Share projects: pack what you maintain into one file, import what someone gave you.

A pack carries cases, knowledge and login definitions. It never carries run
history, saved sessions or evidence, and carries secret values only when asked,
encrypted with a passphrase.
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

from . import __version__, db, paths, projects, secrets

MANIFEST = "autoskill-pack.json"
SECRETS = "secrets.sealed.json"
SKIP_DIRS = {"versions", "execution", "output", "staging", "input", "scripts", "postman", "__pycache__"}
ALLOWED_SUFFIXES = {".json", ".md", ".txt", ".csv"}
MAX_FILE = 5 * 1024 * 1024
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
        if file.is_file() and not (set(relative.parts[:-1]) & SKIP_DIRS) and file.suffix.lower() in ALLOWED_SUFFIXES:
            yield relative, file


def pack(project_ids: list[str] | None = None, out: str | None = None, passphrase: str | None = None) -> dict:
    """Write a share pack. With a passphrase, the secrets the cases refer to travel along, encrypted."""
    chosen = project_ids or [p["id"] for p in projects.list_all() if not p.get("broken")]
    if not chosen:
        raise ValueError("还没有可以分享的项目")
    if passphrase is not None and len(passphrase) < 8:
        raise ValueError("口令至少 8 个字符")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    target = Path(out) if out else paths.workspace() / f"autoskill-share-{stamp}.zip"
    if target.suffix.lower() != ".zip":  # a folder, existing or not
        target = target / f"autoskill-share-{stamp}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    listed, needed = [], set()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for project_id in chosen:
            folder, info = projects.path(project_id), projects.get(project_id)
            for relative, file in _files(folder):
                archive.write(file, f"projects/{project_id}/{relative.as_posix()}")
            for case in projects.cases(project_id):
                needed |= secrets.references(case)
            listed.append({"id": project_id, "name": info["name"], "category": info["category"], "cases": info["cases"]})
        included = []
        if passphrase is not None:
            values = {name: secrets.get_secret(name) for name in sorted(needed) if secrets.get_secret(name) is not None}
            included = sorted(values)
            archive.writestr(SECRETS, json.dumps(encrypt(json.dumps(values, ensure_ascii=False).encode("utf-8"), passphrase)))
        manifest = {"format": 1, "autoskill": __version__, "created_at": db.now(), "projects": listed,
                    "secrets_needed": sorted(needed), "secrets_included": included}
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    missing = sorted(needed - set(included))
    return {"file": str(target), "bytes": target.stat().st_size, "projects": listed,
            "secrets_included": included, "secrets_not_included": missing,
            "note": ("凭据已用口令加密随包带走；把口令通过另一个渠道告诉对方" if included else
                     "分享包不含任何凭据、登录态和运行记录" + ("；对方首次使用时需要设置：" + "、".join(missing) if missing else ""))}


def pack_full(out: str | None = None) -> dict:
    """Zip the whole skill (runtime, projects, knowledge) so the receiver only has to unzip it.

    Leaves out workspace/.local: secrets, saved logins, run history and screenshots stay on this machine.
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    target = Path(out) if out else paths.SKILL.parent / f"autoskill-full-{stamp}.zip"
    if target.suffix.lower() != ".zip":  # a folder, existing or not
        target = target / f"autoskill-full-{stamp}.zip"
    if target.resolve().is_relative_to(paths.SKILL.resolve()):
        raise ValueError("完整包不能输出到 Skill 目录内部，请用 --out 指定其他位置")
    target.parent.mkdir(parents=True, exist_ok=True)
    inside = (paths.SKILL / "workspace").resolve()
    count = 0
    listed = [{"id": p["id"], "name": p["name"], "cases": p["cases"]} for p in projects.list_all() if not p.get("broken")]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(paths.SKILL.rglob("*")):
            if not file.is_file() or "__pycache__" in file.parts or file.suffix in (".pyc", ".pyo"):
                continue
            if file.resolve().is_relative_to(inside):
                continue  # the workspace is added below, from wherever it really is, without its private part
            archive.write(file, (Path("autoskill") / file.relative_to(paths.SKILL)).as_posix())
            count += 1
        for project in listed:
            for relative, file in _files(projects.path(project["id"])):
                archive.write(file, f"autoskill/workspace/projects/{project['id']}/{relative.as_posix()}")
                count += 1
    needed = sorted({name for p in listed for case in projects.cases(p["id"]) for name in secrets.references(case)})
    return {"file": str(target), "bytes": target.stat().st_size, "files": count, "projects": listed,
            "includes_runtime": paths.VENDOR.is_dir(), "secrets_not_included": needed,
            "note": ("对方解压后即可使用，不需要安装任何东西。" if paths.VENDOR.is_dir() else
                     "这份 Skill 目录里没有随包运行时（不是从完整离线包运行的），对方需要自备运行环境。")
                    + "凭据、登录态、运行记录没有打包"
                    + ("；对方首次使用时需要设置：" + "、".join(needed) if needed else "")}


# --- import ---
def _safe_members(archive: zipfile.ZipFile) -> dict[str, list[zipfile.ZipInfo]]:
    """Group pack entries by project, refusing anything that could escape the project folder."""
    grouped: dict[str, list[zipfile.ZipInfo]] = {}
    for member in archive.infolist():
        name = member.filename
        if member.is_dir() or name in (MANIFEST, SECRETS):
            continue
        parts = name.split("/")
        if (len(parts) < 3 or parts[0] != "projects" or not projects.ID.fullmatch(parts[1]) or "\\" in name
                or any(part in ("", ".", "..") or ":" in part for part in parts)):
            raise ValueError(f"分享包里有不安全的路径，已拒绝导入：{name[:80]}")
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES or member.file_size > MAX_FILE:
            raise ValueError(f"分享包里有不允许的文件，已拒绝导入：{name[:80]}")
        grouped.setdefault(parts[1], []).append(member)
    return grouped


def import_pack(file: str, passphrase: str | None = None, overwrite: bool = False) -> dict:
    """Import projects from a pack. Existing projects are kept unless `overwrite` is set."""
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
        grouped = _safe_members(archive)
        imported, skipped = [], []
        for project_id, members in grouped.items():
            target = paths.projects_dir() / project_id
            if target.exists() and not overwrite:
                skipped.append({"id": project_id, "reason": "本机已有同名项目；加 --overwrite 用分享包里的版本替换"})
                continue
            staging = paths.local() / "import" / project_id
            if staging.exists():
                import shutil

                shutil.rmtree(staging)
            for member in members:
                destination = staging / Path(*member.filename.split("/")[2:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
            import shutil

            try:
                from .core import store

                if not (staging / "project.json").is_file():
                    raise ValueError("缺少 project.json")
                for sub in ("analysis", "testcases", "knowledge"):
                    (staging / sub).mkdir(exist_ok=True)
                store.require_valid(staging, check_results=False)  # cases must pass the same checks as local ones
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(str(staging), str(target))
                projects.reindex(project_id)
                imported.append(project_id)
            except (ValueError, KeyError, OSError) as error:
                shutil.rmtree(staging, ignore_errors=True)
                skipped.append({"id": project_id, "reason": "内容没有通过校验：" + str(error)[:200]})
        restored = []
        if SECRETS in archive.namelist() and passphrase:
            values = json.loads(decrypt(json.loads(archive.read(SECRETS).decode("utf-8")), passphrase).decode("utf-8"))
            for name, value in values.items():
                if secrets.NAME.fullmatch(name) and isinstance(value, str):
                    secrets.set_secret(name, value)
                    restored.append(name)
    missing = sorted({name for pid in imported for name in projects.missing_secrets(pid)})
    hint = ""
    if missing:
        hint = ("分享包里带有加密的凭据：加上 --passphrase 重新导入即可恢复" if manifest.get("secrets_included") and not passphrase
                else "还需要设置凭据：" + "；".join(f"autoskill secret set {name}" for name in missing))
    return {"imported": [projects.get(pid) for pid in imported], "skipped": skipped,
            "secrets_restored": sorted(restored), "missing_secrets": missing, "next": hint or "可以直接运行：autoskill run <项目>"}
