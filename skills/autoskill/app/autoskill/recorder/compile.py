"""Compile a finished recording into one UI case and the API cases behind it.

Nothing secret is written into a case: passwords and keys become ${ENV:NAME},
tokens and ids that came from an earlier response become {{variables}} with a
dependency on the case that produces them.
"""
from __future__ import annotations

import json
import re

from .. import builder, projects, secrets, sessions
from . import har as harlib

DROP_HEADERS = re.compile(
    r"^(user-agent|accept.*|referer|origin|host|connection|content-length|content-type|cookie|pragma|"
    r"cache-control|sec-.*|upgrade-insecure-requests|if-.*|priority|dnt|te)$", re.I)
SCHEME = re.compile(r"^(Bearer|Basic|Token)\s+(.+)$", re.I)
LOGIN_PATH = re.compile(r"(login|signin|sign-in|auth|token|session)", re.I)
CHANGING = {"fill", "type", "click", "dblclick", "press", "check", "uncheck", "select"}
MAX_API_CASES = 30
MAX_RESOURCE_CASES = 8


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


class Compiler:
    def __init__(self, project_id: str, rec: dict, browser_state: dict | None, login_ui_case: str | None):
        self.pid = project_id
        self.rec = rec
        self.auth = dict(projects.auth(project_id))
        self.login_ui_case = login_ui_case
        self.state = browser_state or {}
        self.cases: list[dict] = []
        self.pool: dict[str, tuple[int, str, str]] = {}  # value -> (case index, pointer, key)
        self.names: dict[tuple[int, str], str] = {}
        self.notes: list[str] = []
        self.state_variables: dict = dict(self.auth.get("state_variables", {}))
        wanted = set(rec.get("secrets", []))
        for case in projects.cases(project_id):
            wanted |= secrets.references(case)
        self.secret_names = {secrets.get_secret(n): n for n in sorted(wanted) if secrets.get_secret(n)}
        self.next_number = int(projects.next_case_id(project_id, "api")[len("TC-API-"):])

    # --- values ------------------------------------------------------
    def variable(self, value: str) -> tuple[str, str] | None:
        """(name, id of the case that provides it) when the value came from an earlier response."""
        hit = self.pool.get(value)
        if not hit:
            return None
        index, pointer, key = hit
        source = self.cases[index]
        name = self.names.get((index, pointer))
        if not name:
            taken = set(self.names.values()) | set(self.state_variables)
            name = self.names[(index, pointer)] = var_name(key, source["path"], taken)
            source["extensions"].setdefault("extract", {})[name] = pointer
        return name, source["id"]

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

    def scrub(self, value, key: str, deps: set):
        """Replace secrets and correlated values anywhere inside a request part."""
        if isinstance(value, dict):
            return {k: self.scrub(v, k, deps) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v, key, deps) for v in value]
        if isinstance(value, bool) or value is None or value == "":
            return value
        if isinstance(value, str) and value in self.secret_names:
            return "${ENV:" + self.secret_names[value] + "}"
        found = self.variable(str(value))
        if found:
            deps.add(found[1])
            return "{{" + found[0] + "}}"
        if isinstance(value, str) and harlib.SENSITIVE.search(key):
            return self.secret(value, key)
        return value

    def credential(self, token: str, header: str, deps: set) -> str:
        """Where an auth token comes from at replay time: an earlier response, the login case, or the browser login."""
        found = self.variable(token)
        if found:
            deps.add(found[1])
            self.auth_provider = found[1]
            return "{{" + found[0] + "}}"
        login = next((c for c in projects.cases(self.pid) if c["id"] == self.auth.get("api_case")), None)
        provided = list(login.get("extensions", {}).get("extract", {})) if login else []
        if provided:
            deps.add(login["id"])
            return "{{" + next((n for n in provided if "token" in n.lower()), provided[0]) + "}}"
        for origin in self.state.get("origins", []):
            for item in origin.get("localStorage", []):
                if item["value"] == token and self.login_ui_case:
                    name = _identifier(item["name"])
                    self.state_variables[name] = {"from": "localStorage", "name": item["name"]}
                    deps.add(self.login_ui_case)
                    return "{{" + name + "}}"
        self.notes.append(f"请求头 {header} 的值已保存为本机凭据；如果它会过期，接口回放会失败，需人工确认登录方式")
        return self.secret(token, header)

    def signature(self, case: dict) -> str:
        parts = [case["method"], case["path"], case.get("params", {}), case.get("json")]
        return json.dumps(parts, sort_keys=True, ensure_ascii=False)

    def add_business(self, entry: dict, module: str, existing: dict) -> str | None:
        """Build (or reuse) the API case for one captured request. Returns 'new', 'reused' or None."""
        if entry["body_note"]:
            self.notes.append(f'{entry["method"]} {entry["path"]}：{entry["body_note"]}，未生成接口用例')
            return None
        deps: set = set()
        segments = []
        for segment in entry["path"].split("/"):
            found = self.variable(segment) if segment else None
            if found:
                deps.add(found[1])
            segments.append("{{" + found[0] + "}}" if found else segment)
        headers = {}
        for name, value in entry["headers"].items():
            if DROP_HEADERS.match(name):
                continue
            shown = "-".join(part.capitalize() for part in name.split("-"))
            if name == "authorization":
                scheme = SCHEME.match(value)
                headers[shown] = (scheme[1] + " " if scheme else "") + self.credential(scheme[2] if scheme else value, shown, deps)
            else:
                headers[shown] = self.scrub(value, name, deps)
        params = self.scrub(entry["params"], "", deps)
        body = self.scrub(entry["json"], "", deps) if entry["json"] is not None else None
        signed_in = "authorization" in entry["headers"] or "cookie" in entry["headers"]
        login_ids = {self.auth.get("api_case"), self.login_ui_case, getattr(self, "auth_provider", None)} - {None}
        if signed_in and not (deps & login_ids) and not self.rec.get("as_login"):
            fallback = self.auth.get("api_case") or self.login_ui_case
            if fallback:
                deps.add(fallback)
        rules: dict = {"status": entry["status"]}
        if entry["response"] is not None:
            fields = harlib.stable_fields(entry["response"], [entry["json"], entry["params"]])
            if fields:
                rules["json_equals"] = fields
            schema = harlib.infer_schema(entry["response"])
            if schema.get("required") or schema.get("minProperties") or schema.get("type") == "array":
                rules["schema"] = schema
        if len(rules) == 1 and entry["status"] < 300 and entry["status"] not in (204, 205) and entry["mime"]:
            rules["header_contains"] = {"content-type": entry["mime"]}
        verb = {"GET": "查询", "POST": "提交", "PUT": "修改", "PATCH": "修改", "DELETE": "删除"}.get(entry["method"], entry["method"])
        title = f'{verb} {"/".join(segments)}' + (f'（返回 {entry["status"]}）' if entry["status"] >= 400 else "")
        case = builder.api_case(f"TC-API-{self.next_number:03d}", title, entry["method"], "/".join(segments), rules,
                                module=module, headers=headers, params=params, json=body, depends_on=sorted(deps))
        match = existing.get(self.signature(case))
        if match and match["assertions"]["status"] == entry["status"]:
            case, outcome = json.loads(json.dumps(match)), "reused"
        elif self.signature(case) in {self.signature(c) for c in self.cases}:
            return None  # the same request repeated within this recording
        else:
            self.next_number += 1
            outcome = "new"
        self.cases.append(case)
        for pointer, key, value in harlib.correlatable(entry["response"]):
            self.pool.setdefault(str(value), (len(self.cases) - 1, pointer, key))
        return outcome

    def add_resource(self, entry: dict, existing: dict) -> None:
        """A page or static file request: real HTTP, replayable, but not a business API."""
        if entry["method"] != "GET" or entry["status"] != 200 or not entry["mime"]:
            return
        rules: dict = {"status": 200, "header_contains": {"content-type": entry["mime"]}}
        title = re.search(r"<title>([^<]{1,80})</title>", entry["text"] or "", re.I)
        if title and entry["mime"] == "text/html":
            rules["body_contains"] = [f"<title>{title[1]}</title>"]
        case = builder.api_case(f"TC-API-{self.next_number:03d}", f'页面资源可访问 {entry["path"]}', "GET", entry["path"], rules,
                                module="页面与资源", params=entry["params"], traffic="resource", tags=["resource"])
        signature = self.signature(case)
        if signature in {self.signature(c) for c in self.cases}:
            return
        if signature in existing:
            self.cases.append(json.loads(json.dumps(existing[signature])))
            return
        self.next_number += 1
        self.cases.append(case)

    def mark_login(self) -> None:
        """In a login recording, the request that returned the session token becomes the API login case."""
        for index, case in enumerate(self.cases):
            if case["method"] == "POST" and LOGIN_PATH.search(case["path"]) and case["assertions"]["status"] < 400:
                tokens = [v for v, (i, _, key) in self.pool.items() if i == index and harlib.SENSITIVE.search(key)]
                if tokens:
                    self.variable(tokens[0])  # make sure the token is extracted even if nothing used it yet
                    self.auth["api_case"] = case["id"]
                    probe = next((c for c in self.cases[index + 1:] if c["method"] == "GET"
                                  and case["id"] in projects.depends_on(c) and c["assertions"]["status"] == 200), None)
                    if probe:
                        self.auth["api_check_case"] = probe["id"]
                    return


def build(project_id: str, rec: dict, har: dict, state: dict | None, title: str, module: str = "",
          category: str = "") -> dict:
    """Compile and save. Returns a summary for the agent; nothing secret is in it."""
    auth = projects.auth(project_id)
    actions, checks = split_steps(rec["steps"])
    ui_id = projects.next_case_id(project_id, "ui")
    login_ui = ui_id if rec.get("as_login") else auth.get("ui_case") if rec.get("used_login") else None
    automation = {"start_url": rec["start_url"], "actions": actions, "assertions": checks}
    ui_case = builder.ui_case(ui_id, title, automation, module=module or ("登录" if rec.get("as_login") else ""),
                              category=category or ("登录" if rec.get("as_login") else ""), risk=ui_risk(actions),
                              depends_on=[login_ui] if login_ui and login_ui != ui_id else None)
    compiler = Compiler(project_id, rec, state, login_ui)
    base = projects.config(project_id)["base_url"]
    captured, notes = harlib.entries(har, base)
    compiler.notes += notes
    existing = {compiler.signature(c): c for c in projects.cases(project_id) if c["kind"] == "api"}
    business = [e for e in captured if e["business"]]
    counts = {"new": 0, "reused": 0}
    for entry in business[:MAX_API_CASES]:
        if rec.get("used_login") and not rec.get("as_login"):
            entry["headers"].setdefault("cookie", "")  # recorded while signed in: depends on the login
        outcome = compiler.add_business(entry, module or "接口", existing)
        if outcome:
            counts[outcome] += 1
    if len(business) > MAX_API_CASES:
        compiler.notes.append(f"接口请求较多，只为前 {MAX_API_CASES} 个生成了用例")
    traffic = "business"
    if not compiler.cases:
        traffic = "resource"
        for entry in [e for e in captured if e["resource"]][:MAX_RESOURCE_CASES]:
            compiler.add_resource(entry, existing)
        if compiler.cases:
            compiler.notes.append("这个页面没有调用业务接口；接口用例是「页面与静态资源可访问」检查，不是业务接口测试")
    if rec.get("as_login"):
        compiler.auth["ui_case"] = ui_id
        compiler.mark_login()
    if compiler.state_variables:
        compiler.auth["state_variables"] = compiler.state_variables
    if not checks:
        compiler.notes.append("没有记录断言：这条 UI 用例只验证动作可以完成；建议用 autoskill check 补充期望")
    projects.save_cases(project_id, [ui_case, *compiler.cases])
    projects.set_auth(project_id, compiler.auth)
    if rec.get("as_login") and state:
        sessions.save(project_id, ui=state)
    return {
        "ui_case": {"id": ui_id, "title": title, "steps": len(actions), "assertions": len(checks),
                    "depends_on": projects.depends_on(ui_case)},
        "api_cases": [{"id": c["id"], "title": c["title"], "depends_on": projects.depends_on(c)} for c in compiler.cases],
        "api_traffic": traffic if compiler.cases else "none", "api_new": counts["new"], "api_reused": counts["reused"],
        "login": {k: compiler.auth.get(k) for k in ("ui_case", "api_case") if compiler.auth.get(k)},
        "secrets": sorted(secrets.references([ui_case, *compiler.cases])), "notes": compiler.notes,
    }
