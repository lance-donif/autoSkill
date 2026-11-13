"""Build complete case objects from the few fields a recording or a person provides."""
from __future__ import annotations

from . import case_document
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
        "requirement_ids": ["REQ-RECORDED"], "module": module or "未分组", "title": case_document.functional_description({"title": title}), "priority": "P1",
        "preconditions": "【待确认：必要的功能前置条件】",
        "steps": steps or ["打开页面"], "test_data": "仅合成数据；凭据以 ${ENV:名称} 引用",
        "expected": expected or "全部动作可以完成", "source": source, "risk": risk,
        "tags": list(tags or []), "extensions": extensions,
    }


def ui_case(case_id: str, title: str, automation: dict, *, module: str = "", risk: str = "write",
            depends_on: list[str] | None = None, category: str = "", source: str = "autoskill record",
            tags: list[str] | None = None, test_case_document: dict | None = None) -> dict:
    ir.validate(automation)
    steps = [case_document.MISSING_STEPS]
    expected = "1. " + case_document.MISSING_EXPECTED
    case = _common(case_id, "ui", title, module, steps, expected, risk, source, depends_on, category, tags)
    case["extensions"]["automation"] = automation
    return case_document.apply(case, test_case_document)


def api_case(case_id: str, title: str, method: str, path: str, assertions: dict, *, module: str = "",
             headers: dict | None = None, params: dict | None = None, json: object = None, form: dict | None = None,
             extract: dict | None = None, depends_on: list[str] | None = None, category: str = "",
             source: str = "autoskill record", api_source: str = "browser", risk: str | None = None,
             tags: list[str] | None = None, traffic: str = "business", test_case_document: dict | None = None) -> dict:
    if risk is None:
        risk = "read" if method in ("GET", "HEAD", "OPTIONS") else "delete" if method == "DELETE" else "write"
    case = _common(case_id, "api", title, module, [case_document.MISSING_STEPS], "1. " + case_document.MISSING_EXPECTED,
                   risk, source, depends_on, category, tags)
    case.update(api_source=api_source, source_ref=source, method=method, path=path, assertions=assertions)
    if headers:
        case["headers"] = headers
    if params:
        case["params"] = params
    if json is not None:
        case["json"] = json
    if form:
        case["form"] = form  # an ordinary form post (application/x-www-form-urlencoded)
    if extract:
        case["extensions"]["extract"] = extract
    case["extensions"]["traffic"] = traffic  # business | resource (page and static files)
    return case_document.apply(case, test_case_document)
