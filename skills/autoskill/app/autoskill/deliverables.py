"""Copy only the finished Excel and report files into the user's project folder.

Everything else (recordings, screenshots, frozen inputs, evidence, sheets) stays in the data
directory. An existing file with the same name is kept unless it has identical bytes or the
caller explicitly asks to replace it, so a hand-edited workbook is never silently overwritten.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from . import paths

UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe(name: str) -> str:
    return (UNSAFE.sub("-", name).strip().strip(".") or "交付文件")[:120]


def _digest(file: Path) -> str:
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with source.open("rb") as src, temporary.open("wb") as out:
            while chunk := src.read(1024 * 1024):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy(files: list[tuple[str, str | Path]], target: str | Path | None = None, *, overwrite: bool = False) -> dict:
    """Deliver (file name, source) pairs; returns one receipt per file and never raises for a file error."""
    folder = Path(target).expanduser().resolve() if target else paths.delivery_dir()
    if folder is None:
        return {"folder": None, "files": [], "note": "没有交付目录：设置 AUTOSKILL_DELIVERY_DIR，或把 --deliver-to 指向用户项目目录"}
    receipt = {"folder": str(folder), "files": [], "errors": []}
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return {**receipt, "errors": [{"name": None, "reason": f"交付目录不可写：{type(error).__name__}"}]}
    for name, source in files:
        source_path, destination = Path(source).expanduser().resolve(), folder / _safe(name)
        row = {"name": destination.name, "source": str(source_path), "path": str(destination)}
        try:
            if not source_path.is_file():
                raise FileNotFoundError("交付原件不存在")
            digest = _digest(source_path)
            row.update(sha256=digest, size=source_path.stat().st_size)
            if destination.exists():
                if destination.is_file() and _digest(destination) == digest:
                    row["unchanged"] = True
                elif not overwrite:
                    row.update(kept=True, existing_sha256=_digest(destination),
                               reason="交付目录已有同名文件且内容不同；保留原文件，确认后用覆盖方式重新交付")
                else:
                    _copy(source_path, destination)
            else:
                _copy(source_path, destination)
        except OSError as error:
            receipt["errors"].append({"name": destination.name, "reason": f"{type(error).__name__}：{error}"[:300]})
            continue
        receipt["files"].append(row)
    return receipt


def plan_receipt(project: dict, receipt: dict, target: str | Path | None = None) -> dict:
    label = _safe(project.get("name") or project["id"])
    files = [(f"{label}-功能测试用例.xlsx", receipt["delivery_excel"]),
             (f"{label}-功能测试用例-初版.xlsx", receipt["initial_excel"])]
    return copy(files, target)


def report_receipt(receipt: dict, target: str | Path | None = None) -> dict:
    files = [(Path(receipt[key]).name, receipt[key]) for key in ("excel", "markdown", "bugs")
             if isinstance(receipt.get(key), str) and receipt[key]]
    return copy(files, target)
