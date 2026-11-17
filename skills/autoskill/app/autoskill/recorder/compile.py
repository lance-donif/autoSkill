"""Compile a finished recording into one UI case and the API cases behind it.

Nothing secret is written into a case: passwords and keys become ${ENV:NAME},
tokens and ids that came from an earlier response become {{variables}} with a
dependency on the case that produces them.
"""
from __future__ import annotations

import json
import re

from .. import builder, candidate_publication, execution_inputs, projects, secrets
from ..replay import ir
from . import har as harlib
from . import traffic

DROP_HEADERS = re.compile(
    r"^(user-agent|accept.*|referer|origin|host|connection|content-length|cookie|pragma|"
    r"cache-control|sec-.*|upgrade-insecure-requests|if-.*|priority|dnt|te)$", re.IGNORECASE)
SCHEME = re.compile(r"^(Bearer|Basic|Token)\s+(.+)$", re.IGNORECASE)
LOGIN_PATH = re.compile(r"(login|signin|sign-in|auth|token|session)", re.IGNORECASE)
CHANGING = {"fill", "type", "click", "dblclick", "press", "check", "uncheck", "select"}
CONTAINERS = {"data", "result", "response", "items", "records", "rows", "list", "payload", "body", "attributes"}
PATH_ACTIONS = {"create", "update", "delete", "list", "detail", "search", "new", "get"}


def split_steps(steps: list[dict]) -> tuple[list[dict], list[dict]]:
    """Trailing checks are the case's expectations; everything before them stays in recorded order."""
    cut = len(steps)
    while cut and "assertion" in steps[cut - 1]:
        cut -= 1
    return steps[:cut], steps[cut:]


def ui_risk(actions: list[dict]) -> str:
    return "write" if any(step.get("action") in CHANGING for step in actions) else "read"


def _identifier(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", text).strip("_") or "value"
    return "v_" + name if name[0].isdigit() else name


def var_name(key: str, path: str, taken: set) -> str:
    base = _identifier(key)
    if base.lower() == "id":  # /api/tickets -> ticket_id
        noun = next((s for s in reversed(path.split("/")) if s and not s.startswith("{{") and not s[0].isdigit()), "item")
        base = _identifier(noun).rstrip("s") + "_id"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def _field_name(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()


def _resource(value: str) -> str:
    name = _field_name(value)
    if name.endswith("ies"):
        return name[:-3] + "y"
    return name[:-1] if name.endswith("s") and not name.endswith(("ss", "us")) else name


def _id_resource(key: str) -> str:
    parts = _field_name(key).split("_")
    return _resource("_".join(parts[:-1])) if parts[-1] in ("id", "ids") else ""


def _path_resource(path: str, value: str = "") -> str:
    parts = [part for part in path.split("/") if part and part != value and not part.isdigit()
             and not part.startswith("{{") and _field_name(part) not in PATH_ACTIONS]
    return _resource(parts[-1]) if parts else ""


class Compiler:
    def __init__(self, project_id: str, rec: dict, browser_state: dict | None, login_ui_case: str | None):
        self.pid = project_id
        self.rec = rec
        self.auth = dict(projects.auth(project_id))
        self.login_ui_case = login_ui_case
        self.state = browser_state or {}
        self.cases: list[dict] = []
        self.pool: dict[str, list[tuple[int, str, str]]] = {}  # value -> possible (case index, pointer, key) sources
        self.names: dict[tuple[int, str], str] = {}
        self.cookie_sources: dict[tuple[str, str], str] = {}
        self.cookie_logins: set[str] = set()
        self.notes: list[str] = []
        self.uncompiled: list[dict] = []
        self.state_variables: dict = dict(self.auth.get("state_variables", {}))
        wanted = set(secrets.names()) | set(rec.get("secrets", []))
        self.configured_credentials = wanted - candidate_publication.unresolved_credentials()
        self.secret_names = {secrets.get_secret(n): n for n in sorted(wanted) if secrets.get_secret(n)}
        self.credential_sources: list[dict] = []
        self.unresolved: set[str] = set()
        self.next_number = int(candidate_publication.next_case_id(project_id, "api")[len("TC-API-"):])
        self.rand = rec.get("rand") or ""

    def template(self, value):
        """Text this recording made unique becomes {{RAND}} again, so every replay creates its own data."""
        return value.replace(self.rand, ir.RAND) if self.rand and isinstance(value, str) else value

    # --- values ------------------------------------------------------
    def _source_resource(self, hit: tuple[int, str, str], value: str) -> str:
        index, pointer, key = hit
        if named := _id_resource(key):
            return named
        parents = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:-1]]
        parents = [part for part in parents if not part.isdigit() and _field_name(part) not in CONTAINERS]
        return _resource(parents[-1]) if parents else _path_resource(self.cases[index]["path"], value)

    def _variable_source(self, value: str, key: str | None, resource: str) -> tuple[int, str, str] | None:
        hits = self.pool.get(value, [])
        if not hits:
            return None
        sensitive_key = bool(key and harlib.SENSITIVE.search(key))
        wanted = _id_resource(key or "") or ("" if sensitive_key else _resource(resource))
        matching = [hit for hit in hits if wanted and self._source_resource(hit, value) == wanted]
        if wanted and not matching:
            self.notes.append(f"请求字段 {key or resource} 的资源与已捕获来源不匹配，已保留录制值")
            return None
        hits = matching or hits
        matching = ([hit for hit in hits if _field_name(hit[2]) == _field_name(key)] if key is not None else
                    [hit for hit in hits if harlib.STRICT_ID.search(hit[2])])
        hits = matching or hits
        if len(hits) == 1 or len({self._source_resource(hit, value) for hit in hits}) == 1:
            return hits[0]  # later reads of the same resource must not replace its original producer
        if sensitive_key:
            sensitive = [hit for hit in hits if harlib.SENSITIVE.search(hit[2])]
            if sensitive:
                return sensitive[0]
        self.notes.append(f"请求字段 {key or resource or '路径参数'} 的值对应多个资源，无法唯一关联，已保留录制值")
        return None

    def _extract(self, hit: tuple[int, str, str]) -> tuple[str, str]:
        index, pointer, key = hit
        source = self.cases[index]
        name = self.names.get((index, pointer))
        if not name:
            taken = set(self.names.values()) | set(self.state_variables)
            name = self.names[(index, pointer)] = var_name(key, source["path"], taken)
            source["extensions"].setdefault("extract", {})[name] = pointer
        return name, source["id"]

    def variable(self, value: str, key: str | None = None, resource: str = "") -> tuple[str, str] | None:
        """Associate equal values by their field/resource, preserving distinct producers for colliding ids."""
        if len(value) < 4 and key is not None and not (harlib.STRICT_ID.search(key) or _id_resource(key)):
            return None  # a short number is an id only where the field says so: page=1 is not ticket 1
        hit = self._variable_source(value, key, resource)
        return self._extract(hit) if hit else None

    def secret(self, value: str, hint: str) -> str:
        """A ${ENV:NAME} reference for a secret value, storing it locally if it is new."""
        name = self.secret_names.get(value)
        if not name:
            base = _identifier(f"{self.pid}_{hint}").upper()
            name, n = base, 2
            while secrets.get_secret(name) not in (None, value):
                name, n = f"{base}_{n}", n + 1
            secrets.set_secret(name, value)
            self.secret_names[value] = name
        return "${ENV:" + name + "}"

    def scrub(self, value, key: str, deps: set, resource: str = ""):
        """Replace secrets and correlated values anywhere inside a request part."""
        if isinstance(value, dict):
            return {k: self.scrub(v, k, deps, _resource(k) if isinstance(v, (dict, list)) and _field_name(k) not in CONTAINERS
                                  else resource) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v, key, deps, resource) for v in value]
        if isinstance(value, bool) or value is None or value == "":
            return value
        if isinstance(value, str) and value in self.secret_names:
            name = self.secret_names[value]
            self.credential_sources.append({"source": "configured_credential" if name in self.configured_credentials else "recorded_credential", "name": name})
            if name not in self.configured_credentials:
                self.unresolved.add(name)
            return "${ENV:" + name + "}"
        found = self.variable(str(value), key, resource)
        if found:
            deps.add(found[1])
            if harlib.SENSITIVE.search(key):
                self.credential_sources.append({"source": "api_extract", "case_id": found[1], "variable": found[0]})
            return "{{" + found[0] + "}}"
        if isinstance(value, str) and harlib.SENSITIVE.search(key):
            return self.recorded_credential(value, key)
        return self.template(value)

    def credential(self, token: str, header: str, deps: set) -> str:
        """Where an auth token comes from at replay time: an earlier response, the login case, or the browser login."""
        found = self.variable(token, "token")
        if found:
            deps.add(found[1])
            self.auth_provider = found[1]
            self.credential_sources.append({"source": "api_extract", "case_id": found[1], "variable": found[0]})
            return "{{" + found[0] + "}}"
        known = self.secret_names.get(token)
        if known in self.configured_credentials:
            self.credential_sources.append({"source": "configured_credential", "name": known})
            return "${ENV:" + known + "}"
        login = next((c for c in projects.cases(self.pid) if c["id"] == self.auth.get("api_case")), None)
        provided = list(login.get("extensions", {}).get("extract", {})) if login else []
        if provided:
            deps.add(login["id"])
            variable = next((n for n in provided if "token" in n.lower()), provided[0])
            self.credential_sources.append({"source": "api_extract", "case_id": login["id"], "variable": variable})
            return "{{" + variable + "}}"
        for origin in self.state.get("origins", []):
            for item in origin.get("localStorage", []):
                if item["value"] == token and self.login_ui_case:
                    name = _identifier(item["name"])
                    self.state_variables[name] = {"from": "localStorage", "name": item["name"]}
                    deps.add(self.login_ui_case)
                    self.credential_sources.append({"source": "browser_state", "case_id": self.login_ui_case, "variable": name})
                    return "{{" + name + "}}"
        self.notes.append(f"请求头 {header} 只有录制时的凭据，尚无独立来源；请补充 API 登录或明确的运行输入来源")
        return self.recorded_credential(token, header)

    def recorded_credential(self, value: str, hint: str) -> str:
        reference = self.secret(value, hint)
        name = self.secret_names[value]
        self.unresolved.add(name)
        self.credential_sources.append({"source": "recorded_credential", "name": name})
        return reference

    def signature(self, case: dict) -> str:
        content_type = next((value for name, value in case.get("headers", {}).items() if name.lower() == "content-type"), None)
        if content_type is None:
            content_type = "application/json" if "json" in case else "application/x-www-form-urlencoded" if "form" in case else ""
        parts = [case["method"], case["path"], case.get("params", {}), case.get("json"), case.get("form"), content_type.lower().strip()]
        return json.dumps(parts, sort_keys=True, ensure_ascii=False)

    def _request_cookies(self, value: str, deps: set) -> str:
        """Keep jar dependencies and explicit references without guessing a token producer."""
        explicit = []
        parsed = harlib.cookies(value)
        if value.strip() and not parsed:
            self.notes.append("Cookie 请求头无法按名称解析，需补充独立来源后才能冷启动验证")
            return self.recorded_credential(value, "cookie")
        for name, token in parsed.items():
            if provider := self.cookie_sources.get((name, token)):
                deps.add(provider)
                continue
            hits = self.pool.get(token, [])
            matching = [hit for hit in hits if _field_name(hit[2]) == _field_name(name)]
            hits = matching or hits
            if len(hits) == 1:
                variable, provider = self._extract(hits[0])
                deps.add(provider)
                self.auth_provider = provider
                self.credential_sources.append({"source": "api_extract", "case_id": provider, "variable": variable})
                reference = "{{" + variable + "}}"
            elif self.secret_names.get(token) in self.configured_credentials:
                credential = self.secret_names[token]
                self.credential_sources.append({"source": "configured_credential", "name": credential})
                reference = "${ENV:" + credential + "}"
            else:
                reason = "对应多个响应来源，无法唯一关联" if hits else "没有捕获到独立的登录或响应来源"
                self.notes.append(f"Cookie {name} {reason}；需补充来源后才能独立冷启动验证")
                reference = self.recorded_credential(token, "cookie_" + name)
            explicit.append(name + "=" + reference)
        return "; ".join(explicit)

    def _business_headers(self, entry: dict, deps: set) -> dict:
        """Normalize captured headers and record credential dependencies in order."""
        headers = {}
        for name, value in entry["headers"].items():
            if name.lower() == "cookie":
                if cookies := self._request_cookies(value, deps):
                    headers["Cookie"] = cookies
                continue
            if DROP_HEADERS.match(name):
                continue
            shown = "-".join(part.capitalize() for part in name.split("-"))
            if name == "authorization":
                scheme = SCHEME.match(value)
                headers[shown] = (scheme[1] + " " if scheme else "") + self.credential(scheme[2] if scheme else value, shown, deps)
            else:
                headers[shown] = self.scrub(value, name, deps)
        return headers

    @staticmethod
    def is_login(entry: dict) -> bool:
        return entry["method"] == "POST" and bool(LOGIN_PATH.search(entry["path"])) and entry["status"] < 400

    def remember_cookies(self, entry: dict, provider: str) -> None:
        for name, value in entry.get("cookies", {}).items():
            self.cookie_sources[(name, value)] = provider
        if entry.get("cookies") and self.is_login(entry):
            self.cookie_logins.add(provider)

    def _login_dependencies(self, entry: dict, deps: set) -> None:
        for cookie in harlib.cookies(entry["headers"].get("cookie", "")).items():
            if provider := self.cookie_sources.get(cookie):
                deps.add(provider)
        signed_in = "authorization" in entry["headers"] or "cookie" in entry["headers"]
        login_ids = {self.auth.get("api_case"), self.login_ui_case, getattr(self, "auth_provider", None)} - {None}
        if signed_in and not (deps & (login_ids | self.cookie_logins)) and not self.rec.get("as_login"):
            fallback = self.auth.get("api_case") or self.login_ui_case
            if fallback:
                deps.add(fallback)

    def _observed_rules(self, entry: dict) -> dict:
        """Learn a labelled regression baseline without treating it as an independent oracle."""
        rules: dict = {"status": entry["status"]}
        if entry["mime"] == "text/event-stream":
            self.notes.append(f'{entry["method"]} {entry["path"]}：流式正文已保存为来源证据；此接口用例仅检查传输，请用 conversation build 配置多轮关联和独立答案预期')
        if entry["response"] is not None:
            fields = harlib.stable_fields(entry["response"], [entry["json"], entry["params"], entry.get("form")])
            if fields:
                rules["json_equals"] = {pointer: self.template(value) for pointer, value in fields.items()}
            schema = harlib.infer_schema(entry["response"])
            if schema.get("required") or schema.get("minProperties") or schema.get("type") == "array":
                rules["schema"] = schema
        if len(rules) == 1 and entry["status"] < 300 and entry["status"] not in (204, 205) and entry["mime"]:
            rules["header_contains"] = {"content-type": entry["mime"]}
        return rules

    def add_business(self, entry: dict, module: str, existing: dict) -> str | None:
        """Build (or reuse) the API case for one captured request. Returns 'new', 'reused' or None."""
        if entry["body_note"]:
            self.notes.append(f'{entry["method"]} {entry["path"]}：{entry["body_note"]}，未生成接口用例')
            self.uncompiled.append({"protocol": "http", "method": entry["method"], "path": entry["path"], "reason": entry["body_note"]})
            if self.rec.get("as_login") and self.login_ui_case and self.is_login(entry):
                self.remember_cookies(entry, self.login_ui_case)
            return None
        self.credential_sources = []
        self.unresolved = set()
        deps: set = set()
        segments = []
        path = entry["path"].split("/")
        for index, segment in enumerate(path):
            found = self.variable(segment, resource=path[index - 1] if index else "") if segment else None
            if found:
                deps.add(found[1])
            segments.append("{{" + found[0] + "}}" if found else self.template(segment))
        headers = self._business_headers(entry, deps)
        resource = _path_resource("/".join(segments))
        params = self.scrub(entry["params"], "", deps, resource)
        body = self.scrub(entry["json"], "", deps, resource) if entry["json"] is not None else None
        form = self.scrub(entry["form"], "", deps, resource) if entry.get("form") else None
        self._login_dependencies(entry, deps)
        rules = self._observed_rules(entry)
        verb = {"GET": "查询", "POST": "提交", "PUT": "修改", "PATCH": "修改", "DELETE": "删除"}.get(entry["method"], entry["method"])
        technical_label = f'{verb} {"/".join(segments)}' + (f'（返回 {entry["status"]}）' if entry["status"] >= 400 else "")
        if "json_equals" not in rules and not rules.get("schema", {}).get("required"):
            self.notes.append(f'{entry["method"]} {entry["path"]}：响应里没有适合自动断言的稳定字段，只校验了状态码和大致结构；需要更强的校验请人工补充')
        case = builder.api_case(f"TC-API-{self.next_number:03d}", "【待确认：接口功能目标】",
                                entry["method"], "/".join(segments), rules, module=module,
                                headers=headers, params=params, json=body, form=form, depends_on=sorted(deps),
                                traffic="business")
        case["extensions"]["technical_capture_label"] = technical_label
        case["extensions"]["oracle"] = {"source": "observation", "verification": "unverified",
                                         "note": "规则来自录制响应，只能作为回归基线；业务预期需与需求或独立数据核对"}
        signature = self.signature(case)
        repeated = next((c for c in self.cases if self.signature(c) == signature), None)
        if repeated:
            self.remember_cookies(entry, repeated["id"])
            return None  # the same request repeated within this recording
        match = existing.get(signature)
        if match and match["assertions"]["status"] == entry["status"]:
            case, outcome = json.loads(json.dumps(match)), "reused"
            if deps:
                case["extensions"]["depends_on"] = sorted(set(projects.depends_on(case)) | deps)
                if case["preconditions"] == "无" or case["preconditions"].startswith("前置用例："):
                    case["preconditions"] = "前置用例：" + "、".join(case["extensions"]["depends_on"])
        else:
            self.next_number += 1
            outcome = "new"
        execution = case["extensions"].setdefault("execution", {})
        if "Cookie" in headers:
            execution["merge_cookies"] = True
        execution["credential_sources"] = list(self.credential_sources)
        unresolved = sorted(self.unresolved & secrets.references(case))
        if unresolved:
            execution["unresolved_recorded_credentials"] = unresolved
        self.cases.append(case)
        self.remember_cookies(entry, case["id"])
        for pointer, key, value in harlib.correlatable(entry["response"]):
            self.pool.setdefault(str(value), []).append((len(self.cases) - 1, pointer, key))
        return outcome


    def mark_login(self) -> None:
        """In a login recording, the request that returned the session token becomes the API login case."""
        for index, case in enumerate(self.cases):
            if case["method"] == "POST" and LOGIN_PATH.search(case["path"]) and case["assertions"]["status"] < 400:
                tokens = [hit for hits in self.pool.values() for hit in hits if hit[0] == index and harlib.SENSITIVE.search(hit[2])]
                if tokens or case["id"] in self.cookie_logins:
                    if tokens:
                        self._extract(tokens[0])  # make sure the token is extracted even if nothing used it yet
                    self.auth["api_case"] = case["id"]
                    probe = next((c for c in self.cases[index + 1:] if c["method"] == "GET"
                                  and case["id"] in projects.depends_on(c) and c["assertions"]["status"] == 200), None)
                    if probe:
                        self.auth["api_check_case"] = probe["id"]
                    return


def _protocol_observations(entries: list[dict]) -> list[dict]:
    observations = []
    for entry in entries:
        if traffic.protocol(entry) == "websocket":
            observations.append({"protocol": "websocket", "reason": "握手和帧已保存为来源证据；当前编译器不把 WebSocket 转成 REST 用例"})
        elif traffic.category(entry) == "unclassified_beacon":
            observations.append({"protocol": "http", "reason": "浏览器 beacon 尚未关联独立业务目标，仅保留来源观察，不自动生成业务接口用例"})
    return observations


def _candidate_versions(project_id: str, candidates: list[dict]) -> None:
    current = {case["id"]: case for case in projects.cases(project_id, active_only=False)}
    for case in candidates:
        if not case.get("requirement_ids"):
            case["requirement_ids"] = ["REQ-RECORDED"]
        if case["kind"] == "api":
            case["extensions"].setdefault("execution", {})["api_only"] = True
        if case["id"] in current and case != current[case["id"]]:
            case["revision"] = current[case["id"]]["revision"] + 1


def build(project_id: str, rec: dict, har: dict, state: dict | None, title: str, module: str = "",
          category: str = "") -> dict:
    """Compile private candidates; publication requires their actual replay evidence."""
    auth = projects.auth(project_id)
    actions, checks = split_steps(rec["steps"])
    ui_id = candidate_publication.next_case_id(project_id, "ui")
    login_ui = ui_id if rec.get("as_login") else auth.get("ui_case") if rec.get("used_login") else None
    automation = {"start_url": rec["start_url"], "actions": actions, "assertions": checks}
    setup = rec.get("setup_cases", [])
    if setup:
        automation["setup_cases"] = setup
    preparation = [step for stage in setup for step in stage["automation"].get("actions", [])]
    ui_case = builder.ui_case(ui_id, title, automation, module=module or ("登录" if rec.get("as_login") else ""),
                              category=category or ("登录" if rec.get("as_login") else ""), risk=ui_risk(preparation + actions),
                              depends_on=[login_ui] if login_ui and login_ui != ui_id else None,
                              test_case_document=rec.get("test_case_document"))
    observations = [step["recording_result"] for step in rec["steps"] if "recording_result" in step]
    ui_case["extensions"]["verification"] = "verified" if any("assertion" in step for step in rec["steps"]) else "unverified"
    if observations:
        ui_case["extensions"]["recording_result"] = {"status": "Fail" if any(not row["passed"] for row in observations) else "Pass",
                                                    "assertions": observations}
    if rec.get("workflow_binding"):
        ui_case["extensions"]["workflow"] = {key: value for key, value in rec["workflow_binding"].items() if key != "lease_token"}
        ui_case["extensions"]["operation_ids"] = rec.get("operations", [])
    compiler = Compiler(project_id, rec, state, login_ui)
    base = projects.config(project_id)["base_url"]
    captured, notes = harlib.entries(har, base)
    compiler.notes += notes
    existing = {compiler.signature(c): c for c in projects.cases(project_id) if c["kind"] == "api"}
    business = harlib.drop_typeahead([e for e in captured if e["business"]])
    counts = {"new": 0, "reused": 0}
    selected = {id(entry) for entry in business}
    for entry in captured:
        if id(entry) not in selected:
            if rec.get("as_login") and compiler.is_login(entry):
                compiler.remember_cookies(entry, ui_id)
            continue
        if rec.get("used_login") and not rec.get("as_login"):
            entry["headers"].setdefault("cookie", "")  # recorded while signed in: depends on the login
        outcome = compiler.add_business(entry, module or "接口", existing)
        if outcome:
            counts[outcome] += 1
    compiler.uncompiled.extend(_protocol_observations(har.get("log", {}).get("entries", [])))
    if compiler.uncompiled:
        compiler.notes.append("部分业务通信仅保存来源证据，尚未生成可执行接口用例；详见 api_uncompiled")
    # Pages, scripts and styles get no API case: the page case already proves they load, and a plain HTTP
    # client asking for them is what anti-bot gateways answer differently (200 in the browser, 202 outside).
    if rec.get("as_login"):
        compiler.auth["ui_case"] = ui_id
        compiler.mark_login()
    if compiler.state_variables:
        compiler.auth["state_variables"] = compiler.state_variables
    if not any("assertion" in step for step in rec["steps"]):
        compiler.notes.append("没有记录断言：这条 UI 用例只验证动作可以完成；建议用 autoskill check 补充期望")
    candidates = [ui_case, *compiler.cases]
    _candidate_versions(project_id, candidates)
    source = {"kind": "recording", "session_id": rec.get("session_id"), "started_at": rec.get("started_at"),
              "rand": rec.get("rand"), "steps_hash": execution_inputs.content_hash(rec["steps"])}
    if setup:
        source["setup_hash"] = execution_inputs.content_hash(setup)
    return {
        "candidate_id": candidate_publication.identity(project_id, source), "candidates": candidates,
        "candidate_auth": compiler.auth, "candidate_source": source, "published_ids": [],
        "ui_case": {"id": ui_id, "title": title, "steps": sum("action" in s for s in rec["steps"]) + 1,
                    "assertions": sum("assertion" in s for s in rec["steps"]),
                    **({"setup_cases": [stage["case_id"] for stage in setup]} if setup else {}),
                    "verification": ui_case["extensions"]["verification"],
                    "recording_status": ui_case["extensions"].get("recording_result", {}).get("status", "Unverified"),
                    "depends_on": projects.depends_on(ui_case)},
        "api_cases": [{"id": c["id"], "title": c["title"], "depends_on": projects.depends_on(c)} for c in compiler.cases],
        "api_traffic": "business" if compiler.cases else "none", "api_new": counts["new"], "api_reused": counts["reused"],
        "api_uncompiled": compiler.uncompiled,
        "api_compile_scope": {**traffic.selected(har.get("log", {}).get("entries", []))[1],
                              "business_candidates": len(business), "generated_api_cases": len(compiler.cases)},
        "login": {k: compiler.auth.get(k) for k in ("ui_case", "api_case") if compiler.auth.get(k)},
        "secrets": sorted(secrets.references([ui_case, *compiler.cases])), "notes": list(dict.fromkeys(compiler.notes)),
    }
