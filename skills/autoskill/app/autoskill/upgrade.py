"""Standalone offline upgrades with complete workspace backups and recoverable directory replacement.

This module intentionally uses only the standard library. The bundle also ships
the same file as autoskill-upgrade.py so it can run outside either installation.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import platform
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

FORMAT = 1
MANIFEST = "package-manifest.json"
PROGRAM_ROOTS = {"plugin", "docs", "scripts"}
SKILL_PROGRAM_ROOTS = {"app", "vendor", "references", "examples", "models", "scripts"}


def _hash(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(file: Path) -> dict:
    return json.loads(file.read_text(encoding="utf-8"))


def _write(file: Path, value: dict) -> None:
    temporary = file.with_name("." + file.name + "-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(file)
        if os.name != "nt":
            descriptor = os.open(file.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_tree(path: Path) -> None:
    for file in path.rglob("*"):
        if file.is_file() and not file.is_symlink():
            with file.open("r+b" if os.name == "nt" else "rb") as stream:
                os.fsync(stream.fileno())
    for folder in reversed([path, *(file for file in path.rglob("*") if file.is_dir() and not file.is_symlink())]):
        _sync_directory(folder)


def _rename(source: Path, destination: Path) -> None:
    source.rename(destination)
    _sync_directory(destination.parent)


def _relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts or ":" in path.parts[0]:
        raise ValueError("包内路径无效：" + value)
    return path.as_posix()


def _skill(root: Path) -> Path:
    for relative in ("autoskill", "skills/autoskill"):
        if (root / relative / "SKILL.md").is_file():
            return root / relative
    raise ValueError("没有找到完整 autoSkill 安装目录：" + str(root))


def _runtime(root: Path) -> Path:
    target = "windows-x64" if os.name == "nt" else "linux-arm64" if platform.machine().lower() in ("aarch64", "arm64") else "linux-x64"
    relative = "python/python.exe" if os.name == "nt" else "python/bin/python3"
    file = _skill(root) / "vendor" / target / relative
    if not file.is_file():
        raise ValueError("新包没有匹配本机平台的独立 Python：" + str(file))
    return file


def _package(source: Path) -> dict:
    if source.is_dir():
        config = _json(source / "package.json")
        manifest = _json(source / MANIFEST)
        skill = _skill(source).relative_to(source).as_posix()
    else:
        with zipfile.ZipFile(source) as archive:
            config = json.loads(archive.read("package.json"))
            manifest = json.loads(archive.read(MANIFEST))
            skill = next((name for name in ("autoskill", "skills/autoskill") if name + "/SKILL.md" in archive.namelist()), "")
        if not skill:
            raise ValueError("ZIP 根目录不是完整 autoSkill 离线包")
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), dict):
        raise ValueError("新包缺少受支持的完整性清单，请使用含独立升级器的新离线包")
    for name, digest in manifest["files"].items():
        _relative(name)
        if not isinstance(digest, str) or len(digest) != 64 or any(letter not in "0123456789abcdef" for letter in digest):
            raise ValueError("包内 SHA-256 清单无效")
    if manifest.get("version") != config.get("version"):
        raise ValueError("包版本与完整性清单不一致")
    return {"version": config["version"], "skill": skill, "manifest": manifest,
            "source_sha256": _hash(source) if source.is_file() else _hash(source / MANIFEST)}


def _verify_package(root: Path, overrides: list[str] | None = None, *, complete: bool = False) -> dict:
    package = _package(root)
    if complete:
        actual = {file.relative_to(root).as_posix() for file in root.rglob("*") if file.is_file() and file != root / MANIFEST}
        if actual != set(package["manifest"]["files"]) or not actual:
            raise ValueError("新包完整性清单未覆盖全部文件，或包含不存在的文件")
        if any(file.is_symlink() for file in root.rglob("*")):
            raise ValueError("新包包含符号链接，请使用正式构建的离线包")
    omitted = set(overrides or [])
    for name, expected in package["manifest"]["files"].items():
        if name in omitted:
            continue
        file = root / _relative(name)
        if not file.is_file() or _hash(file) != expected:
            raise ValueError("新包文件校验失败：" + name)
    return package


def _extract(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
        return
    destination.mkdir()
    with zipfile.ZipFile(source) as archive:
        seen = set()
        for entry in archive.infolist():
            name = _relative(entry.filename.rstrip("/"))
            if name in seen:
                raise ValueError("ZIP 包内路径重复：" + name)
            seen.add(name)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("升级 ZIP 包含链接条目，请使用正式构建的离线包")
            target = destination / name
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                mode = (entry.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode)


def _workspaces(target: Path, supplied: list[str] | None) -> list[Path]:
    candidates = [_skill(target) / "workspace"]
    if os.environ.get("AUTOSKILL_WORKSPACE"):
        candidates.append(Path(os.environ["AUTOSKILL_WORKSPACE"]))
    candidates += [Path(value) for value in supplied or []]
    candidates.append(_skill(target).parent / "autoskill-cases")
    if os.environ.get("AUTOSKILL_CASE_SKILL"):
        candidates.append(Path(os.environ["AUTOSKILL_CASE_SKILL"]))
    for candidate in [file.expanduser().resolve() for file in candidates]:
        database = candidate / ".local/autoskill.db"
        if database.is_file():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                for table in ("runs", "tasks"):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if "plan_path" in columns:
                        for row in connection.execute(f"SELECT DISTINCT plan_path FROM {table} WHERE plan_path != ''"):
                            value = Path(row[0]).expanduser()
                            if not value.is_absolute():
                                continue
                            candidates.append(value.parent if value.suffix.lower() in (".xlsx", ".xls") else value)
    result = []
    for file in sorted({file.expanduser().resolve() for file in candidates}, key=lambda item: len(item.parts)):
        if file == target or target.is_relative_to(file):
            raise ValueError("工作区不能是整个安装目录或其上级目录；请指定实际数据目录")
        if not any(file.is_relative_to(parent) for parent in result):
            result.append(file)
    return result


def _alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except (OSError, IndexError):
            return False
    return True


def _blockers(workspaces: list[Path]) -> list[dict]:
    found = []
    for workspace in workspaces:
        for name, kind in (("service.json", "service"), ("queue-worker.json", "queue_worker")):
            service = workspace / ".local" / name
            if service.is_file():
                info = _json(service)
                if _alive(info.get("pid")):
                    found.append({"kind": kind, "pid": info["pid"], "port": info.get("port"), "workspace": str(workspace)})
        for recording in [workspace / ".local/recording.json", *workspace.glob(".local/recordings/*/recording.json")]:
            if recording.is_file():
                capture = _json(recording).get("network_capture") or {}
                if _alive(capture.get("pid")):
                    found.append({"kind": "recording", "pid": capture["pid"], "workspace": str(workspace), "recording": str(recording)})
        for heartbeat in workspace.glob(".local/runs/*/native/heartbeat.json"):
            if not heartbeat.with_name("process-result.json").exists():
                native = _json(heartbeat)
                if _alive(native.get("pid")):
                    found.append({"kind": "native", "pid": native["pid"], "workspace": str(workspace)})
        file = workspace / ".local/autoskill.db"
        if file.is_file():
            with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "runs" in tables:
                    for run_id, pid in connection.execute("SELECT id,pid FROM runs WHERE status='Running'"):
                        if _alive(pid):
                            found.append({"kind": "run", "id": run_id, "pid": pid, "workspace": str(workspace)})
    return found


def _processes() -> list[dict]:
    if sys.platform.startswith("linux"):
        found = []
        for folder in Path("/proc").iterdir():
            if not folder.name.isdigit() or int(folder.name) == os.getpid():
                continue
            try:
                argv = [value.decode("utf-8", errors="replace") for value in (folder / "cmdline").read_bytes().split(b"\0") if value]
                found.append({"pid": int(folder.name), "argv": argv, "cwd": str((folder / "cwd").resolve()), "exe": str((folder / "exe").resolve())})
            except (OSError, ValueError):
                continue
        return found
    if os.name == "nt":
        command = "Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                   capture_output=True, text=True, timeout=30, check=True)
        rows = json.loads(completed.stdout or "[]")
        return [{"pid": row["ProcessId"], "argv": _windows_argv(row.get("CommandLine") or ""), "cwd": "", "exe": row.get("ExecutablePath") or ""}
                for row in (rows if isinstance(rows, list) else [rows]) if row["ProcessId"] != os.getpid()]
    return []


def _windows_argv(command: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        return []
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(pointer)


def _under(value: str, roots: list[Path]) -> bool:
    path = Path(value)
    if not value or not path.is_absolute():
        return False
    path = path.resolve()
    return any(path.is_relative_to(root) for root in roots)


def _process_blockers(target: Path, workspaces: list[Path]) -> list[dict]:
    found = []
    roots = [target, *workspaces]
    sessions = set()
    for workspace in workspaces:
        sessions.add("autoskill-" + hashlib.sha1(str(workspace).lower().encode()).hexdigest()[:10])
        for file in workspace.glob(".local/recordings/*/recording.json"):
            value = _json(file).get("session_id")
            if value:
                sessions.add("autoskill-" + hashlib.sha256(json.dumps([str(workspace), value], ensure_ascii=False).encode()).hexdigest()[:20])
    for process in _processes():
        joined = " ".join(process["argv"])
        owned = any(_under(value, roots) for value in [process["cwd"], process["exe"], *process["argv"]])
        executable = Path(process["exe"]).name.lower()
        application = executable.startswith(("python", "autoskill", "agent-browser", "node", "chrome", "chromium"))
        upgrading = "autoskill-upgrade.py" in joined or " upgrade " in " " + joined + " "
        if not upgrading and application and ((owned and "autoskill" in joined) or any(session in joined for session in sessions)):
            modules = [process["argv"][index + 1] for index, value in enumerate(process["argv"][:-1]) if value == "-m"]
            kind = "queue_worker" if "autoskill.server.jobs" in modules else "service" if "autoskill" in modules and "dashboard" in process["argv"] else "process"
            found.append({"kind": kind, "pid": process["pid"], "process": "autoSkill/CDP/浏览器后台进程"})
    return found


def _idle(workspaces: list[Path], target: Path | None = None) -> None:
    if _blockers(workspaces) or (target and _process_blockers(target, workspaces)):
        raise ValueError("autoSkill 服务或回放仍在运行；先停止此安装的仪表盘/插件并等待运行结束，再继续原升级任务。旧目录尚未替换")


@contextlib.contextmanager
def _lock(path: Path):
    """A stable advisory lock outside the directories being renamed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_stream(path) as stream:
        try:
            if os.name == "nt":
                import msvcrt

                if not path.stat().st_size:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise ValueError("另一个升级或写入进程仍持有锁，请稍后查询原任务：" + str(path)) from error
            raise
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _lock_stream(path: Path):
    if os.name != "nt":
        return path.open("a+b")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0xC0000000, 7, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    return os.fdopen(descriptor, "r+b")


def _target_lock(target: Path):
    identity = hashlib.sha256(os.path.normcase(str(target)).encode()).hexdigest()[:24]
    return _lock(target.parent / (".autoskill-upgrade-" + identity + ".lock"))


def _workspace_gate(workspace: Path):
    identity = hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()
    return _lock(Path.home() / ".autoskill" / "maintenance" / (identity + ".lock"))


def _stop_services(target: Path, data: list[Path]) -> None:
    """Stop only positively identified local autoSkill service processes; never active tests or browsers."""
    candidates = _process_blockers(target, data)
    stoppable = {item["pid"] for item in candidates if item["kind"] in ("service", "queue_worker")}
    active = [item for item in [*_blockers(data), *candidates] if item["pid"] not in stoppable]
    if active:
        raise ValueError("录制、回放、原生子进程或未能确认归属的后台进程仍在运行；先结束这些操作，再重试升级，任务数据不会删除")
    for pid in stoppable:
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while any(_alive(pid) for pid in stoppable) and time.monotonic() < deadline:
        time.sleep(0.1)
    _idle(data, target)


@contextlib.contextmanager
def _quiescent(record: dict, stop_services: bool = False):
    target = Path(record["target"])
    data = [Path(info["path"]) for info in record["plan"]["workspaces"]]
    if stop_services:
        _stop_services(target, data)
    _idle(data, target)
    with contextlib.ExitStack() as stack:
        for workspace in data:
            stack.enter_context(_workspace_gate(workspace))
        for workspace in data:
            if (workspace / ".autoskill-plan.lock").is_file():
                stack.enter_context(_lock(workspace / ".autoskill-plan.lock"))
            local = workspace / ".local"
            if local.is_dir():
                stack.enter_context(_lock(local / "queue-worker.lock"))
                for lock in sorted(local.glob("locks/*.lock")):
                    stack.enter_context(_lock(lock))
        _idle(data, target)
        yield


def plan(package: str, target: str, *, workspaces: list[str] | None = None, config_paths: list[str] | None = None) -> dict:
    source, installation = Path(package).expanduser().resolve(strict=True), Path(target).expanduser().resolve(strict=True)
    if (installation / ".git").exists():
        raise ValueError("这是 Git 源码安装，离线升级器不会替换源码仓库；请按升级文档备份后 git pull --ff-only，或将新离线包安装到另一目录并沿用工作区")
    new = _package(source)
    old_skill = _skill(installation)
    if old_skill.relative_to(installation).as_posix() != new["skill"]:
        raise ValueError("新旧安装布局不同；请使用与旧离线安装对应的完整平台包")
    old = _json(installation / "package.json")
    data = _workspaces(installation, workspaces)
    configs = [installation / "cordis.patch.yml", *old_skill.glob("vendor/*/runtime-settings.json"), *(Path(name).expanduser().resolve() for name in config_paths or [])]
    configs = list(dict.fromkeys(configs))
    return {"format": FORMAT, "source": str(source), "target": str(installation), "old_version": old.get("version", "未知"),
            "new_version": new["version"], "source_sha256": new["source_sha256"], "skill": new["skill"],
            "workspaces": [{"path": str(file), "external": not file.is_relative_to(installation), "exists": file.exists()} for file in data],
            "configs": [str(file) for file in configs], "blockers": [*_blockers(data), *_process_blockers(installation, data)],
            "note": "备份整个工作区，包括项目、知识、原生资产、明文凭据、登录态、任务、运行记录和证据；外置工作区路径保持不变。"}


def _sqlite(file: Path) -> bool:
    with file.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\0"


def _walk(root: Path) -> tuple[list[Path], list[dict]]:
    files, links = [], []

    def visit(path: Path, ancestors: frozenset[Path], linked: bool = False) -> None:
        relative = path.relative_to(root)
        if relative.as_posix() in (".local/queue-worker.lock", ".autoskill-plan.lock") or (relative.parent.as_posix() == ".local/locks" and relative.suffix == ".lock"):
            return
        is_link = path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
        if is_link and not linked:
            links.append({"path": path.relative_to(root).as_posix(), "target": os.readlink(path), "directory": path.is_dir()})
        if path.is_dir():
            real = path.resolve()
            if real in ancestors:
                return
            for child in sorted(path.iterdir()):
                visit(child, ancestors | {real}, linked or is_link)
        elif path.is_file():
            files.append(path)
        elif not is_link:
            raise ValueError("工作区包含不能备份的特殊文件：" + str(path))

    for file in sorted(root.iterdir()):
        visit(file, frozenset({root.resolve()}))
    return files, links


def _fingerprint(source: Path) -> str:
    if not source.exists():
        return "absent"
    files, links = _walk(source)
    values = [(file.relative_to(source).as_posix(), _hash(file)) for file in files if not file.name.endswith("-shm")]
    return hashlib.sha256(json.dumps([values, links], sort_keys=True).encode()).hexdigest()


def _copy_workspace(source: Path, destination: Path) -> dict:
    """Back up live WAL pages into standalone database files; WAL/SHM are not replayed into the copies."""
    if not source.exists():
        return {"exists": False, "files": 0, "sqlite": [], "links": [], "fingerprint": "absent"}
    before = _fingerprint(source)
    incoming, links = _walk(source)
    databases = [file for file in incoming if file.relative_to(source).as_posix() == ".local/autoskill.db" and _sqlite(file)]
    companions = {str(file) + suffix for file in databases for suffix in ("-wal", "-shm")}
    destination.mkdir(parents=True)
    for folder in source.rglob("*"):
        if folder.is_dir() and not folder.is_symlink():
            (destination / folder.relative_to(source)).mkdir(parents=True, exist_ok=True)
    for file in incoming:
        if str(file) not in companions:
            copied = destination / file.relative_to(source)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, copied)
    saved = []
    for file in databases:
        copied = destination / file.relative_to(source)
        copied.unlink()
        with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(copied) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite 备份校验失败：" + str(file))
        shutil.copystat(file, copied)
        saved.append(file.relative_to(source).as_posix())
    for folder in [*source.rglob("*"), source]:
        if folder.is_dir() and not folder.is_symlink():
            shutil.copystat(folder, destination / folder.relative_to(source))
    files = {file.relative_to(destination).as_posix(): _hash(file) for file in destination.rglob("*") if file.is_file()}
    after = _fingerprint(source)
    if before != after:
        raise ValueError("工作区在备份期间发生变化，请停止写入后重试；旧安装未被替换：" + str(source))
    omitted = [file.relative_to(source).as_posix() for file in [source / ".local/queue-worker.lock", source / ".autoskill-plan.lock", *source.glob(".local/locks/*.lock")] if file.exists()]
    return {"exists": True, "files": len(files), "sha256": files, "sqlite": saved, "links": links,
            "fingerprint": after, "omitted_transient_locks": omitted}


def _load(journal: str) -> tuple[Path, dict]:
    file = Path(journal).expanduser().resolve(strict=True)
    record = _json(file)
    if record.get("format") != FORMAT or Path(record.get("job", "")).resolve() != file.parent:
        raise ValueError("升级恢复记录不匹配")
    return file, record


def _state(file: Path, record: dict, state: str, **changes) -> None:
    record.update(changes, state=state, updated_at=time.time())
    record.setdefault("history", []).append({"state": state, "at": record["updated_at"]})
    _write(file, record)


def status(journal: str) -> dict:
    file, record = _load(journal)
    return {"journal": str(file), "state": record["state"], "old_version": record["plan"]["old_version"],
            "new_version": record["plan"]["new_version"], "target": record["target"], "old_installation": record["old"],
            "backups": [{"path": item["path"], "workspaces": [{key: value for key, value in workspace.items() if key != "manifest"}
                                                                for workspace in item["workspaces"]], "configs": item["configs"]}
                        for item in record.get("backups", [])],
            "checks": record.get("checks", []), "error": record.get("error", ""), "last_action": record.get("last_action", {}),
            "browser_validation": {"state": "pending", "command": "autoskill doctor --launch", "note": "自动切换检查不启动浏览器；重新启用安装后运行此命令完成本机浏览器验收"},
            "hint": "保留升级记录与旧目录；它们包含完整数据和恢复依据，不会自动删除。"}


def _snapshot(file: Path, record: dict) -> dict:
    directory = file.parent / "backups" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    snapshot = {"path": str(directory), "workspaces": [], "configs": []}
    for index, info in enumerate(record["plan"]["workspaces"]):
        source = Path(info["path"])
        destination = directory / f"workspace-{index}"
        inventory = _copy_workspace(source, destination)
        _write(directory / f"workspace-{index}.json", inventory)
        snapshot["workspaces"].append({"source": str(source), "backup": str(destination), "external": info["external"],
                                       "manifest": str(directory / f"workspace-{index}.json"), "files": inventory["files"], "sqlite": inventory["sqlite"],
                                       "exists": inventory["exists"], "fingerprint": inventory["fingerprint"]})
    for index, name in enumerate(record["plan"]["configs"]):
        source = Path(name)
        destination = directory / f"config-{index}"
        if source.is_file():
            shutil.copy2(source, destination)
        snapshot["configs"].append({"source": name, "backup": str(destination), "exists": source.is_file(),
                                     "sha256": _hash(destination) if destination.exists() else "absent"})
    record.setdefault("backups", []).append(snapshot)
    _sync_tree(directory)
    _write(file, record)
    return snapshot


def _assert_unchanged(snapshot: dict, *, moved_from: Path | None = None, moved_to: Path | None = None) -> None:
    def source_path(value):
        source = Path(value)
        return moved_to / source.relative_to(moved_from) if moved_from and moved_to and source.is_relative_to(moved_from) else source

    for item in snapshot["workspaces"]:
        if _fingerprint(source_path(item["source"])) != item["fingerprint"]:
            raise ValueError("备份后工作区仍有写入；本次切换已取消：" + item["source"])
    for item in snapshot["configs"]:
        source = source_path(item["source"])
        if (_hash(source) if source.is_file() else "absent") != item["sha256"]:
            raise ValueError("备份后配置仍有写入；本次切换已取消：" + item["source"])


def _restore_links(root: Path, manifest: Path) -> None:
    for link in _json(manifest).get("links", []):
        destination = root / _relative(link["path"])
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.is_dir():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(link["target"], target_is_directory=link["directory"])


def prepare(package: str, target: str, *, workspaces: list[str] | None = None, config_paths: list[str] | None = None,
            job_dir: str | None = None, stop_services: bool = False) -> dict:
    installation = Path(target).expanduser().resolve(strict=True)
    with _target_lock(installation):
        return _prepare(package, str(installation), workspaces=workspaces, config_paths=config_paths, job_dir=job_dir, stop_services=stop_services)


def _prepare(package: str, target: str, *, workspaces: list[str] | None, config_paths: list[str] | None,
             job_dir: str | None, stop_services: bool) -> dict:
    info = plan(package, target, workspaces=workspaces, config_paths=config_paths)
    installation = Path(info["target"])
    directory = Path(job_dir).expanduser().resolve() if job_dir else installation.parent / (".autoskill-upgrade-" + uuid.uuid4().hex)
    source = Path(info["source"])
    if directory.is_relative_to(installation) or (source.is_dir() and directory.is_relative_to(source)):
        raise ValueError("升级记录和备份目录必须位于旧安装及新包目录之外")
    if any(directory.is_relative_to(Path(item["path"])) for item in info["workspaces"]):
        raise ValueError("升级备份目录不能放入被备份的工作区内")
    journal = directory / "journal.json"
    if journal.is_file():
        previous = _json(journal)
        if previous["target"] == str(installation) and previous["plan"]["source_sha256"] == info["source_sha256"]:
            return status(str(journal))
        raise ValueError("该恢复目录属于另一项升级，请使用新的目录")
    if not stop_services:
        _idle([Path(item["path"]) for item in info["workspaces"]], installation)
    directory.mkdir(parents=True, exist_ok=False)
    identity = uuid.uuid4().hex
    record = {"format": FORMAT, "job": str(directory), "target": str(installation), "plan": info,
              "payload": str(directory / "payload"), "stage": str(installation.parent / (".autoskill-stage-" + identity)),
              "old": str(installation.parent / (".autoskill-old-" + identity)), "failed": [], "backups": []}
    _state(journal, record, "preparing")
    try:
        _extract(source, Path(record["payload"]))
        _verify_package(Path(record["payload"]), complete=True)
        _runtime(Path(record["payload"]))
        helper = Path(record["payload"]) / "autoskill-upgrade.py"
        if not helper.is_file():
            raise ValueError("新包缺少独立升级器 autoskill-upgrade.py")
        shutil.copy2(helper, directory / "autoskill-upgrade.py")
        _sync_tree(directory)
        if stop_services:
            _state(journal, record, "awaiting_stop")
            return handoff(str(journal), action="prepare-resume", stop_services=True)
        with _quiescent(record):
            _snapshot(journal, record)
            _state(journal, record, "prepared")
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as error:
        _state(journal, record, "prepare_failed", error=str(error))
        raise
    return status(str(journal))


def prepare_resume(journal: str, *, stop_services: bool = False) -> dict:
    file, record = _load(journal)
    with _target_lock(Path(record["target"])):
        file, record = _load(journal)
        if record["state"] == "prepared":
            return status(journal)
        if record["state"] not in ("awaiting_stop", "prepare_failed", "preparing"):
            raise ValueError("当前状态不需要继续准备升级")
        try:
            _verify_package(Path(record["payload"]), complete=True)
            with _quiescent(record, stop_services):
                _snapshot(file, record)
                _state(file, record, "prepared", error="")
        except (OSError, ValueError, sqlite3.Error) as error:
            _state(file, record, "prepare_failed", error=str(error))
            raise
        return status(journal)


def _abandon(path: Path, record: dict) -> None:
    if path.exists() or path.is_symlink():
        destination = path.with_name(path.name + "-retained-" + uuid.uuid4().hex)
        _rename(path, destination)
        record["failed"].append(str(destination))


def _copy_extras(old: Path, stage: Path, workspaces: list[Path]) -> None:
    """Carry unrecognized user directories without copying old application modules over new ones."""
    old_skill = _skill(old)
    def ignored(folder, names):
        return [name for name in names if Path(folder, name) in workspaces]

    for source, destination, protected in ((old, stage, {*PROGRAM_ROOTS, old_skill.relative_to(old).parts[0]}),
                                            (old_skill, _skill(stage), SKILL_PROGRAM_ROOTS | {"workspace"})):
        for file in source.iterdir():
            target = destination / file.name
            if file.name in protected or target.exists() or file in workspaces:
                continue
            if file.is_symlink():
                target.symlink_to(os.readlink(file), target_is_directory=file.is_dir())
            elif file.is_dir():
                shutil.copytree(file, target, symlinks=True, ignore=ignored)
            else:
                shutil.copy2(file, target)


def _overlay(stage: Path, record: dict, snapshot: dict) -> list[str]:
    old = Path(record["target"])
    owned = _package(stage)["manifest"]["files"]
    for info in snapshot["workspaces"]:
        source, backup = Path(info["source"]), Path(info["backup"])
        if info["external"]:
            continue
        relative = source.relative_to(old)
        prefix = relative.as_posix() + "/"
        if any(name.startswith(prefix) for name in owned):
            raise ValueError("工作区与新包程序文件重叠，不能在保留数据时覆盖程序：" + str(source))
        target = stage / relative
        if target.exists() or target.is_symlink():
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if info["exists"]:
            shutil.copytree(backup, target, symlinks=True)
            _restore_links(target, Path(info["manifest"]))
    default = _skill(old) / "workspace"
    if default.is_symlink():
        target = _skill(stage) / "workspace"
        if not target.exists() and not target.is_symlink():
            target.symlink_to(os.readlink(default), target_is_directory=True)
    overrides = []
    for info in snapshot["configs"]:
        source = Path(info["source"])
        if source.is_relative_to(old):
            relative = source.relative_to(old)
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if info["exists"]:
                shutil.copy2(info["backup"], destination)
            else:
                destination.unlink(missing_ok=True)
            overrides.append(relative.as_posix())
    return overrides


def _outside(record: dict) -> None:
    executable = Path(sys.executable).resolve()
    if any(executable.is_relative_to(Path(record[key])) for key in ("target", "stage", "old")):
        raise ValueError("升级器不能使用正在被替换的 Python；请使用 upgrade apply 的独立进程交接，或新包里的独立升级器")


def _validate_installation(root: Path, directory: Path, overrides: list[str], snapshot: dict) -> list[dict]:
    _verify_package(root, overrides)
    environment = {**os.environ, "AUTOSKILL_WORKSPACE": str(directory / "probe-workspace"), "AUTOSKILL_BRIDGE": "off",
                   "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_SESSION": "", "AUTOSKILL_CASE_SKILL": str(directory / "probe-cases"),
                   "PYTHONPATH": str(_skill(root) / "app"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    checks = []
    for command in ("verify", "doctor"):
        completed = subprocess.run([str(_runtime(root)), "-m", "autoskill", command], cwd=root, env=environment,
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            result = {"ok": False, "error": "升级后的命令没有返回有效 JSON"}
        checks.append({"command": command, "ok": completed.returncode == 0 and result.get("ok") is True, "result": result})
        if not checks[-1]["ok"]:
            raise ValueError("升级后 " + command + " 检查失败；旧版本将恢复。诊断：" + json.dumps(result, ensure_ascii=False))
    migration = (
        "import json,os,sqlite3; from pathlib import Path; from autoskill import db; "
        "p=Path(os.environ['AUTOSKILL_WORKSPACE'])/'.local/autoskill.db'; "
        "c=sqlite3.connect(p) if p.exists() else None; "
        "tables=[r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")] if c else []; "
        "before={t:c.execute('select count(*) from '+chr(34)+t.replace(chr(34),chr(34)*2)+chr(34)).fetchone()[0] for t in tables}; "
        "c.close() if c else None; db.init(); c=sqlite3.connect(p) if p.exists() else None; "
        "ok=(not c or c.execute('pragma integrity_check').fetchone()[0]=='ok') and all("
        "c.execute('select count(*) from '+chr(34)+t.replace(chr(34),chr(34)*2)+chr(34)).fetchone()[0]>=n for t,n in before.items()); "
        "print(json.dumps({'ok':ok,'tables':len(before),'scope':'backup_copy'})); c.close() if c else None"
    )
    for index, item in enumerate(snapshot["workspaces"]):
        if not item["exists"]:
            continue
        probe = directory / ("probe-data-" + uuid.uuid4().hex)
        shutil.copytree(item["backup"], probe)
        completed = subprocess.run([str(_runtime(root)), "-c", migration], cwd=root,
                                   env={**environment, "AUTOSKILL_WORKSPACE": str(probe)}, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            result = {"ok": False, "error": "旧工作区副本迁移检查没有返回有效 JSON"}
        checks.append({"command": "workspace_migration", "workspace": index, "ok": completed.returncode == 0 and result.get("ok") is True, "result": result})
        if not checks[-1]["ok"]:
            raise ValueError("旧数据副本初始化或完整性检查失败，原数据未迁移；请保留恢复记录与 probe-data 目录检查")
    return checks


def _restore(file: Path, record: dict, error: str = "") -> None:
    target, old = Path(record["target"]), Path(record.get("rollback_source") or record["old"])
    _state(file, record, "rolling_back", error=error)
    if old.exists():
        _abandon(target, record)
        _write(file, record)
        _rename(old, target)
    if not target.exists():
        raise ValueError("旧目录和目标目录都不存在；请保留备份目录并检查恢复记录：" + str(file))
    _state(file, record, "rolled_back", error=error)


@contextlib.contextmanager
def _action(file: Path, record: dict, name: str):
    record["last_action"] = {"name": name, "state": "running", "at": time.time()}
    _write(file, record)
    try:
        yield
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        latest = _json(file)
        latest.update(last_action={"name": name, "state": "failed", "at": time.time()}, error=str(error))
        _write(file, latest)
        raise
    else:
        latest = _json(file)
        latest["last_action"] = {"name": name, "state": "succeeded", "at": time.time()}
        _write(file, latest)


def apply(journal: str, *, stop_services: bool = False) -> dict:
    _, record = _load(journal)
    with _target_lock(Path(record["target"])):
        file, record = _load(journal)
        if record["state"] == "completed":
            return status(journal)
        with _action(file, record, "apply"), _quiescent(record, stop_services):
            _apply(file, record)
        return status(journal)


def _apply(file: Path, record: dict) -> dict:
    if record["state"] == "completed":
        return status(str(file))
    if record["state"] not in ("prepared", "rolled_back", "apply_failed"):
        raise ValueError("升级已中断或尚未准备完成，请先运行 upgrade recover")
    _outside(record)
    data = [Path(info["path"]) for info in record["plan"]["workspaces"]]
    _idle(data, Path(record["target"]))
    target, stage, old = (Path(record[name]) for name in ("target", "stage", "old"))
    if old.exists():
        raise ValueError("旧安装备份已存在，请先恢复当前升级记录")
    try:
        _state(file, record, "staging", error="")
        _verify_package(Path(record["payload"]), complete=True)
        _abandon(stage, record)
        _write(file, record)
        shutil.copytree(record["payload"], stage, symlinks=True)
        snapshot = _snapshot(file, record)
        _copy_extras(target, stage, data)
        overrides = _overlay(stage, record, snapshot)
        _write(stage / ".autoskill-upgrade-installation.json", {"job": record["job"]})
        _verify_package(stage, overrides)
        _idle(data, target)
        _sync_tree(stage)
        _assert_unchanged(snapshot)
        _state(file, record, "switching_old", overrides=overrides)
        _rename(target, old)
        _state(file, record, "old_moved")
        _assert_unchanged(snapshot, moved_from=target, moved_to=old)
        _rename(stage, target)
        _state(file, record, "new_moved")
        checks = _validate_installation(target, file.parent, overrides, snapshot)
        _state(file, record, "completed", checks=checks)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        if old.exists():
            try:
                _restore(file, record, str(error))
            except OSError as restore_error:
                _state(file, record, "rollback_failed", error=str(error) + "; 恢复暂未完成：" + str(restore_error))
        else:
            _state(file, record, "apply_failed", error=str(error))
        raise
    return status(str(file))


def recover(journal: str, *, stop_services: bool = False) -> dict:
    _, record = _load(journal)
    if record["state"] in ("preparing", "awaiting_stop", "prepare_failed"):
        return prepare_resume(journal, stop_services=stop_services)
    with _target_lock(Path(record["target"])):
        file, record = _load(journal)
        if record["state"] in ("completed", "rolled_back", "prepared"):
            if record["state"] == "completed":
                _verify_package(Path(record["target"]), record.get("overrides"))
            return status(journal)
        with _action(file, record, "recover"), _quiescent(record, stop_services):
            marker = Path(record["target"]) / ".autoskill-upgrade-installation.json"
            current_is_new = marker.is_file() and _json(marker).get("job") == record["job"]
            if record["state"] == "rollback_staging" or (current_is_new and Path(record["old"]).exists()):
                return _rollback(file, record)
            return _recover(file, record)


def _recover(file: Path, record: dict) -> dict:
    if record["state"] in ("completed", "rolled_back", "prepared"):
        return status(str(file))
    _outside(record)
    _idle([Path(info["path"]) for info in record["plan"]["workspaces"]], Path(record["target"]))
    if Path(record["old"]).exists() or record.get("rollback_source"):
        _restore(file, record, "恢复上次未完成的升级")
    elif Path(record["target"]).exists() and Path(record["payload"]).exists():
        _verify_package(Path(record["payload"]), complete=True)
        _state(file, record, "prepared", error="上次切换尚未开始；可以继续 apply")
    else:
        raise ValueError("没有可自动恢复的安装；请保留恢复目录：" + str(file.parent))
    return status(str(file))


def rollback(journal: str, *, stop_services: bool = False) -> dict:
    _, record = _load(journal)
    with _target_lock(Path(record["target"])):
        file, record = _load(journal)
        if record["state"] == "rolled_back":
            return status(journal)
        with _action(file, record, "rollback"), _quiescent(record, stop_services):
            _rollback(file, record)
        return status(journal)


def _rollback(file: Path, record: dict) -> dict:
    if record["state"] == "rolled_back":
        return status(str(file))
    _outside(record)
    _idle([Path(info["path"]) for info in record["plan"]["workspaces"]], Path(record["target"]))
    if not Path(record["old"]).exists():
        raise ValueError("此升级没有可回退的旧安装目录")
    marker = Path(record["target"]) / ".autoskill-upgrade-installation.json"
    current_is_new = marker.is_file() and _json(marker).get("job") == record["job"]
    if record["state"] in ("completed", "rollback_staging") or current_is_new:
        _state(file, record, "rollback_staging")
        snapshot = _snapshot(file, record)
        old = Path(record["stage"] + "-rollback")
        _abandon(old, record)
        old_root = Path(record["old"])
        internal = [old_root / Path(item["source"]).relative_to(Path(record["target"])) for item in snapshot["workspaces"] if not item["external"]]
        shutil.copytree(old_root, old, symlinks=True, ignore=lambda folder, names: [name for name in names if Path(folder, name) in internal])
        for info in snapshot["workspaces"]:
            if not info["external"]:
                destination = old / Path(info["source"]).relative_to(Path(record["target"]))
                if destination.is_symlink() or destination.is_file():
                    destination.unlink()
                elif destination.is_dir():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if info["exists"]:
                    shutil.copytree(info["backup"], destination, symlinks=True)
                    _restore_links(destination, Path(info["manifest"]))
        for info in snapshot["configs"]:
            original = Path(info["source"])
            if original.is_relative_to(Path(record["target"])):
                destination = old / original.relative_to(Path(record["target"]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                if info["exists"]:
                    shutil.copy2(info["backup"], destination)
                else:
                    destination.unlink(missing_ok=True)
        _assert_unchanged(snapshot)
        _sync_tree(old)
        _state(file, record, "rollback_ready", rollback_source=str(old))
    _restore(file, record, "已恢复旧程序并保留当前工作区数据")
    return status(str(file))


def handoff(journal: str, action: str = "apply", *, stop_services: bool = False) -> dict:
    if action not in ("apply", "rollback", "recover", "prepare-resume"):
        raise ValueError("升级交接动作无效")
    file, record = _load(journal)
    payload = Path(record["payload"])
    _verify_package(payload, complete=True)
    if _hash(file.parent / "autoskill-upgrade.py") != _hash(payload / "autoskill-upgrade.py"):
        raise ValueError("独立升级器与冻结的新包不匹配，请保留恢复记录并重新准备升级")
    command = [str(_runtime(Path(record["payload"]))), str(file.parent / "autoskill-upgrade.py"), action, str(file), "--wait-pid", str(os.getpid())]
    if stop_services:
        command.append("--stop-services")
    with (file.parent / "handoff.log").open("ab") as output:
        child = subprocess.Popen(command, cwd=file.parent, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "AUTOSKILL_BRIDGE": "off"},
                                 start_new_session=os.name != "nt", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"state": "launched", "journal": str(file), "pid": child.pid, "command": command,
            "next": "当前 CLI 退出后独立升级器开始执行；使用 upgrade status 查询结果，launched 不表示升级完成"}


def main() -> int:
    parser = argparse.ArgumentParser(description="完整离线升级与数据恢复；在旧安装目录外运行。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "prepare"):
        command = commands.add_parser(name)
        command.add_argument("package")
        command.add_argument("--target", required=True)
        command.add_argument("--workspace", action="append", default=[])
        command.add_argument("--config", action="append", default=[])
        if name == "prepare":
            command.add_argument("--job-dir")
            command.add_argument("--stop-services", action="store_true")
    for name in ("apply", "status", "recover", "rollback", "prepare-resume"):
        command = commands.add_parser(name)
        command.add_argument("journal")
        command.add_argument("--wait-pid", type=int)
        command.add_argument("--stop-services", action="store_true")
    args = parser.parse_args()
    try:
        parent = getattr(args, "wait_pid", None)
        while parent and _alive(parent):
            time.sleep(0.1)
        if args.command in ("plan", "prepare"):
            options = {"workspaces": args.workspace, "config_paths": args.config}
            if args.command == "prepare":
                options["job_dir"] = args.job_dir
                options["stop_services"] = args.stop_services
            result = globals()[args.command](args.package, args.target, **options)
        else:
            options = {} if args.command == "status" else {"stop_services": args.stop_services}
            result = globals()[args.command.replace("-", "_")](args.journal, **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))  # noqa: T201  # The standalone command returns one JSON object.
        return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))  # noqa: T201  # Standalone errors use the same JSON contract.
        return 2


if __name__ == "__main__":
    sys.exit(main())
