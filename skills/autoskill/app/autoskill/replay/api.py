"""HTTP replay of API cases, in order, with variables passed between requests.

`{{name}}` is filled from values earlier cases extracted (or from the saved
login session); `${ENV:NAME}` comes from the machine-local secret store.
Redirects are never followed and nothing is retried.
"""
from __future__ import annotations

import json
import os
import re
import time

from .. import paths, secrets
from ..core import store
from . import ir

VAR = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
SENSITIVE = re.compile(r"(authorization|cookie|token|secret|password|passwd|api[-_]?key|credential|session)", re.IGNORECASE)
MAX_BODY = 64 * 1024


def fill(value, variables: dict):
    """Substitute {{name}}; a string that is exactly one reference keeps the variable's type."""
    if isinstance(value, str):
        whole = VAR.fullmatch(value)
        if whole:
            if whole[1] not in variables:
                raise KeyError(whole[1])
            return variables[whole[1]]

        def one(match):
            if match[1] not in variables:
                raise KeyError(match[1])
            return str(variables[match[1]])

        return VAR.sub(one, value)
    if isinstance(value, dict):
        return {k: fill(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, variables) for v in value]
    return value


def redact(value, key: str = ""):
    """Hide secrets before anything is written to disk or shown in the dashboard."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key and SENSITIVE.search(key) and value not in ("", None):
        return "<已隐藏>"
    return value



def _body_preview(response) -> tuple[object, str]:
    """(parsed JSON or None, text preview) with secrets hidden and size capped."""
    text = response.text if len(response.content) <= MAX_BODY else response.text[:MAX_BODY]
    try:
        parsed = response.json()
    except ValueError:
        return None, text
    return parsed, json.dumps(redact(parsed), ensure_ascii=False, indent=2)[:MAX_BODY]


def _checks(case: dict, response, parsed, elapsed_ms: int) -> list[dict]:
    import jsonschema

    rules = case["assertions"]
    rows = [{"name": "状态码", "passed": response.status_code == rules["status"],
             "expected": rules["status"], "actual": response.status_code}]
    for name, wanted in rules.get("header_contains", {}).items():
        actual = response.headers.get(name, "")
        rows.append({"name": f"响应头 {name} 包含", "passed": str(wanted).lower() in actual.lower(),
                     "expected": wanted, "actual": actual})
    rows.extend({"name": "响应内容包含", "passed": wanted in response.text, "expected": wanted,
                 "actual": "已包含" if wanted in response.text else "未包含"}
                for wanted in rules.get("body_contains", []))
    if "max_ms" in rules:
        rows.append({"name": "响应时间不超过(ms)", "passed": elapsed_ms <= rules["max_ms"],
                     "expected": rules["max_ms"], "actual": elapsed_ms})
    if rules.get("json_equals") or "schema" in rules:
        if parsed is None:
            rows.append({"name": "响应是 JSON", "passed": False, "expected": "JSON", "actual": response.headers.get("content-type", "")})
            return rows
        for pointer, wanted in rules.get("json_equals", {}).items():
            try:
                actual = store.pointer(parsed, pointer)
                passed = json.dumps(actual, sort_keys=True) == json.dumps(wanted, sort_keys=True)
            except (KeyError, IndexError, TypeError, ValueError):
                actual, passed = "字段不存在", False
            rows.append({"name": f"字段 {pointer or '/'}", "passed": passed, "expected": wanted, "actual": redact(actual, pointer)})
        if "schema" in rules:
            errors = list(jsonschema.Draft202012Validator(rules["schema"]).iter_errors(parsed))
            detail = "符合" if not errors else f"{errors[0].json_path}: {errors[0].message}"[:200]
            rows.append({"name": "响应结构", "passed": not errors, "expected": "符合录制时的结构", "actual": detail})
    return rows


def new_client(config: dict, state: dict | None = None):
    """Keep run cookies and use the system's proxy, NO_PROXY and CA configuration."""
    import httpx

    paths.configure_http_environment()
    client = httpx.Client(timeout=float(config.get("timeout_seconds", 20)), follow_redirects=False, trust_env=True,
                          verify=not config.get("ignore_https_errors"))
    for cookie in (state or {}).get("cookies", []):
        client.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", "").lstrip("."), path=cookie.get("path", "/"))
    return client


def run_case(client, case: dict, config: dict, variables: dict) -> dict:
    """Send one API case and judge it. `variables` is updated with anything the case extracts."""
    import httpx

    outcome = {"status": "Blocked", "actual": "", "reason": "", "category": "", "assertions": [],
               "request": None, "response": None, "duration_ms": 0, "extracted": []}
    variables.setdefault("RAND", os.urandom(3).hex())
    try:
        filled = fill({k: case[k] for k in ("path", "headers", "params", "json", "form") if k in case}, variables)
        url = store.request_url(config, {"path": filled["path"]})
        ir.check_origin(url, list(config.get("allowed_origins", [])))
        sent = secrets.resolve({k: filled[k] for k in ("headers", "params", "json", "form") if k in filled})
        if "form" in sent:
            sent["data"] = sent.pop("form")
        try:  # an expected value may name what this run created, for example a title that ends in {{RAND}}
            expected = fill(case["assertions"].get("json_equals", {}), variables)
            if expected:
                case = {**case, "assertions": {**case["assertions"], "json_equals": expected}}
        except KeyError:
            pass
    except KeyError as missing:
        outcome.update(reason=f"缺少变量 {{{{{missing.args[0]}}}}}：提供它的前置用例没有成功", category="missing_variable")
        return outcome
    except PermissionError as error:
        outcome.update(reason=str(error), category="unauthorized_origin" if "授权范围" in str(error) else "not_permitted")
        return outcome
    except ValueError as error:
        outcome.update(reason=str(error), category="missing_secret" if "缺少凭据" in str(error) else "environment")
        return outcome

    # what is stored and shown: the case as written (references intact), never resolved secrets
    outcome["request"] = {"method": case["method"], "url": url, "headers": redact(filled.get("headers", {})),
                          "params": redact(filled.get("params", {})), "json": redact(filled.get("json")),
                          **({"form": redact(filled["form"])} if "form" in filled else {})}
    if case.get("extensions", {}).get("traffic") == "resource":
        sent["timeout"] = float(config.get("page_load_seconds", 90))  # a page's own files may be as slow as the page
    clock = time.monotonic()
    try:
        response = client.request(case["method"], url, **sent)
    except httpx.TimeoutException:
        outcome.update(reason="请求超时，目标服务没有在限定时间内响应", category="timeout",
                       duration_ms=round((time.monotonic() - clock) * 1000))
        return outcome
    except httpx.HTTPError as error:
        outcome.update(reason="无法连接目标服务：" + type(error).__name__, category="environment",
                       duration_ms=round((time.monotonic() - clock) * 1000))
        return outcome
    elapsed = round((time.monotonic() - clock) * 1000)
    parsed, preview = _body_preview(response)
    outcome["response"] = {"status": response.status_code, "headers": redact(dict(response.headers)),
                           "body": preview, "bytes": len(response.content)}
    outcome["duration_ms"] = elapsed
    outcome["assertions"] = rows = _checks(case, response, parsed, elapsed)
    failed = [row for row in rows if not row["passed"]]
    if failed:
        expired = response.status_code in (401, 403) and case["assertions"]["status"] not in (401, 403)
        outcome.update(status="Fail", category="session_expired" if expired else "assertion_failed",
                       actual=f"HTTP {response.status_code}，{elapsed} ms",
                       reason="；".join(f'{row["name"]}：期望 {row["expected"]}，实际 {row["actual"]}' for row in failed)[:500])
        return outcome
    outcome.update(status="Pass", actual=f"HTTP {response.status_code}，{elapsed} ms，{len(rows)} 条断言通过")
    for name, pointer in case.get("extensions", {}).get("extract", {}).items():
        try:
            variables[name] = store.pointer(parsed, pointer)
            outcome["extracted"].append(name)
        except (KeyError, IndexError, TypeError, ValueError):
            outcome.update(status="Fail", category="assertion_failed",
                           reason=f"响应里没有后续用例需要的字段 {pointer}（变量 {name}）")
            outcome["assertions"].append({"name": f"提取变量 {name}", "passed": False, "expected": pointer, "actual": "字段不存在"})
            break
    return outcome
