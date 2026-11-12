"""One recording at a time: every browser action an agent takes becomes a replayable step."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from .. import db, paths, projects, secrets
from ..replay import ir
from . import agent

SESSION = "autoskill-rec"
DOM_JS = (Path(__file__).resolve().parents[1] / "replay" / "dom.js").read_text(encoding="utf-8")
TARGETED = ("click", "dblclick", "hover", "check", "uncheck", "fill", "type", "select")
READ_ONLY = ("snapshot", "get", "is", "wait", "screenshot", "scroll", "scrollintoview", "focus", "eval")
DENIED = ("install", "upgrade", "update", "doctor", "skills", "connect", "dashboard", "auth", "state", "cookies",
          "storage", "network", "close", "record", "flow", "mcp", "chat", "batch", "tab", "pdf", "download", "upload")
DENIED_FLAGS = ("--provider", "-p", "--proxy", "--cdp", "--auto-connect", "--config", "--executable-path", "--args",
                "--allowed-domains", "--state", "--profile", "--session", "--headers", "--extension", "--init-script",
                "--restore", "--capture", "--namespace", "--engine")


class RecordError(Exception):
    pass


def _file() -> Path:
    return paths.local() / "recording.json"


def current(required: bool = True) -> dict | None:
    file = _file()
    if file.is_file():
        return json.loads(file.read_text(encoding="utf-8"))
    if required:
        raise RecordError("当前没有进行中的录制；先运行 autoskill record start <项目>")
    return None


def _save(rec: dict) -> None:
    _file().write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def relative(url: str, base_url: str) -> str:
    """Store same-origin addresses as paths so a shared project still works behind another host."""
    if base_url and ir.origin_of(url) == base_url.rstrip("/"):
        parts = urlsplit(url)
        return (parts.path or "/") + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    return url


def snapshot(rec: dict, interactive: bool = True) -> str:
    data = agent.call(SESSION, ["snapshot", *(["-i"] if interactive else [])]) or {}
    rec["refs"] = data.get("refs", {})
    return data.get("snapshot", "")


def _describe(spec: dict) -> dict | None:
    script = "JSON.stringify((" + DOM_JS + ")().target(" + json.dumps(spec, ensure_ascii=False) + "))"
    result = (agent.call(SESSION, ["eval", "--stdin"], stdin_text=script) or {}).get("result")
    return json.loads(result) if isinstance(result, str) and result != "null" else None


def _spec(rec: dict, token: str, rich: bool = False) -> dict:
    if token.startswith("@"):
        ref = rec["refs"].get(token[1:])
        if not ref:
            raise RecordError(f"引用 {token} 已失效；先运行 autoskill browser snapshot -i 再操作")
        box = agent.call(SESSION, ["get", "box", token], check=False)
        return {"ref": {"role": ref.get("role"), "name": ref.get("name"), "box": box}}
    if rich:
        kind, _, value = token.partition("=")
        if kind in ("testid", "text", "label", "placeholder") and value:
            return {"locator": {"by": kind, "value": value}}
        if kind == "role" and value:
            role, _, name = value.partition(":")
            return {"locator": {"by": "role", "role": role, "name": name or None}}
    return {"locator": {"by": "css", "value": token}}


def _target(rec: dict, token: str, rich: bool = False) -> dict:
    found = _describe(_spec(rec, token, rich))
    if not found or not found.get("locators"):
        raise RecordError(f"页面上找不到 {token}；先运行 autoskill browser snapshot -i 确认元素")
    return found


def _secret_value(rec: dict, text: str, is_secret_field: bool) -> tuple[str, str]:
    """Return (text to type, text to record). Secrets are recorded as ${ENV:NAME} only."""
    if secrets.references(text):
        rec["secrets"] = sorted({*rec["secrets"], *secrets.references(text)})
        return secrets.resolve(text), text
    if not is_secret_field:
        return text, text
    base = rec["project_id"].upper().replace("-", "_") + "_PASSWORD"
    name, n = base, 2
    while secrets.get_secret(name) not in (None, text):
        name, n = f"{base}_{n}", n + 1
    secrets.set_secret(name, text)
    rec["secrets"] = sorted({*rec["secrets"], name})
    return text, "${ENV:" + name + "}"


class PageProbe:
    """Reads the live page through agent-browser so assertions are judged by the replay rules."""

    def _read(self, locator: dict, what: str, arg: str | None = None):
        script = ("JSON.stringify((" + DOM_JS + ")().read(" + json.dumps(locator, ensure_ascii=False) + ", "
                  + json.dumps(what) + ", " + json.dumps(arg) + "))")
        result = (agent.call(SESSION, ["eval", "--stdin"], stdin_text=script) or {}).get("result")
        return json.loads(result) if isinstance(result, str) else None

    def count(self, locator: dict) -> int:
        return int(self._read(locator, "count") or 0)

    def read(self, locator: dict, what: str, arg: str | None = None):
        return self._read(locator, what, arg)

    def url(self) -> str:
        return (agent.call(SESSION, ["get", "url"]) or {}).get("url", "")

    def title(self) -> str:
        return (agent.call(SESSION, ["get", "title"]) or {}).get("title", "")


def start(project_id: str, login: bool = True, as_login: bool = False, url: str | None = None) -> dict:
    """Open the fenced browser on the project's address and begin recording actions and traffic."""
    if current(required=False):
        raise RecordError("已有进行中的录制；先运行 autoskill record stop 或 autoskill record cancel")
    info, config, auth = projects.get(project_id), projects.config(project_id), projects.auth(project_id)
    start_url = ir.absolute_url(url or info["start_url"], config["base_url"])
    ir.check_origin(start_url, config["allowed_origins"])
    used_login, note, state_file = False, "", None
    if login and not as_login and auth.get("ui_case"):
        from .. import runner, sessions

        run = runner.create(project_id, [auth["ui_case"]], "playwright", trigger="login")
        if runner.execute(run["id"])["status"] != "Pass":
            reason = (db.one("SELECT reason FROM results WHERE run_id = ? ORDER BY id DESC", (run["id"],)) or {}).get("reason", "")
            raise RecordError("自动登录没有成功，录制未开始：" + (reason or "请检查登录用例和凭据"))
        state_file = paths.local() / "recording-state.json"
        state_file.write_text(json.dumps(sessions.load(project_id).get("ui", {})), encoding="utf-8")
        used_login, note = True, f"已用登录用例 {auth['ui_case']} 自动登录"
    agent.call(SESSION, ["close"], check=False, timeout=30)
    try:
        agent.call(SESSION, ["open", "about:blank"], launch=agent.launch_args(config["allowed_origins"], state_file), timeout=90)
        agent.call(SESSION, ["network", "har", "start"])
        opened = agent.call(SESSION, ["open", start_url], timeout=90) or {}
    finally:
        if state_file:
            state_file.unlink(missing_ok=True)
    rec = {"project_id": project_id, "started_at": db.now(), "start_url": relative(start_url, config["base_url"]),
           "steps": [], "refs": {}, "secrets": [], "used_login": used_login, "as_login": as_login}
    tree = snapshot(rec)
    _save(rec)
    return {"recording": True, "project": project_id, "url": opened.get("url", start_url), "title": opened.get("title", ""),
            "login": note or ("本次录制的就是登录流程" if as_login else "未登录状态"), "snapshot": tree}


def _record(rec: dict, step: dict) -> dict:
    rec["steps"].append(step)
    _save(rec)
    return {"recorded": len(rec["steps"]), "step": ir.describe(step)}


def _after(rec: dict, result: dict) -> dict:
    """Report where the page is now; leaving the authorised site is refused by the browser fence."""
    url = PageProbe().url()
    result["url"] = url
    try:
        ir.check_origin(url, projects.config(rec["project_id"])["allowed_origins"])
    except PermissionError as error:
        result["warning"] = str(error)
    return result


def undo() -> dict:
    rec = current()
    if not rec["steps"]:
        raise RecordError("还没有记录任何步骤")
    removed = rec["steps"].pop()
    _save(rec)
    return {"removed": ir.describe(removed), "remaining": len(rec["steps"]),
            "note": "只撤销了记录；页面上已经发生的操作不会回退"}


def status() -> dict:
    rec = current(required=False)
    if not rec:
        return {"recording": False}
    return {"recording": True, "project": rec["project_id"], "started_at": rec["started_at"],
            "start_url": rec["start_url"], "steps": [ir.describe(s) for s in rec["steps"]]}


def cancel() -> dict:
    agent.call(SESSION, ["close"], check=False, timeout=30)
    existed = _file().is_file()
    _file().unlink(missing_ok=True)
    return {"cancelled": existed}


def _plain(text: str) -> str:
    """`secret:NAME` is a shell-friendly spelling of ${ENV:NAME}."""
    return "${ENV:" + text[7:] + "}" if text.startswith("secret:") and secrets.NAME.fullmatch(text[7:]) else text


def browser(argv: list[str]) -> dict:
    """Run one agent-browser command inside the recording and record it when it changes the page."""
    rec = current()
    if not argv:
        raise RecordError("缺少浏览器命令，例如：autoskill browser click @e3")
    command, rest = argv[0], argv[1:]
    if command in DENIED or any(a.split("=")[0] in DENIED_FLAGS for a in argv):
        raise RecordError(f"录制中不能使用 {command}：安装、升级、远程浏览器、网络与登录态由 autoskill 统一管理")
    config = projects.config(rec["project_id"])
    if command == "snapshot":
        tree = snapshot(rec, interactive="-i" in rest or not rest)
        _save(rec)
        return {"snapshot": tree}
    if command in ("get", "is", "wait", "screenshot", "scroll", "scrollintoview"):
        return {"result": agent.call(SESSION, argv)}
    if command == "open":
        if not rest:
            raise RecordError("open 需要地址")
        url = ir.absolute_url(rest[0], config["base_url"])
        ir.check_origin(url, config["allowed_origins"])
        agent.call(SESSION, ["open", url], timeout=90)
        result = _record(rec, {"action": "goto", "url": relative(url, config["base_url"])})
    elif command == "reload":
        agent.call(SESSION, ["reload"], timeout=90)
        result = _record(rec, {"action": "reload"})
    elif command == "press":
        if not rest:
            raise RecordError("press 需要按键名，例如 Enter")
        focused = _describe({})
        agent.call(SESSION, ["press", rest[0]])
        step = {"action": "press", "key": rest[0]}
        if focused and focused.get("locators"):
            step["locators"] = focused["locators"]
        result = _record(rec, step)
    elif command in TARGETED:
        if not rest:
            raise RecordError(f"{command} 需要目标，例如 @e3")
        found = _target(rec, rest[0])  # describe before acting: the element may be gone afterwards
        step = {"action": command, "locators": found["locators"]}
        send = list(argv)
        if command in ("fill", "type", "select"):
            if len(rest) < 2:
                raise RecordError(f"{command} 需要内容")
            typed, step["value"] = _secret_value(rec, _plain(rest[1]), bool(found.get("secret")) and command != "select")
            send = [command, rest[0], typed]
        agent.call(SESSION, send)
        result = _record(rec, step)
    else:
        raise RecordError(f"录制不支持 {command}；可用：open snapshot click dblclick hover fill type select check uncheck press reload get is wait")
    result["snapshot"] = snapshot(rec)
    _save(rec)
    return _after(rec, result)


def check(kind: str, target: str | None, expected: str | None, attribute: str | None = None) -> dict:
    """Verify an expectation on the live page now; only a passing one is recorded."""
    from ..replay import ui

    rec = current()
    if kind not in ir.ASSERTIONS:
        raise RecordError("断言类型只能是：" + " ".join(ir.ASSERTIONS))
    needs_target, required = ir.ASSERTIONS[kind]
    step: dict = {"assertion": kind}
    if needs_target:
        if not target:
            raise RecordError(f"{kind} 断言需要目标，例如 @e5、testid=toast 或 CSS 选择器")
        if kind == "count":
            step["locators"] = [_spec(rec, target, rich=True).get("locator") or {"by": "css", "value": target}]
        else:
            step["locators"] = _target(rec, target, rich=True)["locators"]
    if "expected" in required:
        if expected is None:
            raise RecordError(f"{kind} 断言需要期望值")
        step["expected"] = int(expected) if kind == "count" and expected.isdigit() else expected
    elif kind == "checked" and expected is not None:
        step["expected"] = expected.lower() not in ("false", "0", "no")
    if kind == "attribute":
        if not attribute:
            raise RecordError("attribute 断言需要 --attribute 属性名")
        step["attribute"] = attribute
    ir.validate({"start_url": "/", "actions": [], "assertions": [step]})
    outcome = ui.check(PageProbe(), step, 5.0)
    if not outcome["passed"]:
        raise RecordError(f"断言现在不成立，没有记录：{ir.describe(step)}；页面实际为「{outcome['actual']}」")
    return {**_record(rec, step), "actual": outcome["actual"]}


def stop(title: str, module: str = "", category: str = "") -> dict:
    """Finish the recording: export traffic, close the browser, compile and save the cases."""
    from . import compile as compiler

    rec = current()
    if not (title or "").strip():
        raise RecordError("请用 --title 给这条用例起一个说明业务含义的名字")
    if not rec["steps"]:
        raise RecordError("还没有记录任何步骤；先用 autoskill browser 操作页面，或运行 autoskill record cancel 放弃")
    har_file, state_file = paths.local() / "recording.har", paths.local() / "recording-state.json"
    har, state = {"log": {"entries": []}}, None
    try:
        if agent.call(SESSION, ["network", "har", "stop", str(har_file)], check=False) is not None and har_file.is_file():
            har = json.loads(har_file.read_text(encoding="utf-8", errors="replace"))
        if agent.call(SESSION, ["state", "save", str(state_file)], check=False) is not None and state_file.is_file():
            state = json.loads(state_file.read_text(encoding="utf-8"))
    finally:
        agent.call(SESSION, ["close"], check=False, timeout=30)
        har_file.unlink(missing_ok=True)  # raw traffic and cookies never stay on disk
        state_file.unlink(missing_ok=True)
    summary = compiler.build(rec["project_id"], rec, har, state, title.strip(), module, category)
    _file().unlink(missing_ok=True)
    summary["project"] = rec["project_id"]
    return summary
