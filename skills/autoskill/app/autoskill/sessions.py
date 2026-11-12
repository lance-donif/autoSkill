"""Saved login state per project: browser cookies/storage and API variables.

Kept in workspace/.local, encrypted for the current Windows user, never shared.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db, paths, secrets


def _file(project_id: str) -> Path:
    folder = paths.local() / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{project_id}.json"


def load(project_id: str) -> dict:
    file = _file(project_id)
    if not file.is_file():
        return {}
    try:
        return json.loads(secrets.unseal(json.loads(file.read_text(encoding="utf-8"))["sealed"]))
    except (ValueError, KeyError, OSError):
        return {}  # unreadable on this machine or by this user: treat as logged out


def save(project_id: str, ui: dict | None = None, variables: dict | None = None) -> None:
    current = load(project_id)
    if ui is not None:
        current["ui"] = ui
    if variables is not None:
        current["variables"] = {**current.get("variables", {}), **variables}
    current["saved_at"] = db.now()
    sealed = secrets.seal(json.dumps(current, ensure_ascii=False))
    _file(project_id).write_text(json.dumps({"sealed": sealed}), encoding="utf-8")


def clear(project_id: str) -> bool:
    file = _file(project_id)
    existed = file.is_file()
    file.unlink(missing_ok=True)
    return existed


def summary(project_id: str) -> dict:
    """What is saved, without revealing any value."""
    current = load(project_id)
    return {
        "saved": bool(current),
        "saved_at": current.get("saved_at"),
        "browser": bool(current.get("ui")),
        "variables": sorted(current.get("variables", {})),
    }
