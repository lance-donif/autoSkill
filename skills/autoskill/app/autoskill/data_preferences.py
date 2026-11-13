"""Persist dashboard choices inside one data root, atomically consuming legacy browser imports."""
from __future__ import annotations

from . import data_store, paths
from .locking import named_lock


def read() -> dict:
    """Read the selected root without creating files or importing browser-local values."""
    metadata = data_store._metadata(paths.workspace())
    settings = paths.settings()
    dashboard = settings.get("dashboard", {})
    return {"data_id": metadata.get("data_id"), "dashboard": dashboard if isinstance(dashboard, dict) else {},
            "configured_fields": sorted(dashboard) if isinstance(dashboard, dict) else [], "browser": paths.browser_info(),
            "legacy_dashboard_import_eligible": bool(metadata.get("legacy_dashboard_import_eligible") and not settings.get("dashboard_legacy_imported"))}


def _dashboard(value) -> dict:
    if not isinstance(value, dict) or set(value) - {"theme", "engine", "headed"}:
        raise ValueError("dashboard 只接受 theme、engine 和 headed 设置")
    choices = {"theme": ("light", "dark"), "engine": ("playwright", "selenium")}
    for key, allowed in choices.items():
        if key in value and value[key] not in allowed:
            raise ValueError(key + " 设置无效")
    if "headed" in value and type(value["headed"]) is not bool:
        raise ValueError("headed 必须是布尔值")
    return value


def update(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - {"browser", "dashboard", "expected_data_id", "legacy_dashboard_import"}:
        raise ValueError("设置包含不支持的字段")
    if "browser" in body and body["browser"] not in ("system", "bundled"):
        raise ValueError("browser 只能是 system 或 bundled")
    dashboard = _dashboard(body["dashboard"]) if "dashboard" in body else None
    legacy = body.get("legacy_dashboard_import", False)
    if type(legacy) is not bool:
        raise ValueError("legacy_dashboard_import 必须是布尔值")
    with named_lock("settings", "workspace"):
        current = read()
        if "expected_data_id" in body and body["expected_data_id"] != current["data_id"]:
            raise data_store.DataError("data_changed", "数据目录已变化，请刷新页面后在当前数据目录重新选择偏好")
        values = {"browser": body["browser"]} if "browser" in body else {}
        if legacy:
            if current["legacy_dashboard_import_eligible"]:
                values.update(dashboard={key: value for key, value in (dashboard or {}).items() if key not in current["dashboard"]},
                              dashboard_legacy_imported=True)
        elif dashboard is not None:
            values["dashboard"] = dashboard
        if values:
            paths.save_settings(**values)
        return read()
