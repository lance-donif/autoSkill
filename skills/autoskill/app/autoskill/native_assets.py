"""Version whole native test projects in the shared domain without translating their semantics."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from . import db, projects

ASSET_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def relative(value: str) -> str:
    """Normalize a portable path inside the imported tree."""
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
        raise ValueError("资产内部路径必须是相对路径，不能包含上级目录")
    return path.as_posix()


def _prefix(asset_id: str) -> str:
    if not ASSET_ID.fullmatch(asset_id or ""):
        raise ValueError("资产标识需要使用字母、数字、点、短横线或下划线")
    return f"native-assets/{asset_id}"


def _files(source: Path) -> dict[str, tuple[bytes, int]]:
    """Dereference imported links while detecting cycles, preserving every file in the selected tree."""
    found = {}

    def visit(path: Path, name: str, ancestors: frozenset[Path]) -> None:
        resolved = path.resolve(strict=True)
        if resolved.is_dir():
            if resolved in ancestors:
                raise ValueError(f"资产目录存在循环链接：{name}")
            for child in sorted(path.iterdir()):
                visit(child, f"{name}/{child.name}".lstrip("/"), ancestors | {resolved})
        elif resolved.is_file():
            found[relative(name)] = (path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            raise ValueError(f"资产包含无法冻结的特殊文件：{name}")

    visit(source, source.name if source.is_file() else "", frozenset())
    if not found:
        raise ValueError("资产目录没有文件")
    return found


def import_asset(project_id: str, source: str, engine: str, *, name: str = "", argv: list[str] | None = None,
                 cwd: str = ".", source_info: dict | None = None, expected_head: str | None = None,
                 asset_id: str | None = None) -> dict:
    """Import original bytes and entry arguments in one domain commit; no runtime is started."""
    from . import domain, engines

    projects.path(project_id)
    adapter = engines.get(engine)
    file = Path(source).expanduser().resolve(strict=True)
    files = _files(file)
    workdir = relative(cwd)
    if workdir != "." and not any(key.startswith(workdir + "/") for key in files):
        raise ValueError(f"资产工作目录不存在：{workdir}")
    if argv is not None and not isinstance(argv, list):
        raise ValueError("原入口 argv 必须是字符串数组")
    entry = list(argv) if argv is not None else adapter.default_entry(list(files), workdir)
    if not entry or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in entry):
        raise ValueError("原入口 argv 必须是非空字符串数组，不使用 shell 字符串")
    asset_id = asset_id or db.new_id("asset")
    prefix = _prefix(asset_id)
    current = domain.read(project_id)
    expected = current["head"] if expected_head is None else expected_head
    refs = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode}
            for path, (data, mode) in files.items()]
    manifest = {"schema": 1, "id": asset_id, "project_id": project_id, "name": name or file.stem,
                "engine": engine, "argv": entry, "cwd": workdir, "files": refs,
                "origin": source_info or {"kind": "local", "path": str(file)}, "source_root": str(file if file.is_dir() else file.parent),
                "semantics": {"mode": "native", "converted": False, "notes": "保留原文件、入口和工作目录；不转换测试语义"}}
    changes = {f"{prefix}/files/{path}": data for path, (data, _) in files.items()}
    changes[f"{prefix}/asset.json"] = manifest
    old = [key for key in current.get("files", {}) if key.startswith(prefix + "/files/") and key not in changes]
    committed = domain.commit(project_id, changes, expected, reason="导入原生测试资产", remove=old)
    return {**manifest, "head": committed["head"], "asset_version": digest(manifest), "manifest": manifest}


def get(project_id: str, asset_id: str, head: str | None = None) -> dict:
    from . import domain

    snapshot = domain.read(project_id, head=head)
    key = f"{_prefix(asset_id)}/asset.json"
    manifest = snapshot["documents"].get(key)
    if not isinstance(manifest, dict):
        raise ValueError(f"原生资产 {asset_id} 不存在")
    return {**manifest, "head": snapshot["head"], "asset_version": digest(manifest), "manifest": manifest}


def list_assets(project_id: str) -> list[dict]:
    from . import domain

    snapshot = domain.read(project_id)
    return [{**value, "head": snapshot["head"], "asset_version": digest(value)}
            for key, value in sorted(snapshot["documents"].items())
            if key.startswith("native-assets/") and key.endswith("/asset.json") and isinstance(value, dict)]


def materialize(project_id: str, asset_id: str, destination: Path, head: str) -> dict:
    """Copy and verify a pinned generation; execution never reads the mutable import source."""
    from . import domain

    asset = get(project_id, asset_id, head)
    prefix = _prefix(asset_id)
    destination.mkdir(parents=True, exist_ok=True)
    for ref in asset["files"]:
        name = relative(ref["path"])
        data = domain.read_file(project_id, f"{prefix}/files/{name}", head=head)
        if len(data) != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError(f"资产文件校验失败：{name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(ref.get("mode", 0o644))
    return asset
