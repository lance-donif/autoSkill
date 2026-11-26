"""Scan the configured sync folder for native case files (JSON bundles and share ZIPs).

A JSON bundle is `{"project_id": ..., "cases": [...], "status": "pass"}`; only passing files are
imported. Content hashes make the scan incremental, so an unchanged file is never imported twice.
Imported cases keep their `{{variable}}` placeholders and run against the project's active
environment. Unreadable files become a failed receipt instead of stopping the scan.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import time
from pathlib import Path

from . import db, execution_inputs, paths, projects, share
from .locking import named_lock

PASS = {"", "pass", "passed", "通过"}
_last_attempt = 0.0


def _file() -> Path:
    return paths.local() / "sync" / "state.json"


def _read() -> dict:
    try:
        value = json.loads(_file().read_bytes())
    except (OSError, ValueError):
        return {"files": {}}
    return value if isinstance(value, dict) and isinstance(value.get("files"), dict) else {"files": {}}


def status() -> dict:
    folder, error_dir = paths.sync_dir(), paths.error_dir()
    state = _read()
    return {"sync_dir": str(folder) if folder else None, "error_dir": str(error_dir) if error_dir else None,
            "last_scan": state.get("last_scan"),
            "files": [{"file": name, **row} for name, row in sorted(state["files"].items())]}


def _zip(file: Path) -> dict:
    try:
        result = share.import_pack(str(file), None, overwrite=False)
    except (ValueError, OSError) as error:
        return {"status": "failed", "reason": str(error)[:500]}
    imported = [row.get("id") for row in result.get("imported") or []]
    skipped = "；".join(str(row.get("reason")) for row in result.get("skipped") or [])[:500]
    return {"status": "imported" if imported else "failed" if result.get("errors") else "skipped",
            "projects": imported, "cases": [], "reason": skipped}


def _bundle(file: Path, relative: str, digest: str) -> dict:
    try:
        value = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return {"status": "failed", "reason": "不是有效的 JSON：" + str(error)[:200]}
    if not isinstance(value, dict) or not isinstance(value.get("project_id"), str) or not isinstance(value.get("cases"), list):
        return {"status": "failed", "reason": "JSON 用例文件需要是对象：{project_id, cases, status?}"}
    project_id, marked = value["project_id"], str(value.get("status") or "").strip().lower()
    if marked not in PASS:
        return {"status": "skipped", "project_id": project_id, "reason": f"文件状态是「{marked}」，只同步通过状态的用例"}
    try:
        projects.path(project_id)
    except ValueError as error:
        return {"status": "failed", "project_id": project_id, "reason": str(error)}
    incoming = []
    for case in value["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            return {"status": "failed", "project_id": project_id, "reason": "每条用例都需要 id"}
        item = copy.deepcopy(case)
        item.setdefault("lifecycle", "active")
        item.setdefault("revision", 1)
        item.setdefault("extensions", {})["sync"] = {"file": relative, "sha256": digest, "synced_at": db.now()}
        incoming.append(item)
    if not incoming:
        return {"status": "skipped", "project_id": project_id, "reason": "文件中没有用例"}
    try:
        projects.save_cases(project_id, incoming)
    except (ValueError, KeyError, OSError, TypeError) as error:
        return {"status": "failed", "project_id": project_id, "reason": str(error)[:500]}
    return {"status": "imported", "project_id": project_id, "cases": [item["id"] for item in incoming], "reason": ""}


def scan() -> dict:
    """Import new or changed files once; unchanged files stay in the listing with their receipt."""
    folder = paths.sync_dir()
    if folder is None:
        return {"status": "not_configured", "sync_dir": None, "files": [],
                "hint": "还没有配置同步目录：设置环境变量 AUTOSKILL_SYNC_DIR，或在数据目录设置里填写 sync_dir"}
    if not folder.is_dir():
        return {"status": "missing", "sync_dir": str(folder), "files": [], "hint": "同步目录不存在，等待开发侧同步后重试"}
    with named_lock("sync", "scan"):
        state = _read()
        results = []
        for file in sorted(folder.rglob("*")):
            if not file.is_file() or file.suffix.lower() not in (".json", ".zip"):
                continue
            relative = file.relative_to(folder).as_posix()
            try:
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
            except OSError as error:
                results.append({"file": relative, "status": "failed", "reason": str(error)[:200]})
                continue
            previous = state["files"].get(relative)
            if previous and previous.get("sha256") == digest:
                results.append({**previous, "unchanged": True})
                continue
            receipt = _zip(file) if file.suffix.lower() == ".zip" else _bundle(file, relative, digest)
            receipt.update(file=relative, sha256=digest, at=db.now())
            state["files"][relative] = receipt
            results.append(receipt)
        state["last_scan"] = {"at": db.now(), "path": str(folder), "files": len(results),
                              "imported": sum(row.get("status") == "imported" for row in results),
                              "failed": sum(row.get("status") == "failed" for row in results)}
        execution_inputs.write_json(_file(), state)
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
