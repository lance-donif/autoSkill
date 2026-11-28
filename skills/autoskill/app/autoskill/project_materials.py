"""Browse authoritative project files and export portable knowledge, cases and report artifacts."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from . import data_refs, db, paths, projects, share
from .locking import named_lock, project_lock

CATEGORIES = ("knowledge", "testcases", "reports")
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}


def _digest(file: Path) -> str:
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _files(folder: Path):
    if not folder.is_dir():
        return
    root = paths.workspace().resolve()
    for file in sorted(folder.rglob("*")):
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            raise ValueError("项目资料不能包含指向数据目录外的链接")
        if file.is_file():
            yield file


def _legacy_reports(project_id: str):
    for row in db.rows("SELECT id FROM runs WHERE project_id = ? AND delete_requested = 0", (project_id,)):
        folder = paths.local() / "runs" / row["id"] / "test-report"
        if folder.is_dir():
            yield "历史任务/" + "run_" + row["id"], folder
    folder = paths.local() / "task-reports"
    for snapshot in sorted(folder.glob("*/snapshot.json")):
        value = json.loads(snapshot.read_bytes())
        if value.get("project_id") == project_id:
            yield "历史任务/" + snapshot.parent.name, snapshot.parent
    folder = paths.local() / "reports" / project_id
    if folder.is_dir():
        yield "历史汇总", folder


def _entry(project_id: str, category: str, file: Path, name: str, *, historical: bool = False) -> dict:
    reference = data_refs.store(file)
    identity = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return {"id": identity, "category": category, "name": name, "bytes": file.stat().st_size, "sha256": _digest(file),
            "text": file.suffix.lower() in TEXT_SUFFIXES, "historical": historical,
            "download_url": f"/api/project-materials/{quote(project_id, safe='')}/files/{identity}", "reference": reference}


def inventory(project_id: str) -> dict:
    with project_lock(project_id):
        root = projects.path(project_id)
        info = projects.get(project_id)
        items = []
        for category in CATEGORIES:
            folder = root / category
            items.extend(_entry(project_id, category, file, file.relative_to(folder).as_posix()) for file in _files(folder))
        for prefix, folder in _legacy_reports(project_id):
            items.extend(_entry(project_id, "reports", file, prefix + "/" + file.relative_to(folder).as_posix(), historical=True)
                         for file in _files(folder))
        return {"project": {key: info[key] for key in ("id", "name", "base_url")}, "root": str(root),
                "data_root": str(paths.workspace()), "directories": {key: str(root / key) for key in CATEGORIES}, "items": items,
                "counts": {key: sum(item["category"] == key for item in items) for key in CATEGORIES},
                "note": "知识库与用例直接读取项目原文件；新报告保存在 reports，旧报告原路径和字节保留。"}


def file_for(project_id: str, identity: str) -> Path:
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("资料文件标识无效")
    item = next((item for item in inventory(project_id)["items"] if item["id"] == identity), None)
    if item is None:
        raise ValueError("资料文件不存在或已移除")
    return data_refs.resolve(item["reference"])


def _knowledge_path(project_id: str, name: str) -> Path:
    from . import knowledge

    return knowledge.file_path(project_id, name.removeprefix("knowledge/") if isinstance(name, str) else name)


def write_knowledge(project_id: str, name: str, content: str, expected_hash: str | None = None, *, delete: bool = False) -> dict:
    """Compare the actual source revision before changing the one authoritative knowledge file."""
    if not isinstance(content, str):
        raise ValueError("知识内容需要是文字")
    if expected_hash is not None and (not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)):
        raise ValueError("知识内容版本无效，请重新读取文件")
    with project_lock(project_id):
        file = _knowledge_path(project_id, name)
        relative = file.relative_to(projects.path(project_id) / "knowledge")
        if relative.parts[0].casefold() == "raw":
            raise ValueError("原始资料只读")
        current = _digest(file) if file.is_file() else None
        if current != expected_hash:
            raise ValueError("知识文件已变化，请重新读取后再保存；原内容未覆盖")
        if delete:
            if current is None:
                raise ValueError("知识文件不存在")
            file.unlink()
            return {"deleted": True, "file": "knowledge/" + relative.as_posix()}
        file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=file.parent, prefix=".knowledge-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode("utf-8"))
        try:
            temporary.replace(file)
        finally:
            temporary.unlink(missing_ok=True)
        return _entry(project_id, "knowledge", file, relative.as_posix())


def _export_target(prefix: str) -> tuple[str, Path]:
    identity = prefix + "-" + uuid.uuid4().hex
    folder = paths.local() / "material-exports"
    folder.mkdir(parents=True, exist_ok=True)
    return identity, folder / (identity + ".zip")


def export_file(identity: str) -> Path:
    if not isinstance(identity, str) or not re.fullmatch(r"(?:materials|share)-[a-f0-9]{32}", identity):
        raise ValueError("资料包标识无效")
    file = paths.local() / "material-exports" / (identity + ".zip")
    if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(paths.workspace().resolve()):
        raise ValueError("导出资料包不存在")
    return file


def _export_receipt(identity: str, file: Path, **extra) -> dict:
    return {"export_id": identity, "file": str(file), "bytes": file.stat().st_size, "sha256": _digest(file),
            "download_url": "/api/project-materials/exports/" + identity, **extra}


def export_project(project_id: str) -> dict:
    """Export original project bytes, versioned sources and retained reports without declaring execution validity."""
    identity, target = _export_target("materials")
    try:
        with project_lock(project_id), contextlib.ExitStack() as locks:
            root = projects.path(project_id)
            reports = root / "reports"
            for folder in sorted(reports.iterdir()) if reports.is_dir() else []:
                if folder.is_dir():
                    locks.enter_context(named_lock("test-report", folder.name))
            files = [(file.relative_to(root).as_posix(), file) for file in _files(root)]
            for prefix, folder in _legacy_reports(project_id):
                locks.enter_context(named_lock("test-report", folder.name))
                files.extend(("reports/" + prefix + "/" + file.relative_to(folder).as_posix(), file) for file in _files(folder))
            records = {}
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
                for category in CATEGORIES:
                    package.writestr(f"{project_id}/{category}/", b"")
                for name, file in files:
                    key = project_id + "/" + name
                    records[key] = share._archive_file(package, file, key)
                manifest = {"format": "autoskill-project-materials", "schema": 1, "project_id": project_id,
                            "created_at": db.now(), "files": records, "includes_local_verification": False,
                            "note": "保留知识、用例、报告和领域版本原字节；这是资料归档，完整历史恢复请使用数据管理的整库导出。"}
                package.writestr("materials-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return _export_receipt(identity, target, files=len(records), note=manifest["note"])
    except Exception:
        target.unlink(missing_ok=True)
        raise


def export_share(spec: dict) -> dict:
    chosen = spec.get("project_ids")
    if chosen is not None and (not isinstance(chosen, list) or not all(isinstance(value, str) and projects.ID.fullmatch(value) for value in chosen)):
        raise ValueError("请选择有效项目")
    if "full" in spec and type(spec["full"]) is not bool:
        raise ValueError("完整包选项需要是布尔值")
    requested_secrets = spec.get("include_secrets", False)
    if type(requested_secrets) is not bool:
        raise ValueError("包含凭据选项需要是布尔值")
    include = requested_secrets and not spec.get("full")
    passphrase = spec.get("passphrase") if include else None
    if include and (not isinstance(passphrase, str) or not passphrase):
        raise ValueError("携带项目引用凭据需要填写加密口令")
    identity, target = _export_target("share")
    try:
        result = share.pack_full(out=str(target)) if spec.get("full") else share.pack(chosen, out=str(target), passphrase=passphrase)
        return _export_receipt(identity, target, **{key: value for key, value in result.items() if key not in ("file", "bytes")})
    except Exception:
        target.unlink(missing_ok=True)
        raise
