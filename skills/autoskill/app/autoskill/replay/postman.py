"""Export API cases as a Postman Collection v2.1 plus an Environment.

Requests keep the order they are given in (prerequisites first). `{{name}}`
variables are the same syntax Postman uses; `${ENV:NAME}` secrets become empty
environment variables, so no secret value ever leaves the machine.
"""
from __future__ import annotations

import json
import re
import uuid
from urllib.parse import quote

SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
ENV_REF = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
AT = ("const __at = (o, p) => p === '' ? o : p.slice(1).split('/')"
      ".reduce((v, k) => v == null ? undefined : v[k.replace(/~1/g, '/').replace(/~0/g, '~')], o);")
FILL = r'''const __value = (name) => {
  const value = pm.variables.get(name);
  if (value === undefined) throw new Error("缺少变量：" + name);
  const typed = pm.collectionVariables.get("__autoskill_json_" + name);
  if (typed !== undefined && typed !== null) {
    const original = JSON.parse(typed);
    const serialized = (item) => item !== null && typeof item === "object" ? JSON.stringify(item) : String(item);
    if (serialized(original) === serialized(value)) return original;
  }
  return value;
};
const __fill = (value) => {
  if (typeof value === "string") {
    const whole = value.match(/^\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}$/);
    if (whole) return __value(whole[1]);
    return value.replace(/\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}/g, (_, name) => String(__value(name)));
  }
  if (Array.isArray(value)) return value.map(__fill);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, __fill(item)]));
  }
  return value;
};'''


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _convert(value, secrets_seen: set):
    """Turn ${ENV:NAME} into {{NAME}} and remember the name."""
    if isinstance(value, str):
        secrets_seen.update(ENV_REF.findall(value))
        return ENV_REF.sub(r"{{\1}}", value)
    if isinstance(value, dict):
        return {k: _convert(v, secrets_seen) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(v, secrets_seen) for v in value]
    return value


def test_script(case: dict) -> list[str]:
    rules = case["assertions"]
    extract = case.get("extensions", {}).get("extract", {})
    lines = [FILL, "const __expected = __fill(" + _js(rules.get("json_equals", {})) + ");",
             f'pm.test("状态码为 {rules["status"]}", () => pm.response.to.have.status({rules["status"]}));']
    for name, wanted in rules.get("header_contains", {}).items():
        lines.append(f"pm.test({_js('响应头 ' + name + ' 包含 ' + str(wanted))}, () => "
                     f"pm.expect((pm.response.headers.get({_js(name)}) || '').toLowerCase()).to.include({_js(str(wanted).lower())}));")
    lines.extend(f"pm.test({_js('响应内容包含 ' + wanted[:40])}, () => pm.expect(pm.response.text()).to.include({_js(wanted)}));"
                 for wanted in rules.get("body_contains", []))
    if "max_ms" in rules:
        lines.append(f'pm.test("响应时间不超过 {rules["max_ms"]} ms", () => pm.expect(pm.response.responseTime).to.be.at.most({rules["max_ms"]}));')
    if rules.get("json_equals") or "schema" in rules or extract:
        lines += [AT, "let __body;", 'pm.test("响应是 JSON", () => { __body = pm.response.json(); });']
        for pointer in rules.get("json_equals", {}):
            lines.append(f"pm.test({_js('字段 ' + (pointer or '/'))}, () => pm.expect(__at(__body, {_js(pointer)})).to.eql(__expected[{_js(pointer)}]));")
        if "schema" in rules:
            lines.append(f'pm.test("响应结构", () => pm.response.to.have.jsonSchema({_js(rules["schema"])}));')
        for name, pointer in extract.items():
            lines.append(f"pm.test({_js('提取变量 ' + name)}, () => {{ const v = __at(__body, {_js(pointer)}); "
                         f"pm.expect(v).to.not.equal(undefined); pm.collectionVariables.set({_js(name)}, v); "
                         f"pm.collectionVariables.set({_js('__autoskill_json_' + name)}, JSON.stringify(v)); }});")
    return lines


def _fields(values: dict, secrets_seen: set) -> list[dict]:
    """Keep repeated query and form fields as distinct Postman entries."""
    converted = _convert(values, secrets_seen)
    return [{"key": key, "value": "" if item is None else item if isinstance(item, str) else _js(item)}
            for key, value in converted.items() for item in (value if isinstance(value, list) else [value])]


def _item(case: dict, secrets_seen: set) -> dict:
    case = _convert(case, secrets_seen)
    path = _convert(case["path"], secrets_seen)
    query = _fields(case.get("params", {}), secrets_seen)
    encoded = "&".join(quote(q["key"], safe="{}") + "=" + quote(q["value"], safe="{}") for q in query)
    raw = "{{base_url}}" + path + ("?" + encoded if query else "")
    request = {
        "method": case["method"],
        "header": [{"key": k, "value": v} for k, v in _convert(case.get("headers", {}), secrets_seen).items()],
        "url": {"raw": raw, "host": ["{{base_url}}"], "path": [part for part in path.split("/") if part], "query": query},
        "description": case.get("expected", ""),
    }
    if "json" in case and case["json"] is not None:
        if not any(h["key"].lower() == "content-type" for h in request["header"]):
            request["header"].append({"key": "Content-Type", "value": "application/json"})
        body = json.dumps(_convert(case["json"], secrets_seen), ensure_ascii=False, indent=2)
        request["body"] = {"mode": "raw", "raw": body, "options": {"raw": {"language": "json"}}}
    elif "form" in case:
        request["body"] = {"mode": "urlencoded", "urlencoded": _fields(case["form"], secrets_seen)}
    prepare = [FILL, "__fill(" + _js(case["assertions"].get("json_equals", {})) + ");"]
    if case.get("json") is not None:
        prepare.append("pm.request.body.update(JSON.stringify(__fill(" + _js(case["json"]) + ")));")
    events = [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": prepare}},
              {"listen": "test", "script": {"type": "text/javascript", "exec": test_script(case)}}]
    return {
        "id": str(uuid.uuid4()),
        "name": f'{case["id"]} {case["title"]}',
        "request": request,
        "event": events,
    }


def export(name: str, cases: list[dict], base_url: str) -> tuple[dict, dict]:
    """Return (collection, environment) for the given API cases, in the given order."""
    secrets_seen: set[str] = set()
    items = [_item(case, secrets_seen) for case in cases if case.get("kind") == "api"]
    extracted = sorted({k for case in cases for k in case.get("extensions", {}).get("extract", {})})
    collection = {
        "info": {"_postman_id": str(uuid.uuid4()), "name": name, "schema": SCHEMA,
                 "description": "由 autoSkill 根据录制的接口流量生成。按顺序运行：前面的请求会为后面的请求提供变量。\n"
                                "观测基线不是独立业务预期。差异：仅包含接口步骤；Cookie、登录续期、失败后续跑及 JSON Schema 方言由 Postman 运行时解释。"},
        "item": items,
        "variable": [{"key": key, "value": ""} for key in extracted],
    }
    if items:
        initialization = [f"if (pm.info.requestId === {_js(items[0]['id'])} || !pm.collectionVariables.get('RAND')) {{",
                          "  pm.collectionVariables.set('RAND', pm.variables.replaceIn('{{$guid}}').replace(/-/g, '').slice(0, 12));"]
        initialization += [f"  pm.collectionVariables.unset({_js('__autoskill_json_' + key)});" for key in extracted]
        initialization += [f"  pm.collectionVariables.unset({_js(key)});" for key in extracted]
        initialization.append("}")
        collection["event"] = [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": initialization}}]
    values = [{"key": "base_url", "value": base_url.rstrip("/"), "type": "default", "enabled": True}]
    values += [{"key": key, "value": "", "type": "secret", "enabled": True} for key in sorted(secrets_seen)]
    environment = {"id": str(uuid.uuid4()), "name": name + " 环境", "values": values,
                   "_postman_variable_scope": "environment"}
    return collection, environment
