"""Complete data snapshots preserve ordinary files and verify database and archive inventories."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

TRANSIENT = (".local/locks", ".local/bridge-io", ".local/agent-io", ".local/browser-control")
MANIFEST = "data-manifest.json"


def write_json(file: Path, value) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(file)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(file: Path, default=None):
    try:
        return json.loads(file.read_bytes())
    except FileNotFoundError:
        return {} if default is None else default


def digest(file: Path) -> str:
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _transient(relative: str) -> bool:
    return any(relative == prefix or relative.startswith(prefix + "/") for prefix in TRANSIENT)


def inventory(root: Path, *, directories: list | None = None) -> tuple[dict, list[dict]]:
    records, omitted = {}, []

    def visit(folder: Path, prefix: str, parents: set[Path]) -> None:
        resolved = folder.resolve()
        if resolved in parents:
            raise ValueError("数据目录存在循环目录链接，无法生成完整副本：" + prefix)
        ancestors = {*parents, resolved}
        for entry in sorted(folder.iterdir()):
            relative = prefix + entry.name
            if _transient(relative):
                omitted.append({"path": relative, "reason": "临时传输或本机进程控制目录，重新启动时重建"})
            elif entry.is_dir():
                if directories is not None:
                    directories.append(relative)
                visit(entry, relative + "/", ancestors)
            elif entry.is_file():
                info = entry.stat()
                records[relative] = {"size": info.st_size, "sha256": digest(entry)}
            elif entry.is_symlink():
                raise ValueError("数据目录存在无法读取的链接：" + relative)
            else:
                raise ValueError("数据目录包含无法作为普通文件保存的对象：" + relative)

    if root.exists():
        visit(root, "", set())
    return records, omitted


def sqlite_files(root: Path, files: dict) -> set[str]:
    found = set()
    for name, record in files.items():
        if name == ".local/autoskill.db" and record["size"] >= 16:
            with (root / name).open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    found.add(name)
    return found


def snapshot(source: Path, destination: Path) -> dict:
    """Use SQLite backup and reject a changing source instead of certifying mixed revisions."""
    before_directories = []
    before, omitted = inventory(source, directories=before_directories)
    databases = sqlite_files(source, before)
    sidecars = {name + suffix for name in databases for suffix in ("-wal", "-shm", "-journal")}
    modes = {name: stat.S_IMODE((source / name).stat().st_mode) for name in [*before_directories, *before] if name not in sidecars}
    destination.mkdir(parents=True, exist_ok=True)
    for name in before_directories:
        (destination / name).mkdir(parents=True, exist_ok=True)
    for name in before:
        if name in databases or name in sidecars:
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    for name in databases:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect((source / name).as_uri() + "?mode=ro", uri=True)) as original, \
                contextlib.closing(sqlite3.connect(target)) as copied:
            original.backup(copied)
            if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite 数据快照校验失败：" + name)
        shutil.copystat(source / name, target)
    after_directories = []
    after, _after_omitted = inventory(source, directories=after_directories)
    # SQLite readers update shared-memory locks; those bytes are not persistent database content.
    shared_memory = {name + "-shm" for name in databases}
    if ({name: value for name, value in before.items() if name not in shared_memory} !=
            {name: value for name, value in after.items() if name not in shared_memory} or before_directories != after_directories):
        raise ValueError("复制期间源数据发生变化，未发布副本；请结束活动任务后重试")
    saved, _unused = inventory(destination)
    for name, record in before.items():
        if name not in databases and name not in sidecars and saved.get(name) != record:
            raise ValueError("数据文件复制后校验失败：" + name)
    _restore_modes(destination, modes)
    return {"files": saved, "directories": before_directories, "modes": modes, "sqlite": sorted(databases), "omitted_runtime": omitted}


def relative_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("数据包文件路径必须是字符串")
    path = PurePosixPath(value)
    if not value or "\0" in value or path.is_absolute() or PureWindowsPath(value).drive or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("数据包文件路径无效：" + value)
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(number) for number in range(1, 10)), *("LPT" + str(number) for number in range(1, 10))}
        for part in path.parts:
            if (any(character in '<>:"\\|?*' or ord(character) < 32 for character in part) or part.endswith((" ", ".")) or
                    part.split(".", 1)[0].upper() in reserved):
                raise ValueError("数据包文件名无法在当前 Windows 文件系统表示，未导入：" + value)
    return path.as_posix()


def _archive_entries(package: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries, seen = {}, set()
    for info in package.infolist():
        name = relative_name(info.filename.removesuffix("/") if info.is_dir() else info.filename)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not info.is_dir()):
            raise ValueError("数据包不能用链接或特殊对象冒充文件：" + name)
        key = os.path.normcase(name)
        if key in seen:
            raise ValueError("数据包包含重复或在本机冲突的路径：" + name)
        seen.add(key)
        entries[name] = info
    files = {os.path.normcase(name) for name, info in entries.items() if not info.is_dir()}
    for name in entries:
        if any(os.path.normcase(parent.as_posix()) in files for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")):
            raise ValueError("数据包的文件与目录路径冲突：" + name)
    return entries


def _archive_layout(package: zipfile.ZipFile, entries: dict) -> tuple[str, dict | None]:
    if MANIFEST in entries:
        manifest = json.loads(package.read(MANIFEST))
        if not isinstance(manifest, dict) or manifest.get("format") != "autoskill-data" or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("数据包清单格式无效")
        if not isinstance(manifest.get("directories", []), list) or not isinstance(manifest.get("modes", {}), dict):
            raise ValueError("数据包目录或权限清单格式无效")
        return "data/", manifest
    roots = [name for name in entries if name == "data-root.json" or (name.endswith("/data-root.json") and len(PurePosixPath(name).parts) == 2)]
    if len(roots) != 1:
        raise ValueError("普通 ZIP 必须包含一个完整 data-root.json 数据根，可平铺或放在一个顶层目录中")
    metadata = json.loads(package.read(entries[roots[0]]))
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1 or not isinstance(metadata.get("data_id"), str) or not metadata["data_id"]:
        raise ValueError("普通 ZIP 的数据根标识无效")
    return roots[0].removesuffix("data-root.json"), None


def _archive_contents(entries: dict, prefix: str, manifest: dict | None) -> tuple[dict, set[str], dict]:
    files, directories, modes = {}, set(), {}
    for name, info in entries.items():
        if (name == prefix.removesuffix("/") and info.is_dir()) or (manifest is not None and name == MANIFEST):
            continue
        if not name.startswith(prefix):
            raise ValueError("数据包含数据根之外的内容，未静默丢弃：" + name)
        relative = relative_name(name.removeprefix(prefix))
        if info.is_dir():
            directories.add(relative)
        else:
            files[relative] = info
        directories.update(parent.as_posix() for parent in PurePosixPath(relative).parents if parent != PurePosixPath("."))
        if info.create_system == 3 and info.external_attr >> 16:
            modes[relative] = stat.S_IMODE(info.external_attr >> 16)
    if manifest is not None:
        expected = {relative_name(name) for name in manifest["files"]}
        declared = {relative_name(name) for name in manifest.get("directories", [])}
        parents = {parent.as_posix() for name in expected for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")}
        if expected != set(files) or not directories.issubset(declared | parents):
            raise ValueError("数据包内容与完整清单不一致，未导入任何数据")
        directories.update(declared)
        allowed_paths = expected | directories
        for name, mode in manifest.get("modes", {}).items():
            if relative_name(name) not in allowed_paths or type(mode) is not int or not 0 <= mode <= 0o7777 or (name in modes and modes[name] != mode):
                raise ValueError("数据包文件权限清单无效：" + name)
            modes[name] = mode
    if set(files) & directories:
        raise ValueError("数据包的文件与目录清单冲突")
    return files, directories, modes


def _restore_modes(destination: Path, modes: dict) -> None:
    for name in sorted(modes, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        (destination / name).chmod(modes[name])


def extract(archive: Path, destination: Path) -> dict:
    with zipfile.ZipFile(archive) as package:
        entries = _archive_entries(package)
        prefix, manifest = _archive_layout(package, entries)
        files, directories, modes = _archive_contents(entries, prefix, manifest)
        for name in sorted(directories):
            (destination / name).mkdir(parents=True, exist_ok=True)
        records = {}
        for name, info in files.items():
            file = destination / name
            file.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as incoming, file.open("xb") as target:
                shutil.copyfileobj(incoming, target)
            record = {"size": file.stat().st_size, "sha256": digest(file)}
            if manifest is not None and record != manifest["files"][name]:
                raise ValueError("数据包文件校验失败：" + name)
            records[name] = record
        _restore_modes(destination, modes)
        return manifest or {"format": "autoskill-data", "schema_version": 1, "origin": "folder-zip", "files": records,
                            "directories": sorted(directories), "modes": modes, "sqlite": [], "omitted_runtime": []}


def publish_directory(source: Path, target: Path) -> None:
    """Atomically rename without replacing a concurrently created target, including an empty directory."""
    if os.name == "nt":
        source.rename(target)
        return
    import ctypes

    from .directory_rename import no_replace

    if no_replace is None:
        raise OSError(errno.ENOTSUP, "当前文件系统接口不支持不覆盖的原子目录发布，原目录未修改")
    if no_replace(os.fsencode(source), os.fsencode(target)) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(target))
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree(root: Path) -> None:
    for file in root.rglob("*"):
        if file.is_file():
            with file.open("rb") as stream:
                os.fsync(stream.fileno())
    if os.name != "nt":
        for folder in [*sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True), root]:
            descriptor = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def archive(root: Path, destination: Path, manifest: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("导出目标已存在，请选择新的文件名，原文件未覆盖")
    with tempfile.NamedTemporaryFile(prefix=".autoskill-data-", suffix=".zip", dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    owned = False
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as package:
            package.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            for name in manifest.get("directories", []):
                package.write(root / name, "data/" + name + "/")
            for name in manifest["files"]:
                package.write(root / name, "data/" + name)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # O_EXCL publication also protects against another exporter choosing the same filename.
        with destination.open("xb") as target, temporary.open("rb") as source:
            owned = True
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if owned:
            destination.unlink()
        raise
    finally:
        temporary.unlink(missing_ok=True)
