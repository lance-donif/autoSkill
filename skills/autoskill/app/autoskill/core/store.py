"""QA canonical store, independent API executor and export adapters."""

import argparse
import collections
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]  # the skill directory
VERSION = "1.1.0"
SUPPORTED_VERSIONS = ("1.0.0", "1.1.0")
STATES = ["Pass", "Fail", "Blocked", "Not Run"]
FILES = {"ui": "functional-cases.json", "api": "api-cases.json"}
COMMON = [
    ("schema_version", "格式版本"),
    ("id", "用例编号"),
    ("kind", "用例类型"),
    ("revision", "用例修订"),
    ("lifecycle", "生命周期"),
    ("module", "所属模块"),
    ("requirement_ids", "关联需求"),
    ("title", "用例标题"),
    ("priority", "优先级"),
    ("preconditions", "前置条件"),
    ("steps", "测试步骤"),
    ("test_data", "测试数据"),
    ("expected", "预期结果"),
    ("source", "来源"),
    ("risk", "执行风险"),
    ("tags", "标签"),
    ("extensions", "扩展字段"),
]
API_COLS = [
    ("api_source", "接口来源"),
    ("source_ref", "来源定位"),
    ("method", "请求方式"),
    ("path", "接口地址"),
    ("headers", "Headers"),
    ("params", "Query 参数"),
    ("json", "请求体"),
    ("assertions", "预期断言"),
]
RESULT_COLS = [
    ("status", "测试状态"),
    ("actual", "实际结果"),
    ("reason", "失败或阻塞原因"),
    ("evidence", "证据"),
    ("run_id", "运行编号"),
    ("case_revision", "执行用例修订"),
    ("started_at", "执行时间"),
    ("actual_assertions", "实际断言"),
]


def now():
    return dt.datetime.now(dt.UTC).isoformat()


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def load_cases(p):
    return [c for k in FILES for c in read(p / "testcases" / FILES[k])]


def load_results(p):
    return [r for kind in FILES for r in read(p / "execution" / f"{kind}-results.json")]


def safe_file(p, rel):
    q = Path(rel)
    if q.is_absolute() or ".." in q.parts:
        raise ValueError("证据必须使用无 .. 的相对路径")
    dest = (p / q).resolve()
    if not dest.is_relative_to(p.resolve()):
        raise ValueError("证据路径越界")
    return dest


def meaningful_schema(value):
    """Reject annotations, booleans true and unconstrained union branches."""
    if value is False:
        return True
    if not isinstance(value, dict) or not value:
        return False
    for key in ("anyOf", "oneOf"):
        if key in value and (not value[key] or not all(meaningful_schema(v) for v in value[key])):
            return False
    constraints = {
        "const",
        "enum",
        "required",
        "minProperties",
        "maxProperties",
        "minItems",
        "maxItems",
        "contains",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "not",
    }
    if any(key in value and value[key] not in ([], {}) for key in constraints):
        return True
    if value.get("additionalProperties") is False:
        return True
    if "type" in value and value["type"] not in ("object", ["object"]):
        return True
    if any(meaningful_schema(v) for v in value.get("allOf", [])):
        return True
    return any(key in value for key in ("anyOf", "oneOf"))


def external_schema_refs(value):
    refs = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("$ref", "$dynamicRef") and isinstance(item, str) and not item.startswith("#"):
                refs.append(item)
            refs.extend(external_schema_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(external_schema_refs(item))
    return refs


def valid_pointer(ptr):
    return isinstance(ptr, str) and (
        ptr == "" or (ptr.startswith("/") and re.search(r"~(?![01])", ptr) is None)
    )


SAFE_CREDENTIAL = re.compile(
    r"(?:(?:Bearer|Basic|Token)\s+)?(?:\$\{ENV:[A-Za-z_][A-Za-z0-9_]*\}|\{\{[A-Za-z_][A-Za-z0-9_]*\}\})"
)


def _credential_reference(value, normalized, path):
    """Cookie names are syntax; every cookie value must remain an unresolved reference."""
    if not isinstance(value, str):
        return False
    if SAFE_CREDENTIAL.fullmatch(value):
        return True
    if normalized != "cookie" or path != ("headers",):
        return False
    reference = r"(?:\$\{ENV:[A-Za-z_][A-Za-z0-9_]*\}|\{\{[A-Za-z_][A-Za-z0-9_]*\}\})"
    pair = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+=" + reference
    return all(re.fullmatch(pair, item.strip()) for item in value.split(";"))


def has_real_assertion(a):
    """A success expectation needs more than the status code, unless the response has no body by definition."""
    if a.get("status") in (204, 205) or 300 <= a.get("status", 0) < 400:
        return True
    return bool(
        a.get("json_equals") or a.get("body_contains") or a.get("header_contains") or meaningful_schema(a.get("schema"))
    )


def credential_errors(value, location="data", *, _path=()):
    sensitive = {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "token",
        "credential",
    }
    errors = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _path == ("assertions",) and key == "schema":
                continue  # Response schema property names describe fields; they are not credential values.
            if key == "extract" and isinstance(item, dict) and all(valid_pointer(v) for v in item.values()):
                continue  # variable name -> JSON pointer, not a credential
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            target = location + "." + key
            if (
                normalized in sensitive
                or normalized.endswith(("token", "secret", "apikey", "password", "cookie"))
            ) and item not in ("", None) and not _credential_reference(item, normalized, _path):
                errors.append(target + ": 敏感字段只能写成 ${ENV:名称} 或 {{变量}} 引用，不能写入真实值")
            errors.extend(credential_errors(item, target, _path=(*_path, key)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            errors.extend(credential_errors(item, f"{location}[{i}]", _path=(*_path, i)))
    return errors


def _validate_project_collections(project, reqs, case_lists, result_lists, errors):
    if not isinstance(project, dict):
        errors.append("project.json 必须是对象")
    elif project.get("schema_version") not in SUPPORTED_VERSIONS:
        errors.append("不支持的项目 schema_version")
    for label, items in [
        ("requirements", reqs),
        *case_lists.items(),
        *[(k + " results", v) for k, v in result_lists.items()],
    ]:
        if not isinstance(items, list):
            errors.append(label + ": 必须是数组")
        elif any(not isinstance(item, dict) for item in items):
            errors.append(label + ": 数组元素必须是对象")


def _validation_requirement_ids(reqs, errors):
    req_ids = set()
    for r in reqs:
        if not all(isinstance(r.get(x), str) and r[x].strip() for x in ["id", "module", "title", "source"]):
            errors.append("需求必填字段缺失")
            continue
        if r["id"] in req_ids:
            errors.append("需求 ID 重复")
        req_ids.add(r["id"])
    return req_ids


def _validate_api_contract(c, cid, errors, jsonschema):
    a = c["assertions"]
    if "schema" in a:
        try:
            jsonschema.Draft202012Validator.check_schema(a["schema"])
        except jsonschema.SchemaError:
            errors.append(f"{cid}: 响应 schema 非法")
        if not meaningful_schema(a["schema"]):
            errors.append(f"{cid}: 响应 schema 没有有效约束")
        if external_schema_refs(a["schema"]):
            errors.append(f"{cid}: 响应Schema必须先显式解析外部引用，禁止运行时自动访问")
    errors.extend(f"{cid}: 非法 JSON Pointer" for ptr in a.get("json_equals", {}) if not valid_pointer(ptr))


def _validate_case_catalog(case_lists, req_ids, schema, errors, jsonschema):
    byid = {}
    for kind, items in case_lists.items():
        for c in items:
            schema_errors = list(jsonschema.Draft202012Validator(schema).iter_errors(c))
            if schema_errors:
                errors.extend("用例结构错误: " + e.json_path + " / " + e.validator for e in schema_errors)
                continue
            cid = c["id"]
            errors.extend(credential_errors(c, cid))
            if c["kind"] != kind:
                errors.append(f"{cid}: 存储文件与 kind 不匹配")
            if cid in byid:
                errors.append(f"{cid}: 用例 ID 重复")
            byid[cid] = c
            errors.extend(f"{cid}: 未知需求 {rid}" for rid in c["requirement_ids"] if rid not in req_ids)
            if kind == "api":
                _validate_api_contract(c, cid, errors, jsonschema)
    return byid


def _validate_result_evidence(p, r, c, cid, status, errors):
    if r.get("case_content_sha256") != case_fingerprint(c):
        errors.append(f"{cid}: 用例内容已变化或缺少内容哈希，旧结果不能复用")
    checks = r["assertions"]
    if not r["actual"] or not r["evidence"] or not checks:
        errors.append(f"{cid}: 缺少实际结果、断言或证据")
    elif status == "Pass" and not all(a["passed"] for a in checks):
        errors.append(f"{cid}: Pass 包含失败断言")
    elif status == "Fail" and all(a["passed"] for a in checks):
        errors.append(f"{cid}: Fail 没有失败断言")
    for rel in r["evidence"]:
        try:
            if not safe_file(p, rel).is_file():
                errors.append(f"{cid}: 证据不存在")
        except (ValueError, TypeError):
            errors.append(f"{cid}: 非法证据路径")


def _validate_result_catalog(p, result_lists, result_schema, byid, errors, jsonschema, seen):
    for kind, items in result_lists.items():
        for r in items:
            schema_errors = list(jsonschema.Draft202012Validator(result_schema).iter_errors(r))
            if schema_errors:
                errors.extend("结果结构错误: " + e.json_path + " / " + e.validator for e in schema_errors)
                continue
            errors.extend(credential_errors(r, "result"))
            cid = r["case_id"]
            c = byid.get(cid)
            if not c:
                errors.append(f"{cid}: 结果关联未知用例")
                continue
            if c["kind"] != kind:
                errors.append(f"{cid}: 结果通道不匹配")
            if cid in seen:
                errors.append(f"{cid}: 重复当前结果")
            seen.add(cid)
            if r["case_revision"] != c["revision"]:
                errors.append(f"{cid}: 旧版本结果，先归档后再执行")
            status = r["status"]
            if status in ["Pass", "Fail"]:
                _validate_result_evidence(p, r, c, cid, status, errors)
            if status == "Blocked" and not r["reason"]:
                errors.append(f"{cid}: Blocked 缺少原因")


def validate(p, check_results=True):
    import jsonschema

    errors = []
    try:
        project = read(p / "project.json")
        reqs = read(p / "analysis/requirements.json")
        case_lists = {kind: read(p / "testcases" / filename) for kind, filename in FILES.items()}
        result_lists = (
            {kind: read(p / "execution" / f"{kind}-results.json") for kind in FILES} if check_results else {}
        )
    except (ValueError, OSError) as e:
        return ["项目 JSON 文件无法读取: " + type(e).__name__]
    _validate_project_collections(project, reqs, case_lists, result_lists, errors)
    if errors:
        return errors
    req_ids = _validation_requirement_ids(reqs, errors)
    schema = read(ROOT / "references/case.schema.json")
    byid = _validate_case_catalog(case_lists, req_ids, schema, errors, jsonschema)
    seen = set()
    result_schema = read(ROOT / "references/result.schema.json")
    _validate_result_catalog(p, result_lists, result_schema, byid, errors, jsonschema, seen)
    return errors


def require_valid(p, check_results=True):
    errors = validate(p, check_results=check_results)
    if errors:
        raise ValueError("\n".join(errors))


def init(p):
    if p.exists():
        raise ValueError("目标已存在，拒绝覆盖；请选择新项目目录")
    for sub in [
        "input",
        "analysis",
        "testcases",
        "testcase-skill",
        "knowledge",
        "execution/screenshots",
        "execution/logs",
        "execution/history",
        "output",
        "versions",
        "staging",
    ]:
        (p / sub).mkdir(parents=True, exist_ok=True)
    write(
        p / "project.json",
        {"id": "PRJ-" + uuid.uuid4().hex[:12], "schema_version": VERSION, "created_at": now()},
    )
    write(
        p / "qa-config.json",
        {
            "mode": "review",
            "execution_authorized": False,
            "base_url": "",
            "allowed_origins": [],
            "approvals": [],
            "timeout_seconds": 20,
            "cleanup_status": "未执行；未创建数据",
        },
    )
    for sub in [
        "analysis/requirements.json",
        "analysis/api-inventory.json",
        "testcases/functional-cases.json",
        "testcases/api-cases.json",
        "execution/ui-results.json",
        "execution/api-results.json",
    ]:
        write(p / sub, [])
    (p / "testcase-skill/SKILL.md").write_text(
        "# 项目测试规范\n\n待需求分析后补充业务规则、适用场景、来源及待确认项。主控须主动读取。\n",
        encoding="utf-8",
    )
    for k in [
        "business-rules",
        "user-roles",
        "data-dictionary",
        "test-accounts",
        "test-data-rules",
        "historical-defects",
    ]:
        (p / "knowledge" / f"{k}.md").write_text(
            f"# {k}\n\n待补充；未提供不代表不存在。凭据仅存环境变量名称。\n", encoding="utf-8"
        )


def coverage(p):
    cases = [c for c in load_cases(p) if c["lifecycle"] == "active"]
    rows = []
    for r in read(p / "analysis/requirements.json"):
        ids = [c["id"] for c in cases if r["id"] in c["requirement_ids"]]
        rows.append(
            {
                "requirement_id": r["id"],
                "module": r["module"],
                "title": r["title"],
                "case_ids": ids,
                "covered": bool(ids),
            }
        )
    obj = {
        "requirements": len(rows),
        "covered": sum(r["covered"] for r in rows),
        "ratio": sum(r["covered"] for r in rows) / len(rows) if rows else None,
        "rows": rows,
    }
    write(p / "analysis/coverage.json", obj)
    return obj


def resolved(v):
    if isinstance(v, str):
        m = re.fullmatch(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}", v)
        if m:
            if m[1] not in os.environ:
                raise ValueError("缺少凭据环境变量 " + m[1])
            return os.environ[m[1]]
        if "${ENV:" in v:
            raise ValueError("环境变量引用必须是完整字符串")
    if isinstance(v, dict):
        return {k: resolved(x) for k, x in v.items()}
    if isinstance(v, list):
        return [resolved(x) for x in v]
    return v


def pointer(v, ptr):
    if not valid_pointer(ptr):
        raise ValueError("非法 JSON Pointer")
    if ptr == "":
        return v
    for encoded_token in ptr[1:].split("/"):
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        if isinstance(v, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise KeyError(token)
            v = v[int(token)]
        elif isinstance(v, dict):
            v = v[token]
        else:
            raise KeyError(token)
    return v


def request_url(config, case):
    base = config["base_url"]
    b = urlsplit(base)
    if (
        b.scheme not in ["http", "https"]
        or not b.hostname
        or b.username
        or b.password
        or b.path not in ["", "/"]
        or b.query
        or b.fragment
    ):
        raise ValueError("base_url 必须是 HTTP(S) origin")
    origin = f"{b.scheme}://{b.netloc}"
    path = case["path"]
    decoded = path
    for _ in range(3):
        decoded = unquote(decoded)
    if (
        not path.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(x in [".", ".."] for x in decoded.split("/"))
        or urlsplit(path).netloc
        or urlsplit(path).scheme
        or "?" in path
        or "#" in path
    ):
        raise ValueError("拒绝不安全或含 query 的接口路径；query 请用 params")
    return origin + path


def archive_channel(p, kind):
    old = read(p / "execution" / f"{kind}-results.json")
    if not old:
        return
    target = p / "execution/history" / ("ARCHIVE-" + uuid.uuid4().hex)
    target.mkdir(parents=True)
    for r in old:
        for rel in r.get("evidence", []):
            src = safe_file(p, rel)
            if src.is_file():
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
    write(target / f"{kind}-results.json", old)


def case_fingerprint(case):
    return hashlib.sha256(
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _api_basic_checks(assertions, response):
    """Judge text, headers and timing without persisting response or credential values."""
    checks = [
        {"name": "HTTP 状态码", "passed": response.status_code == assertions["status"]}
    ]
    for name, wanted in assertions.get("header_contains", {}).items():
        checks.append({"name": "响应头包含预期值 " + name,
                       "passed": resolved(wanted).lower() in response.headers.get(name, "").lower()})
    for index, wanted in enumerate(assertions.get("body_contains", []), 1):
        checks.append({"name": f"响应内容包含预期文本 {index}", "passed": resolved(wanted) in response.text})
    if "max_ms" in assertions:
        checks.append({"name": "响应时间不超过预期毫秒数",
                       "passed": response.elapsed.total_seconds() * 1000 <= assertions["max_ms"]})
    return checks


def _api_response_checks(c, response, jsonschema):
    checks = _api_basic_checks(c["assertions"], response)
    a = c["assertions"]
    extras = c.get("extensions", {}).get("extra_assertions", {})
    blocked_reason = None
    if a.get("json_equals") or "schema" in a or extras:
        try:
            body = response.json()
            for ptr, want in a.get("json_equals", {}).items():
                try:
                    got = pointer(body, ptr)
                    passed = json.dumps(got, sort_keys=True, ensure_ascii=False) == json.dumps(
                        resolved(want), sort_keys=True, ensure_ascii=False
                    )
                except (KeyError, IndexError, TypeError, ValueError):
                    passed = False
                checks.append({"name": "JSON 字段断言 " + ptr, "passed": passed})
            if "schema" in a:
                checks.append(
                    {
                        "name": "JSON Schema",
                        "passed": not list(
                            jsonschema.Draft202012Validator(a["schema"]).iter_errors(body)
                        ),
                    }
                )
            if extras:
                paths = (
                    {item.get("path") for item in body.get("tree", []) if isinstance(item, dict)}
                    if isinstance(body, dict)
                    else set()
                )
                for wanted in extras.get("required_paths", []):
                    if (
                        wanted not in paths
                        and isinstance(body, dict)
                        and body.get("truncated") is True
                    ):
                        blocked_reason = "目录响应截断，不能确认完整路径"
                    checks.append({"name": "目录包含 " + wanted, "passed": wanted in paths})
                if extras.get("no_nested_paths"):
                    checks.append(
                        {
                            "name": "非递归目录没有嵌套路径",
                            "passed": not any("/" in x for x in paths if isinstance(x, str)),
                        }
                    )
        except ValueError:
            checks.append({"name": "响应是有效 JSON", "passed": False})
    return checks, blocked_reason


def run_api(p):
    import httpx
    import jsonschema

    from .. import paths

    paths.configure_http_environment()

    require_valid(p, check_results=False)
    cfg = read(p / "qa-config.json")
    run = "RUN-" + uuid.uuid4().hex
    archive_channel(p, "api")
    results = []
    write(p / "execution/api-results.json", [])
    # Each case is independently gated. Never follow redirects or retry writes.
    with httpx.Client(
        timeout=float(cfg.get("timeout_seconds", 20)), follow_redirects=False, trust_env=True
    ) as client:
        for c in read(p / "testcases/api-cases.json"):
            if c["lifecycle"] != "active":
                continue
            r = {
                "case_id": c["id"],
                "case_revision": c["revision"],
                "case_content_sha256": case_fingerprint(c),
                "run_id": run,
                "started_at": now(),
                "status": "Blocked",
                "actual": "",
                "assertions": [],
                "evidence": [],
                "reason": "",
            }
            try:
                if cfg.get("mode") not in ["review", "auto"] or cfg.get("execution_authorized") is not True:
                    raise ValueError("未获得执行授权")
                if c["api_source"] == "inferred":
                    raise ValueError("推导接口尚未确认")
                risk = c["risk"]
                if c["method"] not in ["GET", "HEAD", "OPTIONS"] and risk == "read":
                    risk = "write"
                if risk != "read" and risk not in cfg.get("approvals", []):
                    raise ValueError("缺少专门风险授权: " + risk)
                url = request_url(cfg, c)
                kwargs = {k: resolved(c[k]) for k in ["headers", "params", "json"] if k in c}
                try:
                    response = client.request(c["method"], url, **kwargs)
                except httpx.HTTPError as e:
                    raise ValueError("网络执行受阻: " + type(e).__name__) from None
                checks, blocked_reason = _api_response_checks(c, response, jsonschema)
                if blocked_reason:
                    raise ValueError(blocked_reason)
                r.update(
                    status="Pass" if all(x["passed"] for x in checks) else "Fail",
                    actual=f"HTTP {response.status_code}; {response.elapsed.total_seconds():.3f}s; 响应正文未持久化",
                    assertions=checks,
                    reason="" if all(x["passed"] for x in checks) else "实际响应不符合预期断言",
                )
                # Randomized filenames avoid untrusted IDs being used as filesystem paths.
                rel = f"execution/logs/{run}-{uuid.uuid4().hex[:12]}.json"
                evidence = {
                    "case_id": c["id"],
                    "case_revision": c["revision"],
                    "case_content_sha256": case_fingerprint(c),
                    "run_id": run,
                    "http_status": response.status_code,
                    "elapsed_seconds": response.elapsed.total_seconds(),
                    "assertions": checks,
                    "timestamp": now(),
                }
                write(p / rel, evidence)
                r["evidence"] = [rel]
            except ValueError as e:
                r.update(status="Blocked", actual="", reason=str(e), assertions=[], evidence=[])
            except Exception as e:  # noqa: BLE001  # Record unexpected executor failures without losing run results.
                r.update(
                    status="Blocked",
                    actual="",
                    reason="执行器错误: " + type(e).__name__,
                    assertions=[],
                    evidence=[],
                )
            results.append(r)
            write(p / "execution/api-results.json", results)
    write(p / "execution/api-results.json", results)
    require_valid(p)
    print(json.dumps(dict(collections.Counter(r["status"] for r in results)), ensure_ascii=False))  # noqa: T201  # Preserve the legacy command output.
    return results


def merged(p):
    stored = {r["case_id"]: r for r in load_results(p)}
    rows = []
    for c in load_cases(p):
        result = stored.get(c["id"], {"status": "Not Run"})
        if result.get("status") in ("Pass", "Fail") and (
            result.get("case_revision") != c["revision"]
            or result.get("case_content_sha256") != case_fingerprint(c)
        ):
            result = {"status": "Not Run", "reason": "旧执行结果与当前用例版本或内容不一致"}
        rows.append(
            dict(
                c,
                actual_assertions=result.get("assertions", []),
                **{k: v for k, v in result.items() if k not in c},
            )
        )
    return rows


def summary(rows):
    return {
        s: sum(c.get("status", "Not Run") == s for c in rows if c["lifecycle"] == "active") for s in STATES
    }


def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, dict):
        s = json.dumps(v, ensure_ascii=False)
    elif isinstance(v, list):
        s = "\n".join(str(x) if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in v)
    else:
        s = str(v)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s[:32000] + "\n[内容过长，请查看权威源 JSON]" if len(s) > 32000 else s


def export(p):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    require_valid(p)
    cov = coverage(p)
    rows = merged(p)
    artifacts = []
    mappings = {}

    def sheet(wb, name, cols, data):
        ws = wb.create_sheet(name)
        ws.append([label for key, label in cols])
        ws.freeze_panes = "A2"
        for obj in data:
            ws.append([cell_text(obj.get(key, "")) for key, label in cols])
            for cell in ws[ws.max_row]:
                cell.data_type = "s"
        ws.auto_filter.ref = ws.dimensions
        for i, (key, _label) in enumerate(cols, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = (
                46 if key in ["steps", "expected", "actual", "extensions", "json", "assertions"] else 24
            )
        for cell in ws[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="214A66")
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            if "status" in [x[0] for x in cols]:
                idx = [x[0] for x in cols].index("status")
                value = row[idx].value
                color = {"Pass": "E2F0D9", "Fail": "FCE4D6", "Blocked": "FFF2CC", "Not Run": "E7E6E6"}.get(
                    value, "FFFFFF"
                )
                row[idx].fill = PatternFill("solid", fgColor=color)
        return ws

    for kind, title in [("ui", "功能测试用例"), ("api", "接口测试用例")]:
        selected = [r for r in rows if r["kind"] == kind]
        wb = Workbook()
        wb.remove(wb.active)
        cols = COMMON + (API_COLS if kind == "api" else []) + RESULT_COLS
        sheet(wb, "测试用例", cols, selected)
        sheet(
            wb,
            "需求覆盖",
            [
                ("requirement_id", "需求编号"),
                ("module", "模块"),
                ("title", "需求"),
                ("case_ids", "关联用例"),
                ("covered", "已关联用例"),
            ],
            cov["rows"],
        )
        sheet(
            wb,
            "执行统计",
            [("status", "状态"), ("count", "数量")],
            [{"status": s, "count": n} for s, n in summary(selected).items()],
        )
        f = p / "output" / f"{title}.xlsx"
        wb.save(f)
        check = load_workbook(f, read_only=True)
        if check["测试用例"].max_row != len(selected) + 1:
            raise ValueError("导出用例数量不一致")
        check.close()
        artifacts.append("output/" + f.name)
        mappings[kind] = dict(cols)
    wb = Workbook()
    wb.remove(wb.active)
    sheet(
        wb,
        "需求追踪矩阵",
        [
            ("requirement_id", "需求编号"),
            ("module", "模块"),
            ("title", "需求"),
            ("case_ids", "关联用例"),
            ("covered", "已关联用例"),
        ],
        cov["rows"],
    )
    wb.save(p / "analysis/traceability-matrix.xlsx")
    artifacts.append("analysis/traceability-matrix.xlsx")
    sources = [
        "project.json",
        "analysis/requirements.json",
        "testcases/functional-cases.json",
        "testcases/api-cases.json",
        "execution/ui-results.json",
        "execution/api-results.json",
    ]
    write(
        p / "output/export-manifest.json",
        {
            "schema_version": VERSION,
            "mapping_version": VERSION,
            "generated_at": now(),
            "source_sha256": {x: hashlib.sha256((p / x).read_bytes()).hexdigest() for x in sources},
            "artifacts": artifacts,
            "columns": mappings,
            "limitations": [
                "Excel 单元格长度受限，JSON 为权威源",
                "敏感凭据不得写入源 JSON；导出前由操作者核查",
            ],
        },
    )
    print("Excel 和导出清单已生成")  # noqa: T201  # Preserve the legacy command output.


def report(p):
    require_valid(p)
    cov = coverage(p)
    rows = merged(p)
    stats = summary(rows)

    def md(s):
        return str(s).replace("|", "\\|").replace("\n", " ")

    text = [
        "# 测试执行报告",
        "",
        f"生成时间：{now()}",
        "",
        f"用例格式版本：{VERSION}",
        "",
        "需求关联覆盖："
        + (
            f"{cov['covered']}/{cov['requirements']}"
            if cov["requirements"]
            else "无需求，无法计算；不是 100%"
        ),
        "需求关联不代表场景充分或测试通过。",
        "",
        "| 状态 | 数量 |",
        "| --- | --- |",
    ]
    text += [f"| {s} | {stats[s]} |" for s in STATES]
    text += ["", "| 用例 | 修订 | 状态 | 实际结果/原因 | 证据 |", "| --- | --- | --- | --- | --- |"]
    text += [
        "| "
        + " | ".join(
            md(x)
            for x in [
                r["id"],
                r["revision"],
                r.get("status", "Not Run"),
                r.get("reason") or r.get("actual", "尚未执行"),
                ", ".join(r.get("evidence", [])),
            ]
        )
        + " |"
        for r in rows
        if r["lifecycle"] == "active"
    ]
    text += ["", "## 未覆盖需求"] + [
        f"- {r['requirement_id']}：{r['title']}" for r in cov["rows"] if not r["covered"]
    ]
    if all(r["covered"] for r in cov["rows"]):
        text.append("无已登记的未关联需求。文档遗漏与场景充分性仍需分析核对。")
    text += [
        "",
        "## 缺陷与限制",
        "逐项失败见上表；没有实际执行证据的用例不得视为通过。",
        "本引擎不自动实施项目数据初始化与清理；业务场景需项目适配。",
        "",
        "## 测试数据清理",
        read(p / "qa-config.json").get("cleanup_status", "未提供清理状态"),
    ]
    (p / "output/测试执行报告.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    print("报告已生成")  # noqa: T201  # Preserve the legacy command output.


def import_openapi(p, f):
    doc = read(f)
    if not ("openapi" in doc or doc.get("swagger") == "2.0"):
        raise ValueError("不是 OpenAPI/Swagger 文档")
    source = p / "input" / ("openapi-" + hashlib.sha256(Path(f).read_bytes()).hexdigest()[:12] + ".json")
    if source.resolve() != Path(f).resolve():
        shutil.copy2(f, source)
    inv = []
    for path, item in doc.get("paths", {}).items():
        for method, op in item.items():
            if method.upper() not in ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]:
                continue
            inv.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "api_source": "openapi",
                    "source_ref": str(source.relative_to(p))
                    + "#/paths/"
                    + path.replace("~", "~0").replace("/", "~1")
                    + "/"
                    + method,
                    "operation": op,
                    "path_parameters": item.get("parameters", []),
                    "document_security": doc.get("security", []),
                    "servers": doc.get("servers", []),
                    "unresolved_refs_possible": True,
                }
            )
    write(p / "analysis/api-inventory.json", inv)
    print(f"导入 {len(inv)} 个接口；引用需分析阶段解析")  # noqa: T201  # Preserve the legacy command output.


def search(p, query):
    for f in sorted((p / "knowledge").rglob("*")):
        if f.is_file() and f.suffix in [".md", ".json", ".csv", ".txt"]:
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if query.casefold() in line.casefold():
                    print(f"{f.relative_to(p)}:{i}: {line}")  # noqa: T201  # Preserve the legacy command output.


def sync_skills(p):
    """Write the project's own SKILL.md: what it covers and how to replay it with autoskill."""
    require_valid(p, check_results=False)
    project = read(p / "project.json")
    config = read(p / "qa-config.json")
    auth = read(p / "auth.json") if (p / "auth.json").is_file() else {}
    pid = str(project["id"])
    name = project.get("name") or pid
    slug = re.sub(r"[^a-z0-9-]+", "-", pid.lower()).strip("-")[:40] or "project"
    cases = [c for c in load_cases(p) if c["lifecycle"] == "active"]
    ui = sum(c["kind"] == "ui" for c in cases)
    login = [auth[k] for k in ("ui_case", "api_case") if auth.get(k)]
    lines = [
        "---",
        f"name: {slug}-cases",
        f"description: 「{name}」的回放用例（{ui} 条页面用例、{len(cases) - ui} 条接口用例）。对这个系统做回归测试、查看或补充它的用例时使用；需要 autoSkill 技能。",
        "---",
        f"# {name}",
        "",
        f"- 被测地址：{config.get('base_url') or '（未设置）'}",
        "- 用例定义：`testcases/functional-cases.json`（页面）、`testcases/api-cases.json`（接口）",
        "- 业务知识：`knowledge/`（业务规则、角色、测试数据；确认过的事实可以写回这里）",
        "- 登录：" + (f"用例 {'、'.join(login)}，回放和录制时自动执行，不需要手动登录" if login else "未定义登录用例"),
        "",
        "## 怎么用",
        "",
        "```",
        f"autoskill context {pid}          # 登录方式、分页用例与知识索引",
        f"autoskill cases {pid} --limit 50 # 按 next_cursor 继续读取；--case <编号> 读取完整定义",
        f"autoskill knowledge list {pid}   # 先看索引，再按需 knowledge read",
        f"autoskill run {pid}              # 回放全部用例；前置用例自动先执行",
        f"autoskill run {pid} --case <编号>",
        f"autoskill record start {pid}     # 录制新场景，结束时 autoskill record stop --title <标题>",
        "```",
        "",
        "## 用例",
        "",
        "| 编号 | 类型 | 标题 | 前置 |",
        "| --- | --- | --- | --- |",
    ]
    for c in cases[:20]:
        depends = "、".join(c.get("extensions", {}).get("depends_on", []))
        title = str(c["title"]).replace("|", "\\|")
        lines.append(f"| {c['id']} | {'页面' if c['kind'] == 'ui' else '接口'} | {title} | {depends} |")
    if len(cases) > 20:
        lines.extend(["", f"这里只预览前 20 条，共 {len(cases)} 条。用 `autoskill cases {pid}` 按返回的 next_cursor 继续读取，或明确加 `--all`。"])
    (p / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.rmtree(p / "case-skills", ignore_errors=True)  # the older per-kind skills are superseded


def _validate_saved_api_contract(c, jsonschema):
    a = c["assertions"]
    location = c["id"] + ".assertions"
    if "schema" in a:
        try:
            jsonschema.Draft202012Validator.check_schema(a["schema"])
        except jsonschema.SchemaError:
            raise ValueError(location + ".schema: 响应Schema格式错误") from None
        if external_schema_refs(a["schema"]):
            raise ValueError(location + ".schema: 响应Schema禁止未解析的外部引用")
    if "schema" in a and not meaningful_schema(a["schema"]):
        raise ValueError(location + ": 接口用例缺少有效断言；请按原业务预期补充字段、响应内容、响应头或有约束的响应Schema")
    if any(not valid_pointer(ptr) for ptr in a.get("json_equals", {})):
        raise ValueError(location + ".json_equals: 非法 JSON Pointer")


def _saved_case_revision(case, previous, preserve_revisions):
    if previous and previous["kind"] != case["kind"]:
        raise ValueError("已有用例不能更改类型")
    if preserve_revisions:
        return
    case["schema_version"] = VERSION
    if previous:
        left = {k: v for k, v in case.items() if k != "revision"}
        right = {k: v for k, v in previous.items() if k != "revision"}
        case["revision"] = previous["revision"] + (left != right)
    else:
        case["revision"] = 1


def save_cases(p, incoming, *, preserve_revisions=False, requirements=None):
    """Replace a complete case catalog with snapshots and automatic revisions."""
    import jsonschema

    require_valid(p, check_results=False)
    if not isinstance(incoming, list) or any(not isinstance(c, dict) for c in incoming):
        raise ValueError("用例输入必须是对象数组")
    schema = read(ROOT / "references/case.schema.json")
    old = load_cases(p)
    byid = {c["id"]: c for c in old}
    proposed = []
    seen = set()
    reqs = read(p / "analysis/requirements.json") if requirements is None else requirements
    requirement_errors = []
    req_ids = _validation_requirement_ids(reqs, requirement_errors)
    if requirement_errors:
        raise ValueError("；".join(requirement_errors))
    for raw in incoming:
        c = copy.deepcopy(raw)
        schema_errors = list(jsonschema.Draft202012Validator(schema).iter_errors(c))
        if schema_errors:
            raise ValueError("用例Schema不符: " + ", ".join(e.json_path for e in schema_errors))
        if c["id"] in seen:
            raise ValueError("重复用例ID")
        if not set(c["requirement_ids"]).issubset(req_ids):
            raise ValueError("用例关联未知需求")
        seen.add(c["id"])
        _saved_case_revision(c, byid.get(c["id"]), preserve_revisions)
        problems = credential_errors(c, c["id"])
        if problems:
            raise ValueError("\n".join(problems))
        if c["kind"] == "api":
            _validate_saved_api_contract(c, jsonschema)
        proposed.append(c)
    for c in old:
        if c["id"] not in seen:
            archived = copy.deepcopy(c)
            if archived["lifecycle"] != "archived":
                archived.update(lifecycle="archived", revision=c["revision"] + 1)
            proposed.append(archived)
    snapshot = p / "versions" / ("SNAPSHOT-" + uuid.uuid4().hex)
    for rel in ["project.json", "analysis/requirements.json", *[f"testcases/{f}" for f in FILES.values()]]:
        dest = snapshot / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p / rel, dest)
    if requirements is not None and requirements != read(p / "analysis/requirements.json"):
        write(p / "analysis/requirements.json", requirements)
    for kind, filename in FILES.items():
        write(p / "testcases" / filename, [c for c in proposed if c["kind"] == kind])
    sync_skills(p)
    return proposed


def _dispatch_project_command(ap, a, p):
    if a.command == "init":
        init(p)
    elif a.command == "validate":
        require_valid(p)
        print("校验通过")  # noqa: T201  # Preserve the legacy command output.
    elif a.command == "coverage":
        require_valid(p)
        cov = coverage(p)
        print(json.dumps(cov, ensure_ascii=False))  # noqa: T201  # Preserve the legacy command output.
        return 0 if cov["requirements"] and cov["covered"] == cov["requirements"] else 2
    elif a.command == "run-api":
        results = run_api(p)
        return 2 if any(r["status"] != "Pass" for r in results) else 0
    elif a.command == "export":
        export(p)
    elif a.command == "report":
        report(p)
    elif a.command == "save-cases":
        if not a.extra:
            ap.error("需要完整用例目录 JSON")
        save_cases(p, read(Path(a.extra)))
        print("用例已保存，版本和项目技能已同步")  # noqa: T201  # Preserve the legacy command output.
    elif a.command == "sync-skills":
        sync_skills(p)
        print("UI/API 项目用例 Skill 已生成")  # noqa: T201  # Preserve the legacy command output.
    elif a.command == "search":
        if not a.extra:
            ap.error("需要关键词")
        search(p, a.extra)
    elif a.command == "import-openapi":
        if not a.extra:
            ap.error("需要 OpenAPI JSON 路径")
        import_openapi(p, Path(a.extra))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "command",
        choices=[
            "init",
            "doctor",
            "validate",
            "coverage",
            "run-api",
            "export",
            "report",
            "import-openapi",
            "search",
            "save-cases",
            "sync-skills",
        ],
    )
    ap.add_argument("project", nargs="?")
    ap.add_argument("extra", nargs="?")
    a = ap.parse_args()
    if a.command == "doctor":
        print(  # noqa: T201  # Preserve the legacy command output.
            json.dumps(
                {
                    "python": sys.version.split()[0],
                    **{
                        x: importlib.util.find_spec(x) is not None
                        for x in ["httpx", "jsonschema", "openpyxl"]
                    },
                    "agent_browser_cli": shutil.which("agent-browser") is not None,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not a.project:
        ap.error("需要项目目录")
    p = Path(a.project).resolve()
    return _dispatch_project_command(ap, a, p)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, ImportError, TypeError) as e:
        print("错误: " + str(e), file=sys.stderr)  # noqa: T201  # Preserve the legacy command output.
        sys.exit(1)
