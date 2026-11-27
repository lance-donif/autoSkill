"""Saved login state per project or scoped platform account.

Kept as plain JSON in workspace/.local and included in full skill sharing packs.
Platform-scoped logins isolate storage by (platform_id, environment_key, account_id)
so multiple accounts never overwrite one another and distinct platforms never collide.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import db, paths, secrets
from .core import store
from .locking import named_lock


def account_lock_key(*, platform_id: str = "", environment_id: str = "", account_id: str = "") -> str:
    """Queue serialization key for a shared platform account, or empty when not shared."""
    pid = str(platform_id or "").strip()
    aid = str(account_id or "").strip()
    if pid and aid:
        env_key = str(environment_id or "").strip() or "default"
        return f"platform:{pid}:{env_key}:{aid}"
    return ""


def _scope(project_id: str, *, platform_id: str = "", environment_id: str = "", account_id: str = "") -> tuple[Path, str]:
    root = paths.local() / "sessions"
    pid = str(platform_id or "").strip()
    env_key = str(environment_id or "").strip()
    aid = str(account_id or "").strip()
    if pid and aid:
        key = f"platform:{pid}:{env_key or 'default'}:{aid}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        folder = root / "platform"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{digest}.json", f"platform:{digest}"
    if aid:
        key = f"scoped:{project_id}:{env_key or 'default'}:{aid}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        folder = root / "scoped"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{digest}.json", f"scoped:{digest}"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{project_id}.json", project_id


def _file(project_id: str, *, platform_id: str = "", environment_id: str = "", account_id: str = "") -> Path:
    file, _lock = _scope(project_id, platform_id=platform_id, environment_id=environment_id, account_id=account_id)
    return file


def load(project_id: str, *, platform_id: str = "", environment_id: str = "", account_id: str = "") -> dict:
    file, lock_key = _scope(project_id, platform_id=platform_id, environment_id=environment_id, account_id=account_id)
    with named_lock("session", lock_key):
        if not file.is_file():
            return {}
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            return json.loads(secrets.unseal(data["sealed"])) if "sealed" in data else data  # older versions sealed it
        except (ValueError, KeyError, OSError):
            return {}  # unreadable on this machine or by this user: treat as logged out


def save(project_id: str, ui: dict | None = None, variables: dict | None = None, *,
         platform_id: str = "", environment_id: str = "", account_id: str = "") -> None:
    file, lock_key = _scope(project_id, platform_id=platform_id, environment_id=environment_id, account_id=account_id)
    with named_lock("session", lock_key):
        current = {}
        if file.is_file():
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
                current = json.loads(secrets.unseal(raw["sealed"])) if "sealed" in raw else raw
            except (ValueError, KeyError, OSError):
                current = {}
        if ui is not None:
            current["ui"] = ui
        if variables is not None:
            current["variables"] = {**current.get("variables", {}), **variables}
        if platform_id or account_id:
            current["scope"] = {
                "platform_id": str(platform_id or ""),
                "environment_id": str(environment_id or ""),
                "account_id": str(account_id or ""),
            }
        current["saved_at"] = db.now()
        store.write(file, current)


def clear(project_id: str, *, platform_id: str = "", environment_id: str = "", account_id: str = "") -> bool:
    file, lock_key = _scope(project_id, platform_id=platform_id, environment_id=environment_id, account_id=account_id)
    with named_lock("session", lock_key):
        existed = file.is_file()
        file.unlink(missing_ok=True)
        return existed


def summary(project_id: str, *, platform_id: str = "", environment_id: str = "", account_id: str = "") -> dict:
    """What is saved, without revealing any value."""
    current = load(project_id, platform_id=platform_id, environment_id=environment_id, account_id=account_id)
    return {
        "saved": bool(current),
        "saved_at": current.get("saved_at"),
        "browser": bool(current.get("ui")),
        "variables": sorted(current.get("variables", {})),
    }
