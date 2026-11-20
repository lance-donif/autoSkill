"""Initialize, inspect and transfer one complete external data root without replacing program files."""
from __future__ import annotations

import contextlib
import datetime
import errno
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import data_refs, data_runtime, data_transfer, maintenance, paths, runtime_identity
from .locking import named_lock


class DataError(ValueError):
    """Data transfer failures carry concrete activity or consistency details."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _stamp() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


def _metadata(root: Path) -> dict:
    if not (root / "data-root.json").is_file():
        return {}
    value = data_transfer.read_json(root / "data-root.json")
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("data_id"), str) or not value["data_id"]:
        raise DataError("invalid_data", "数据根标识无效，未覆盖原文件")
    return value


@contextlib.contextmanager
def selected_workspace(expected: str | None = None):
    """Keep a request on the dataset and location from which its selection was read."""
    root = paths.workspace()
    with maintenance.access(root):
        data_id = _metadata(root).get("data_id")
        # A copied workspace retains its data_id and can then be edited independently.
        identity = f"{data_id}:{runtime_identity.location_id(root)}" if data_id else None
        if expected is not None and expected != identity:
            raise DataError("data_changed", "数据目录已变化，请刷新页面后重新选择要删除的内容")
        yield identity


def _source(source: Path) -> tuple[Path, Path | None]:
    for child in ("", "workspace", "autoskill/workspace", "skills/autoskill/workspace"):
        folder = source / child
        if folder.is_dir() and any((folder / name).exists() for name in ("data-root.json", "projects", ".local")):
            skill = folder.parent if folder.name == "workspace" else None
            return folder.resolve(), skill
    raise DataError("invalid_data", "没有找到完整数据目录；请选择数据根、旧 workspace、旧安装目录或 autoSkill 数据 ZIP")


def _check_idle(root: Path) -> None:
    active = data_runtime.activities(root)
    if active:
        raise DataError("data_busy", "以下录制、回放或浏览器仍在使用源数据，请结束后重试，原数据未修改", activities=active)


@contextlib.contextmanager
def _gate(root: Path):
    _check_idle(root)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(maintenance.exclusive(root))
        except ValueError as error:
            raise DataError("data_busy", str(error), activities=data_runtime.activities(root)) from error
        _check_idle(root)
        yield


def _original_path(path: Path, origin: tuple[Path, str] | None) -> str:
    if origin is None or not path.is_relative_to(origin[0]):
        return str(path)
    original = origin[1]
    base = PureWindowsPath(original) if PureWindowsPath(original).is_absolute() else PurePosixPath(original)
    return str(base.joinpath(*path.relative_to(origin[0]).parts))


def _transport_path(value: str, origin: tuple[Path, str] | None) -> Path:
    if origin is not None:
        original = origin[1]
        base = PureWindowsPath(original) if PureWindowsPath(original).is_absolute() else PurePosixPath(original)
        path = type(base)(value)
        if path.is_relative_to(base):
            return origin[0].joinpath(*path.relative_to(base).parts)
    return Path(value)


def _plan_sources(root: Path, skill: Path | None, *, origin: tuple[Path, str] | None = None) -> list[Path]:
    found = []
    if skill:
        found.append(skill.parent / "autoskill-cases")
    if os.environ.get("AUTOSKILL_CASE_SKILL"):
        try:
            found.append(data_refs.resolve(os.environ["AUTOSKILL_CASE_SKILL"], root))
        except data_refs.UnmappedPathError:
            found.append(_transport_path(os.environ["AUTOSKILL_CASE_SKILL"], origin).resolve())
    database = root / ".local/autoskill.db"
    if database.is_file():
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("runs", "tasks"):
                if table not in tables or "plan_path" not in {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}:
                    continue
                for row in connection.execute(f"SELECT DISTINCT plan_path FROM {table} WHERE plan_path!=''"):
                    try:
                        found.append(data_refs.resolve(row[0], root))
                    except data_refs.UnmappedPathError:
                        found.append(_transport_path(row[0], origin))
    return list(dict.fromkeys(folder.resolve() for folder in found if folder.is_dir() and not folder.resolve().is_relative_to(root)))


def _collect_plans(source: Path, destination: Path, metadata: dict, skill: Path | None, *, origin: tuple[Path, str] | None = None) -> list[dict]:
    copied = []
    mappings = list(metadata.get("path_mappings", []))
    for folder in _plan_sources(source, skill, origin=origin):
        canonical = _original_path(folder, origin)
        existing = next((item for item in mappings if item["source"] == canonical), None)
        if existing and (destination / existing["destination"]).is_dir():
            continue
        default = skill is not None and folder == (skill.parent / "autoskill-cases").resolve()
        relative = (".local/case-plans/default/autoskill-cases" if default else
                    ".local/case-plans/imported/" + uuid.uuid5(uuid.NAMESPACE_URL, canonical).hex + "/autoskill-cases")
        target = destination / relative
        if target.exists():
            raise DataError("target_not_empty", "归集用例表的目标已存在，未覆盖", root=str(target))
        with _gate(folder):
            inventory = data_transfer.snapshot(folder, target)
        mappings.append({"source": canonical, "destination": relative})
        copied.append({"source": canonical, "destination": relative, "files": len(inventory["files"])})
    metadata["path_mappings"] = mappings
    return copied


def _runtime_settings(skill: Path | None, destination: Path) -> list[str]:
    copied = []
    if not skill:
        return copied
    for file in (skill / "vendor").glob("*/runtime-settings.json"):
        target = destination / ".local/runtime-settings" / (file.parent.name + ".json")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            copied.append(target.relative_to(destination).as_posix())
    return copied


def _new_metadata(root: Path, existing: dict | None = None, *, legacy: bool = False) -> dict:
    value = dict(existing or {})
    value.update(schema_version=1, data_id=value.get("data_id") or uuid.uuid4().hex)
    value.setdefault("created_at", _stamp())
    value.setdefault("legacy_roots", [])
    value["locations"] = list(dict.fromkeys([*value.get("locations", []), str(root)]))
    value.setdefault("legacy_dashboard_import_eligible", legacy)
    return value


def status() -> dict:
    root = paths.workspace()
    metadata = _metadata(root)
    legacy = paths.SKILL / "workspace"
    external = os.environ.get("AUTOSKILL_CASE_SKILL")
    selected = paths.case_skill()
    return {"root": str(root), "data_id": metadata.get("data_id"), "initialized": bool(metadata),
            "legacy_source": str(legacy) if legacy.is_dir() and legacy.resolve() != root else None,
            "external_case_skill": {"path": external, "resolved": str(selected), "exists": selected.is_dir(), "inside_data_root": selected.is_relative_to(root)},
            "activity": data_runtime.activities(root), "suggested_import_target": str(root.with_name(root.name + "-imported")),
            "legacy_dashboard_import_eligible": bool(metadata.get("legacy_dashboard_import_eligible")),
            "note": "数据目录包含用例、凭据、登录态、工作流、执行记录和证据；程序更新只替换程序目录。"}


def ensure_initialized(root: Path | None = None) -> dict:
    """Bootstrap before CLI/server work; read-only context and direct database readers never call this."""
    root = (root or paths.workspace()).resolve()
    metadata = _metadata(root)
    marker = data_transfer.read_json(root / ".local/runtime-location.json")
    if not metadata and runtime_identity.is_local(marker, root):
        with maintenance.access(root), named_lock("data-root", "metadata"):
            metadata = _metadata(root) or _new_metadata(root)
            data_transfer.write_json(root / "data-root.json", metadata)
    if metadata and runtime_identity.is_local(marker, root) and not (root / ".local/relocation-pending.json").is_file() and not _plan_sources(root, None):
        return {"status": "ready", "root": str(root), "data_id": metadata["data_id"]}
    legacy = paths.SKILL / "workspace"
    empty = _empty(root)
    if empty and not os.environ.get("AUTOSKILL_WORKSPACE") and root == paths.default_workspace() and legacy.is_dir() and legacy.resolve() != root:
        return import_data(legacy, root)
    with _gate(root):
        root.mkdir(parents=True, exist_ok=True)
        meaningful, _omitted = data_transfer.inventory(root)
        metadata = _new_metadata(root, _metadata(root), legacy=bool(meaningful))
        collected = _collect_plans(root, root, metadata, None)
        data_transfer.write_json(root / "data-root.json", metadata)
        attachment = data_runtime.attach(root)
        return {"status": "ready", "root": str(root), "data_id": metadata["data_id"], "collected_case_plans": collected, **attachment}


def _empty(target: Path) -> bool:
    if not target.exists():
        return True
    if not target.is_dir():
        return False
    return not any(target.iterdir())


def _empty_target(target: Path) -> None:
    if not _empty(target):
        raise DataError("target_not_empty", "导入目标必须是新的或空的数据目录；原数据未合并、覆盖或删除", root=str(target))


def import_data(source: str | Path, target: str | Path | None = None, *, original_source: str | Path | None = None) -> dict:
    source = Path(source).expanduser().resolve()
    target = Path(target).expanduser().resolve() if target is not None else paths.workspace()
    _empty_target(target)
    if source == target or target.is_relative_to(source) or source.is_relative_to(target):
        raise DataError("invalid_data", "导入源与目标不能相同或互相包含")
    target.parent.mkdir(parents=True, exist_ok=True)
    with maintenance.exclusive(target), tempfile.TemporaryDirectory(prefix=".autoskill-data-import-", dir=target.parent) as temporary:
        stage = Path(temporary) / "data"
        stage.mkdir()
        if source.is_file():
            try:
                manifest = data_transfer.extract(source, stage)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
                raise DataError("invalid_data", "ZIP 无法完整读取或校验失败，未发布数据：" + str(error)) from error
            metadata = _new_metadata(target, _metadata(stage), legacy=not bool(_metadata(stage)))
        else:
            actual, skill = _source(source)
            with _gate(actual):
                manifest = data_transfer.snapshot(actual, stage)
                origin = (source, str(original_source)) if original_source is not None else None
                canonical = _original_path(actual, origin)
                metadata = _new_metadata(target, _metadata(stage), legacy=not bool(_metadata(stage)))
                metadata["legacy_roots"] = list(dict.fromkeys([*metadata["legacy_roots"], canonical]))
                data_transfer.write_json(stage / "data-root.json", metadata)
                # Read dependencies from the copied database, not a later revision of the live source.
                if origin is None:
                    _collect_plans(stage, stage, metadata, skill)
                else:
                    _collect_plans(stage, stage, metadata, skill, origin=origin)
                _runtime_settings(skill, stage)
        metadata["legacy_roots"] = list(dict.fromkeys([*metadata["legacy_roots"], *metadata.get("locations", [])]))
        metadata["locations"] = list(dict.fromkeys([*metadata.get("locations", []), str(target)]))
        data_transfer.write_json(stage / "data-root.json", metadata)
        data_runtime.attach(stage, force=True, location_root=target)
        data_transfer.write_json(stage / ".local/data-import.json", {"source": str(original_source or source), "root": str(target),
                                                                  "at": _stamp(), "source_manifest": manifest, "original_source_retained": True})
        data_transfer.sync_tree(stage)
        _empty_target(target)
        try:
            if target.exists():
                target.rmdir()
            data_transfer.publish_directory(stage, target)
        except OSError as error:
            if error.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
                raise DataError("target_not_empty", "导入发布时目标目录发生变化，未覆盖或删除其中的数据，请选择空目录重试", root=str(target)) from error
            raise
    files, _omitted = data_transfer.inventory(target)
    return {"status": "imported", "source": str(original_source or source), "root": str(target), "data_id": metadata["data_id"], "files": len(files),
            "requires_restart": target != paths.workspace(), "manifest": {"files": len(files), "verified": True},
            "note": "已导入，原目录保留；导入到另一目录时尚未启用，请修改 workspace 配置并重启。"}


def export_data(destination: str | Path | None = None) -> dict:
    ensure_initialized()
    root = paths.workspace()
    destination = (Path(destination).expanduser().resolve() if destination is not None else
                   root.parent / (root.name + "-" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".zip"))
    if destination.is_relative_to(root):
        raise DataError("invalid_data", "完整数据包请保存到数据目录之外，避免把导出文件再次收入自身")
    with _gate(root), tempfile.TemporaryDirectory(prefix=".autoskill-data-export-", dir=root.parent) as temporary:
        stage = Path(temporary) / "data"
        stage.mkdir()
        inventory = data_transfer.snapshot(root, stage)
        metadata = _metadata(stage)
        manifest = {"format": "autoskill-data", "schema_version": 1, "data_id": metadata["data_id"], "created_at": _stamp(), **inventory}
        data_transfer.archive(stage, destination, manifest)
    from . import runtime_identity

    export_id = uuid.uuid4().hex
    result = {"status": "exported", "root": str(root), "data_id": metadata["data_id"], "archive": str(destination), "files": len(inventory["files"]),
              "export_id": export_id, "download_url": "/api/data/exports/" + export_id, "sha256": data_transfer.digest(destination), "size": destination.stat().st_size,
              "manifest": {"files": len(inventory["files"]), "verified": True, "sqlite": inventory["sqlite"], "omitted_runtime": inventory["omitted_runtime"]}}
    data_transfer.write_json(root / ".local/data-exports" / (export_id + ".json"),
                             {**result, "location": {"machine_id": runtime_identity.machine_id(), "location_id": runtime_identity.location_id()}})
    return result


def download_file(export_id: str) -> Path:
    """Only serve a recorded complete export whose location and bytes still match its receipt."""
    from . import runtime_identity

    if not isinstance(export_id, str) or not re.fullmatch(r"[a-f0-9]{32}", export_id):
        raise DataError("not_found", "数据导出标识无效")
    receipt = data_transfer.read_json(paths.workspace() / ".local/data-exports" / (export_id + ".json"))
    if not receipt or receipt.get("export_id") != export_id or not runtime_identity.is_local(receipt.get("location")):
        raise DataError("not_found", "当前数据位置没有可下载的这份导出，请重新导出")
    file = Path(receipt["archive"])
    if not file.is_file() or file.stat().st_size != receipt["size"] or data_transfer.digest(file) != receipt["sha256"]:
        raise DataError("invalid_data", "导出文件不存在或已变化，不能交付为原数据包")
    return file
