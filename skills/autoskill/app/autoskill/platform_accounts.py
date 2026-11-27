"""Plain-text shared platform and multi-account store under workspace/.local."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import db, paths
from .core import store
from .locking import named_lock

SCHEMA = 1
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
AUTH_MODES = ("none", "account_password")


def _empty() -> dict:
    return {"schema": SCHEMA, "platforms": {}, "accounts": {}}


def _validate_id(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or not ID_RE.fullmatch(text):
        raise ValueError(f"{label}标识只能使用字母、数字、短横线和下划线")
    return text


def _clean_variables(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("账号变量必须是 JSON 对象")
    result = {}
    for name, item in value.items():
        if not isinstance(name, str) or not VARIABLE_NAME.fullmatch(name):
            raise ValueError("账号变量名只能使用字母、数字和下划线，且不能以数字开头")
        if isinstance(item, (str, int, float)):
            result[name] = item
        else:
            raise ValueError(f"账号变量 {name} 只能是文字、数字或布尔值")
    return result


def _normalize_platform(platform_id: str, data: dict, existing: dict | None = None) -> dict:
    base = dict(existing or {})
    if not isinstance(data, dict):
        raise ValueError("平台配置必须是 JSON 对象")
    name = str(data.get("name") if "name" in data else base.get("name") or platform_id).strip()
    if not name:
        raise ValueError("平台名称不能为空")
    auth_mode = str(data.get("auth_mode") if "auth_mode" in data else base.get("auth_mode") or "account_password").strip()
    if auth_mode not in AUTH_MODES:
        raise ValueError("平台登录方式只能是 none 或 account_password")
    login_url = str(data.get("login_url") if "login_url" in data else base.get("login_url") or "").strip()
    login_steps = data.get("login_steps") if "login_steps" in data else base.get("login_steps", [])
    if not isinstance(login_steps, list):
        raise ValueError("login_steps 必须是数组")
    login_api = data.get("login_api") if "login_api" in data else base.get("login_api", {})
    if not isinstance(login_api, dict):
        raise ValueError("login_api 必须是对象")
    return {
        "id": platform_id,
        "name": name,
        "login_url": login_url,
        "auth_mode": auth_mode,
        "login_steps": copy.deepcopy(login_steps),
        "login_api": copy.deepcopy(login_api),
        "updated_at": db.now(),
    }


def _normalize_account(account_id: str, data: dict, platforms: dict, existing: dict | None = None) -> dict:
    base = dict(existing or {})
    if not isinstance(data, dict):
        raise ValueError("账号配置必须是 JSON 对象")
    platform_id = str(data.get("platform_id") if "platform_id" in data else base.get("platform_id") or "").strip()
    if platform_id:
        _validate_id(platform_id, "平台")
    platform = platforms.get(platform_id) if platform_id else None
    platform_name = str(
        data.get("platform_name")
        if "platform_name" in data
        else (platform["name"] if isinstance(platform, dict) and platform.get("name") else base.get("platform_name") or "")
    ).strip()
    environment_key = str(data.get("environment_key") if "environment_key" in data else base.get("environment_key") or "").strip()
    environment_name = str(data.get("environment_name") if "environment_name" in data else base.get("environment_name") or "").strip()
    username = str(data.get("username") if "username" in data else base.get("username") or "").strip()
    password_raw = data.get("password") if "password" in data else base.get("password", "")
    if password_raw is None:
        password = ""
    elif isinstance(password_raw, str):
        password = password_raw
    else:
        raise ValueError("账号密码必须是文本")
    label = str(data.get("label") if "label" in data else base.get("label") or username or account_id).strip()
    if not label:
        label = username or account_id
    variables = _clean_variables(data.get("variables") if "variables" in data else base.get("variables"))
    return {
        "id": account_id,
        "platform_id": platform_id,
        "platform_name": platform_name,
        "environment_key": environment_key,
        "environment_name": environment_name,
        "label": label,
        "username": username,
        "password": password,
        "variables": variables,
        "updated_at": db.now(),
    }


def _read_raw(file: Path) -> dict:
    if not file.is_file():
        return _empty()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    platforms = raw.get("platforms") if isinstance(raw.get("platforms"), dict) else {}
    accounts = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
    return {"schema": int(raw.get("schema") or SCHEMA), "platforms": platforms, "accounts": accounts}


def load() -> dict:
    """Read the plain-text platform and account catalog without creating files."""
    with named_lock("platform-accounts", "workspace"):
        return copy.deepcopy(_read_raw(paths.platform_accounts_file()))


def save(data: dict) -> dict:
    """Persist the full catalog atomically."""
    if not isinstance(data, dict):
        raise ValueError("平台账号库必须是 JSON 对象")
    normalized = {
        "schema": SCHEMA,
        "platforms": copy.deepcopy(data.get("platforms") if isinstance(data.get("platforms"), dict) else {}),
        "accounts": copy.deepcopy(data.get("accounts") if isinstance(data.get("accounts"), dict) else {}),
    }
    with named_lock("platform-accounts", "workspace"):
        target = paths.local() / "platform-accounts.json"
        store.write(target, normalized)
    return normalized


def redact_account(account: dict | None) -> dict | None:
    """Strip plain-text passwords before returning account data to list views or evidence."""
    if account is None:
        return None
    cleaned = copy.deepcopy(account)
    cleaned.pop("password", None)
    return cleaned


def find_shared_projects() -> dict[str, list[dict]]:
    """Map each account_id to the projects referencing it across environments."""
    refs: dict[str, dict[str, dict]] = {}
    root = paths.workspace() / "projects"
    if not root.is_dir():
        return {}
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        meta_file = folder / "project.json"
        cfg_file = folder / "qa-config.json"
        if not meta_file.is_file() or not cfg_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        project_id = folder.name
        project_name = str(meta.get("name") or project_id)
        for env in cfg.get("environments") or []:
            if not isinstance(env, dict):
                continue
            account_ids = list(env.get("account_ids") or [])
            default_id = env.get("default_account_id")
            if default_id and default_id not in account_ids:
                account_ids.append(default_id)
            for account_id in account_ids:
                if not isinstance(account_id, str) or not account_id:
                    continue
                by_project = refs.setdefault(account_id, {})
                entry = by_project.setdefault(project_id, {"id": project_id, "name": project_name, "environment_ids": []})
                env_id = env.get("id")
                if env_id and env_id not in entry["environment_ids"]:
                    entry["environment_ids"].append(env_id)
    return {account_id: [by_project[pid] for pid in sorted(by_project)] for account_id, by_project in refs.items()}


def shared_projects_for(account_id: str) -> list[dict]:
    return find_shared_projects().get(account_id, [])


def get_platform(platform_id: str) -> dict | None:
    if not platform_id:
        return None
    data = load()
    found = data["platforms"].get(platform_id)
    return copy.deepcopy(found) if isinstance(found, dict) else None


def list_platforms() -> list[dict]:
    data = load()
    return [copy.deepcopy(data["platforms"][key]) for key in sorted(data["platforms"]) if isinstance(data["platforms"][key], dict)]


def upsert_platform(platform_id: str, payload: dict) -> dict:
    identity = _validate_id(platform_id, "平台")
    with named_lock("platform-accounts", "workspace"):
        file = paths.local() / "platform-accounts.json"
        data = _read_raw(file)
        existing = data["platforms"].get(identity)
        record = _normalize_platform(identity, payload, existing)
        data["platforms"][identity] = record
        # Keep cached platform_name synchronized on accounts belonging to this platform.
        for account in data["accounts"].values():
            if isinstance(account, dict) and account.get("platform_id") == identity:
                account["platform_name"] = record["name"]
        store.write(file, data)
        return copy.deepcopy(record)


def get_account(account_id: str, *, include_password: bool = True) -> dict | None:
    if not account_id:
        return None
    data = load()
    found = data["accounts"].get(account_id)
    if not isinstance(found, dict):
        return None
    account = copy.deepcopy(found)
    platform = data["platforms"].get(account.get("platform_id") or "")
    if isinstance(platform, dict) and platform.get("name") and not account.get("platform_name"):
        account["platform_name"] = platform["name"]
    return account if include_password else redact_account(account)


def list_accounts(*, include_password: bool = True, include_shared: bool = True) -> list[dict]:
    data = load()
    shared = find_shared_projects() if include_shared else {}
    items = []
    for account_id in sorted(data["accounts"]):
        raw = data["accounts"][account_id]
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        platform = data["platforms"].get(item.get("platform_id") or "")
        if isinstance(platform, dict) and platform.get("name") and not item.get("platform_name"):
            item["platform_name"] = platform["name"]
        if not include_password:
            item.pop("password", None)
        if include_shared:
            item["shared_projects"] = shared.get(account_id, [])
        items.append(item)
    return items


def upsert_account(account_id: str, payload: dict) -> dict:
    identity = _validate_id(account_id, "账号")
    with named_lock("platform-accounts", "workspace"):
        file = paths.local() / "platform-accounts.json"
        data = _read_raw(file)
        existing = data["accounts"].get(identity)
        record = _normalize_account(identity, payload, data["platforms"], existing)
        # Auto-register minimal platform stub when a new platform_id is supplied with platform_name.
        pid = record.get("platform_id")
        if pid and pid not in data["platforms"]:
            data["platforms"][pid] = _normalize_platform(
                pid,
                {"name": record.get("platform_name") or pid, "auth_mode": "account_password"},
            )
        data["accounts"][identity] = record
        store.write(file, data)
    return copy.deepcopy(record)


def delete_account(account_id: str) -> bool:
    identity = _validate_id(account_id, "账号")
    with named_lock("platform-accounts", "workspace"):
        file = paths.local() / "platform-accounts.json"
        data = _read_raw(file)
        existed = identity in data["accounts"]
        if existed:
            del data["accounts"][identity]
            store.write(file, data)
        return existed


def resolve_accounts(account_ids: list[str], *, include_password: bool = False, include_shared: bool = False,
                     environment_name: str = "", platform_name: str = "") -> list[dict]:
    """Hydrate referenced account ids in their configured order."""
    if not account_ids:
        return []
    data = load()
    shared = find_shared_projects() if include_shared else {}
    resolved = []
    for account_id in account_ids:
        raw = data["accounts"].get(account_id)
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        platform = data["platforms"].get(item.get("platform_id") or "")
        if isinstance(platform, dict) and platform.get("name"):
            item["platform_name"] = item.get("platform_name") or platform["name"]
        elif platform_name and not item.get("platform_name"):
            item["platform_name"] = platform_name
        if environment_name and not item.get("environment_name"):
            item["environment_name"] = environment_name
        if not include_password:
            item.pop("password", None)
        if include_shared:
            item["shared_projects"] = shared.get(account_id, [])
        resolved.append(item)
    return resolved


def summary_for_result(account: dict | None, *, mode: str = "none") -> dict:
    """Password-free account descriptor stored in execution manifests and results.detail."""
    if mode == "none" or account is None:
        return {"mode": mode, "account_id": "", "label": "不登录" if mode == "none" else "", "username": "", "platform_id": ""}
    return {
        "mode": mode,
        "account_id": str(account.get("id") or account.get("account_id") or ""),
        "label": str(account.get("label") or account.get("username") or account.get("id") or ""),
        "username": str(account.get("username") or ""),
        "platform_id": str(account.get("platform_id") or ""),
    }
