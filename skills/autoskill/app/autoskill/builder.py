"""Build complete case objects from the few fields a recording or a person provides."""
from __future__ import annotations

from .core import store
from .replay import ir


def _common(case_id: str, kind: str, title: str, module: str, steps: list[str], expected: str, risk: str,
            source: str, depends_on: list[str] | None, category: str, tags: list[str] | None) -> dict:
    extensions: dict = {}
    if depends_on:
        extensions["depends_on"] = list(depends_on)
    if category:
        extensions["category"] = category
    return {
        "schema_version": store.VERSION, "id": case_id, "kind": kind, "revision": 1, "lifecycle": "active",
        "requirement_ids": ["REQ-RECORDED"], "module": module or "未分组", "title": title, "priority": "P1",
        "preconditions": "前置用例：" + "、".join(depends_on) if depends_on else "无",
        "steps": steps or ["打开页面"], "test_data": "仅合成数据；凭据以 ${ENV:名称} 引用",
        "expected": expected or "全部动作可以完成", "source": source, "risk": risk,
        "tags": list(tags or []), "extensions": extensions,
    }


def ui_case(case_id: str, title: str, automation: dict, *, module: str = "", risk: str = "write",
            depends_on: list[str] | None = None, category: str = "", source: str = "autoskill record",
            tags: list[str] | None = None) -> dict:
    ir.validate(automation)
    steps = ["打开 " + automation["start_url"], *[ir.describe(s) for s in automation.get("actions", [])]]
    expected = "；".join(s.get("name") or ir.describe(s) for s in automation.get("assertions", []))
    case = _common(case_id, "ui", title, module, steps, expected, risk, source, depends_on, category, tags)
    case["extensions"]["automation"] = automation
    return case


def api_case(case_id: str, title: str, method: str, path: str, assertions: dict, *, module: str = "",
             headers: dict | None = None, params: dict | None = None, json: object = None,
             extract: dict | None = None, depends_on: list[str] | None = None, category: str = "",
             source: str = "autoskill record", api_source: str = "browser", risk: str | None = None,
             tags: list[str] | None = None, traffic: str = "business") -> dict:
    if risk is None:
        risk = "read" if method in ("GET", "HEAD", "OPTIONS") else "delete" if method == "DELETE" else "write"
    expected = f'HTTP {assertions["status"]}' + ("，并满足录制到的字段与结构" if len(assertions) > 1 else "")
    case = _common(case_id, "api", title, module, [f"{method} {path}"], expected, risk, source, depends_on, category, tags)
    case.update(api_source=api_source, source_ref=source, method=method, path=path, assertions=assertions)
    if headers:
        case["headers"] = headers
    if params:
        case["params"] = params
    if json is not None:
        case["json"] = json
    if extract:
        case["extensions"]["extract"] = extract
    case["extensions"]["traffic"] = traffic  # business | resource (page and static files)
    return case
