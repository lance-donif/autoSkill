"""One recording at a time: every browser action an agent takes becomes a replayable step."""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import db, paths, projects, secrets
from ..replay import ir
from ..replay.tabs import Tabs
from . import actions, agent



def session_name() -> str:
    """One browser session per workspace: two autoSkill folders on one machine must never share a browser."""
    import hashlib

    return "autoskill-" + hashlib.sha1(str(paths.workspace()).lower().encode("utf-8")).hexdigest()[:10]
DOM_JS = (Path(__file__).resolve().parents[1] / "replay" / "dom.js").read_text(encoding="utf-8")
TARGETED = ("click", "dblclick", "hover", "check", "uncheck", "fill", "type", "select", "focus", "scrollintoview")


class RecordError(Exception):
    pass


def _file() -> Path:
    return paths.local() / "recording.json"


def current(required: bool = True) -> dict | None:
    file = _file()
    if file.is_file():
        rec = json.loads(file.read_text(encoding="utf-8"))
        ir.set_rand(rec.get("rand", ""))
        return rec
    if required:
        raise RecordError("当前没有进行中的录制；先运行 autoskill record start <项目>")
    return None


def _save(rec: dict) -> None:
    _file().write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def _observe_tabs(rec: dict, settle: float = 0) -> dict | None:
    """Map native handles to recording ids; never store a CDP target id in a case."""
    deadline = time.monotonic() + settle
    while True:
        tabs = (agent.call(session_name(), ["tab", "list"]) or {}).get("tabs", [])
        if not tabs:
            raise RecordError("所有标签页都已关闭；请取消录制后重新开始")
        live = [tab["tabId"] for tab in tabs]
        active = next((tab["tabId"] for tab in tabs if tab.get("active")), live[0])
        if "tab_map" not in rec:
            rec.update(tab_map={native: f"t{i + 1}" for i, native in enumerate(live)},
                       live_tabs=live, active_tab=active, next_tab=len(live) + 1)
        old_live, old_active = rec["live_tabs"], rec["active_tab"]
        opened, closed = [t for t in live if t not in old_live], [t for t in old_live if t not in live]
        if opened or closed or active != old_active or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    for native in opened:
        rec["tab_map"][native] = f"t{rec['next_tab']}"
        rec["next_tab"] += 1
    ids = rec["tab_map"]
    if active != old_active:
        rec["frame_path"] = []
        rec["refs"] = {}
    base = projects.config(rec["project_id"])["base_url"]
    change = {"opened": [{"id": ids[t["tabId"]], "url": relative(Tabs.destination(t.get("url", "")), base)}
                         for t in tabs if t["tabId"] in opened],
              "closed": [ids[t] for t in closed], "active": ids[active]}
    rec.update(live_tabs=live, active_tab=active,
               tab_info=[{"id": ids[t["tabId"]], "active": t["tabId"] == active,
                          "url": t.get("url", ""), "title": t.get("title", "")} for t in tabs])
    return change if opened or closed or active != old_active else None


def _sync_tabs(rec: dict) -> None:
    change = _observe_tabs(rec)
    if change:
        _record(rec, {"action": "tab_sync", "tabs": change})
        snapshot(rec)


def _act(rec: dict, argv: list[str]) -> dict | None:
    active = rec["tab_map"][rec["active_tab"]]
    try:
        agent.call(session_name(), argv)
    except agent.AgentError:
        # A click can close its own CDP target before the mouse-event reply arrives.
        # Only accept that specific observed transition; every other action error stays an error.
        change = _observe_tabs(rec)
        if not change or active not in change["closed"]:
            raise
        return change
    return None


def _tab_command(rec: dict, rest: list[str]) -> dict:
    if not rest or rest == ["list"]:
        _save(rec)
        return {"tabs": rec["tab_info"], "tab": rec["tab_map"][rec["active_tab"]]}
    op = rest[0]
    live = {rec["tab_map"][t]: t for t in rec["live_tabs"]}
    if op == "new" and len(rest) <= 2:
        config = projects.config(rec["project_id"])
        url = ir.absolute_url(rest[1], config["base_url"]) if len(rest) == 2 else "about:blank"
        ir.check_origin(url, config["allowed_origins"])
        agent.call(session_name(), ["tab", "new", "about:blank"])
        _observe_tabs(rec)
        tab = rec["tab_map"][rec["active_tab"]]
        if url != "about:blank":
            _open(url, float(config.get("page_load_seconds", 90)))
        step = {"action": "tab_new", "tab": tab, "url": relative(url, config["base_url"])}
        change = _observe_tabs(rec)
        if change:
            step["tabs"] = change
    else:
        closing = op == "close"
        tab = (rest[1] if len(rest) == 2 else rec["tab_map"][rec["active_tab"]]) if closing else op
        if (closing and len(rest) > 2) or (not closing and len(rest) != 1) or not re.fullmatch(r"t[1-9][0-9]*", tab):
            raise RecordError("用法：browser tab list / tab new [地址] / tab t1 / tab close [t1]")
        if tab not in live:
            raise RecordError(f"标签页 {tab} 不存在或已关闭；运行 browser tab list 查看当前页面")
        if closing and len(live) == 1:
            raise RecordError("不能关闭最后一个标签页；请结束或取消录制")
        agent.call(session_name(), ["tab", *(["close"] if closing else []), live[tab]])
        _observe_tabs(rec)
        step = {"action": "tab_close" if closing else "tab_switch", "tab": tab}
        if closing:
            step["active"] = rec["tab_map"][rec["active_tab"]]
    result = _record(rec, step)
    rec["refs"] = {}
    result.update(snapshot=snapshot(rec), tabs=rec["tab_info"])
    _save(rec)
    return _after(rec, result)


def relative(url: str, base_url: str) -> str:
    """Store same-origin addresses as paths so a shared project still works behind another host."""
    if base_url and ir.origin_of(url) == base_url.rstrip("/"):
        parts = urlsplit(url)
        return (parts.path or "/") + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    return url


READY_JS = "JSON.stringify({state: document.readyState, password: !!document.querySelector('input[type=password]')})"
WEB = ("http://", "https://")


def _page_state() -> dict:
    """Where the browser is and how far the page has loaded. Never raises."""
    url = (agent.call(session_name(), ["get", "url"], check=False, timeout=30) or {}).get("url", "")
    info: dict = {}
    if url.startswith(WEB):
        raw = (agent.call(session_name(), ["eval", "--stdin"], stdin_text=READY_JS, check=False, timeout=30) or {}).get("result")
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            info = {}
    return {"url": url, "state": info.get("state", ""), "password": bool(info.get("password"))}


def _wait_ready(budget: float, problem: str = "") -> dict:
    """Wait until the page has finished loading, for at most `budget` seconds.

    agent-browser gives up on a navigation after 25 s; a slow page is still loading at that point, so the
    wait is done here against the page itself.
    """
    started = time.monotonic()
    while True:
        page = _page_state()
        if page["state"] == "complete":
            break
        if page["url"].startswith("chrome-error:"):
            raise RecordError("目标页面打不开（地址不可达、域名或证书有问题）" + (f"：{problem}" if problem else ""))
        if time.monotonic() - started >= budget:
            if not page["url"].startswith(WEB):
                raise RecordError(f"浏览器在 {budget:.0f} 秒内没有打开目标页面" + (f"：{problem}" if problem else ""))
            page["slow"] = True  # still loading: carry on with what is there
            break
        time.sleep(1)
    page["seconds"] = round(time.monotonic() - started, 1)
    return page


def _open(url: str, budget: float) -> dict:
    """Navigate and wait for the page. A navigation that outlives agent-browser's own wait is not a failure."""
    started = time.monotonic()
    problem = ""
    for attempt in (1, 2):
        try:
            agent.call(session_name(), ["open", url], timeout=90)
            problem = ""
        except agent.AgentError as error:
            problem = str(error)
        landed = (agent.call(session_name(), ["get", "url"], check=False, timeout=30) or {}).get("url", "")
        if problem or landed.startswith(WEB) or landed.startswith("chrome-error:"):
            break  # loading, loaded or failed; only "returned fine but still blank" is worth a second try
    page = _wait_ready(max(5.0, budget - (time.monotonic() - started)), problem)
    page["seconds"] = round(time.monotonic() - started, 1)
    return page


def _settled_snapshot(rec: dict) -> str:
    """The first snapshot of a page: single-page apps draw their controls a moment after loading."""
    tree = snapshot(rec)
    deadline = time.monotonic() + 12
    while not rec["refs"] and time.monotonic() < deadline:
        time.sleep(1.5)
        tree = snapshot(rec)
    return tree


def snapshot(rec: dict, interactive: bool = True) -> str:
    data = actions.frame_snapshot(rec, DOM_JS, interactive) if rec.get("frame_path") else None
    if data is None:
        data = agent.call(session_name(), ["snapshot", *(["-i"] if interactive else [])]) or {}
    rec["refs"] = data.get("refs", {})
    return data.get("snapshot", "")


def _describe(spec: dict) -> dict | None:
    script = "JSON.stringify((" + DOM_JS + ")().target(" + json.dumps(spec, ensure_ascii=False) + "))"
    result = (actions.evaluate(session_name(), script) or {}).get("result")
    return json.loads(result) if isinstance(result, str) and result != "null" else None


def _spec(rec: dict, token: str, rich: bool = False) -> dict:
    if token.startswith("@"):
        ref = rec["refs"].get(token[1:])
        if not ref:
            raise RecordError(f"引用 {token} 已失效；先运行 autoskill browser snapshot 再操作")
        if ref.get("selector"):
            return {"locator": {"by": "css", "value": ref["selector"]}}
        box = agent.call(session_name(), ["get", "box", token], check=False)
        return {"ref": {"role": ref.get("role"), "name": ref.get("name"), "box": box}}
    if rich:
        kind, _, value = token.partition("=")
        if kind in ("testid", "text", "label", "placeholder") and value:
            return {"locator": {"by": kind, "value": value}}
        if kind == "role" and value:
            role, _, name = value.partition(":")
            return {"locator": {"by": "role", "role": role, "name": name or None}}
    return {"locator": {"by": "css", "value": token}}


def _as_target(locator: dict) -> str:
    """A locator written the way `check` and `browser` accept it."""
    if locator["by"] == "css":
        return locator["value"]
    if locator["by"] == "role":
        return f'role={locator["role"]}:{locator.get("name") or ""}'
    return f'{locator["by"]}={locator["value"]}'


def _target(rec: dict, token: str, rich: bool = False) -> dict:
    found = _describe(_spec(rec, token, rich))
    if not found or not found.get("locators"):
        hint = "先运行 autoskill browser snapshot --all 找到它的 @编号，或改用 CSS 选择器"
        if token.startswith("text="):
            hint = "text= 要求与元素的完整文字完全一致（不是包含）；" + hint
        elif token.startswith("testid="):
            hint = "页面上没有 data-testid 为这个值的元素；" + hint
        raise RecordError(f"页面上找不到 {token}；{hint}")
    return found


def _secret_value(rec: dict, text: str, is_secret_field: bool) -> tuple[str, str]:
    """Return (text to type, text to record). Secrets are recorded as ${ENV:NAME} only."""
    if secrets.references(text):
        rec["secrets"] = sorted({*rec["secrets"], *secrets.references(text)})
        return secrets.resolve(ir.expand(text)), text
    if not is_secret_field:
        return ir.expand(text), text  # {{RAND}} is typed as this recording's value and recorded as written
    return ir.expand(text), _secret_ref(rec, text, "PASSWORD")


def _secret_ref(rec: dict, text: str, label: str = "VALUE") -> str:
    """Keep opaque command payloads and sensitive field values out of cases."""
    if secrets.references(text):
        rec["secrets"] = sorted({*rec.get("secrets", []), *secrets.references(text)})
        return text
    base = re.sub(r"[^A-Za-z0-9_]", "_", rec["project_id"] + "_" + label).upper()
    if not base or base[0].isdigit():
        base = "VALUE_" + base
    name, n = base, 2
    while secrets.get_secret(name) not in (None, text):
        name, n = f"{base}_{n}", n + 1
    secrets.set_secret(name, text)
    rec["secrets"] = sorted({*rec.get("secrets", []), name})
    return "${ENV:" + name + "}"


def _redact(rec: dict, value):
    if isinstance(value, str):
        for name in rec.get("secrets", []):
            secret = secrets.get_secret(name)
            if secret:
                value = value.replace(secret, "${ENV:" + name + "}")
        return value
    if isinstance(value, list):
        return [_redact(rec, item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(rec, item) for key, item in value.items()}
    return value


class PageProbe:
    """Reads the live page through agent-browser so assertions are judged by the replay rules."""

    def _read(self, locator: dict, what: str, arg: str | None = None):
        script = ("JSON.stringify((" + DOM_JS + ")().read(" + json.dumps(locator, ensure_ascii=False) + ", "
                  + json.dumps(what) + ", " + json.dumps(arg) + "))")
        result = (actions.evaluate(session_name(), script) or {}).get("result")
        return json.loads(result) if isinstance(result, str) else None

    def count(self, locator: dict) -> int:
        return int(self._read(locator, "count") or 0)

    def read(self, locator: dict, what: str, arg: str | None = None):
        return self._read(locator, what, arg)

    def url(self) -> str:
        return (agent.call(session_name(), ["get", "url"]) or {}).get("url", "")

    def title(self) -> str:
        return (agent.call(session_name(), ["get", "title"]) or {}).get("title", "")


def _fit_window() -> None:
    """Make the page fill the maximised window (or a full-size screen when no window is shown)."""
    try:
        size = [1920, 1080]
        if paths.headed():
            got = (agent.call(session_name(), ["eval", "--stdin"],
                              stdin_text="JSON.stringify([outerWidth, outerHeight, innerWidth, innerHeight])") or {}).get("result")
            outer_w, outer_h, inner_w, _ = json.loads(got)
            if outer_w - inner_w < 100:
                return  # the page already fills the window
            size = [outer_w - 16, outer_h - 140]
        agent.call(session_name(), ["set", "viewport", str(size[0]), str(size[1])], check=False)
    except (agent.AgentError, ValueError, TypeError):
        pass  # a smaller page is an inconvenience, not a reason to stop


def _element_png(locator: dict) -> bytes:
    """The pixels of an image or canvas as shown, without requesting it again (that would draw a new captcha)."""
    script = ("(() => { const el = (" + DOM_JS + ")().find(" + json.dumps(locator, ensure_ascii=False) + ")[" + str(locator.get("nth", 0)) + "];"
              "const c = document.createElement('canvas'); c.width = el.naturalWidth || el.width; c.height = el.naturalHeight || el.height;"
              "c.getContext('2d').drawImage(el, 0, 0, c.width, c.height); return c.toDataURL('image/png'); })()")
    result = (actions.evaluate(session_name(), script) or {}).get("result")
    if isinstance(result, str) and result.startswith("data:image/png;base64,"):
        return base64.b64decode(result.split(",", 1)[1])
    raise RecordError("读不到验证码图片的像素（目标不是图片或画布，或图片来自其他站点）；这一次请用户手动输入验证码")


def start(project_id: str, login: bool = True, as_login: bool = False, url: str | None = None) -> dict:
    """Open the project's address and begin recording actions and traffic."""
    notes = []
    previous = current(required=False)
    if previous:
        if _page_state()["url"].startswith(WEB):
            raise RecordError("已有进行中的录制；先运行 autoskill record stop 或 autoskill record cancel")
        _file().unlink(missing_ok=True)  # its browser is gone, so it can never be finished
        notes.append(f"上一次没有结束的录制（{len(previous['steps'])} 步）的浏览器已经关闭，无法继续，已丢弃")
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
    rec = {"project_id": project_id, "started_at": db.now(), "start_url": relative(start_url, config["base_url"]),
           "steps": [], "refs": {}, "secrets": [], "used_login": used_login, "as_login": as_login,
           "rand": uuid.uuid4().hex[:6], "frame_path": []}
    ir.set_rand(rec["rand"])
    agent.call(session_name(), ["close"], check=False, timeout=30)
    try:
        launch = [*agent.launch_args(config["allowed_origins"], state_file, bool(config.get("ignore_https_errors")),
                                    config=config, asset_root=projects.path(project_id)),
                  "--no-auto-dialog"]
        try:
            agent.call(session_name(), ["open", "about:blank"], launch=launch, timeout=90)
        except agent.AgentError as error:
            agent.call(session_name(), ["close"], check=False, timeout=30)
            raise RecordError(agent.launch_problem(error)) from None
        _fit_window()
        agent.call(session_name(), ["network", "har", "start"])
        try:
            page = actions.with_dialogs(session_name(), rec,
                                        lambda: _open(start_url, float(config.get("page_load_seconds", 90))))
        except (RecordError, agent.AgentError):
            agent.call(session_name(), ["close"], check=False, timeout=30)
            raise
    finally:
        if state_file:
            state_file.unlink(missing_ok=True)
    _observe_tabs(rec)
    tree = _settled_snapshot(rec)
    _save(rec)
    page = {**page, **_page_state()}  # the app may have moved on (for example to its sign-in page) while drawing
    result = {"recording": True, "project": project_id, "url": page["url"], "title": PageProbe().title(),
              "login": note or ("本次录制的就是登录流程" if as_login else "未登录状态"), "snapshot": tree}
    if page.get("slow"):
        notes.append(f"页面加载很慢，等了 {page['seconds']:.0f} 秒仍未完全加载，已继续；元素可能还没出齐，必要时再运行 autoskill browser snapshot")
    elif page["seconds"] >= 20:
        notes.append(f"页面加载用了 {page['seconds']:.0f} 秒")
    if relative(page["url"], config["base_url"]).rstrip("/") != rec["start_url"].rstrip("/"):
        result["redirected"] = f"打开的是 {start_url}，页面跳转到了 {page['url']}"
    if page["password"] and not as_login and not used_login:
        result["needs_login"] = ("页面上有密码输入框，看起来要先登录才能继续。运行 autoskill record cancel 放弃这次录制，"
                                 "再按 SKILL.md「登录」一节用 --as-login 录一次登录；账号密码向用户要，不要猜。"
                                 "如果要测的就是这个登录页本身，可以直接继续")
    if notes:
        result["notes"] = notes
    return result


def _record(rec: dict, step: dict) -> dict:
    rec["steps"].append(step)
    _save(rec)
    return {"recorded": len(rec["steps"]), "step": ir.describe(step)}


def _after(rec: dict, result: dict) -> dict:
    """Report the active page and its stable recording tab id."""
    url = PageProbe().url()
    result["url"] = url
    if rec.get("active_tab"):
        result["tab"] = rec["tab_map"][rec["active_tab"]]
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
    if removed.get("action") == "dialog":
        policy = {key: value for key, value in removed.items() if key != "action"}
        if rec.get("dialog_policy") == policy:
            rec.pop("dialog_policy", None)
    _save(rec)
    return {"removed": ir.describe(removed), "remaining": len(rec["steps"]),
            "note": "只撤销了记录；页面上已经发生的操作不会回退"}


def status() -> dict:
    rec = current(required=False)
    if not rec:
        return {"recording": False}
    return {"recording": True, "project": rec["project_id"], "started_at": rec["started_at"],
            "start_url": rec["start_url"], "steps": [ir.describe(s) for s in rec["steps"]],
            "prepared_dialog": rec.get("dialog_policy"), "frame_path": rec.get("frame_path", []),
            "artifacts": rec.get("artifacts", [])}


def cancel() -> dict:
    from . import native

    rec = current(required=False)
    evidence = native.finish_captures(session_name(), rec) if rec else []
    if rec:
        native.stop_permission_holder(rec)
    agent.call(session_name(), ["close"], check=False, timeout=30)
    existed = _file().is_file()
    _file().unlink(missing_ok=True)
    return {"cancelled": existed, "artifacts": evidence}


def _plain(text: str) -> str:
    """`secret:NAME` is a shell-friendly spelling of ${ENV:NAME}."""
    return "${ENV:" + text[7:] + "}" if text.startswith("secret:") and secrets.NAME.fullmatch(text[7:]) else text


def _native(rec: dict | None, argv: list[str]) -> dict:
    """Expose native capabilities while keeping unsupported mutations visible."""
    from . import native

    _, native_command, native_rest = native.split_command(argv)
    if rec and rec.get("frame_path") and native_command == "snapshot":
        raise RecordError("选中的 iframe 使用局部快照；原生 snapshot 选项请先 browser frame main")
    frame_targeted = {"click", "dblclick", "focus", "fill", "type", "press", "key", "keydown", "keyup", "keyboard",
                      "hover", "check", "uncheck", "select", "scroll", "scrollintoview", "scrollinto", "drag",
                      "upload", "download", "find", "tap", "swipe", "highlight", "wait", "storage"}
    targeted_read = native_command in ("get", "is") and native_rest[:1] not in (["url"], ["title"], ["cdp-url"])
    if rec and rec.get("frame_path") and (native_command in frame_targeted or targeted_read):
        raise RecordError(f"原生 {native_command} 的这个参数形式不能作用于选中的 iframe，操作尚未执行；"
                          "请使用可回放的 click/fill/drag/get/is/wait/scroll/eval 参数形式，或先 browser frame main")
    if rec and argv[:2] in (["set", "geo"], ["set", "geolocation"]):
        actions.grant_permission("geolocation")
    outcome = native.execute(session_name(), argv, rec,
                             store_value=(lambda text, label: _secret_ref(rec, text, label)) if rec else None)
    if outcome is None:
        raise RecordError(f"原生浏览器没有处理 {argv[0]}")
    result = {"result": outcome.get("result")}
    if rec and rec.get("frame_path"):
        result["scope"] = "page" if native_command in ("screenshot", "pdf", "record", "trace", "profiler") else "browser"
    if outcome.get("artifacts"):
        result["artifacts"] = outcome["artifacts"]
    if rec:
        step = outcome.get("step")
        if native_command == "snapshot":
            data = outcome.get("result")
            rec["refs"] = data.get("refs", {}) if isinstance(data, dict) else {}
        if step and step.get("action") == "browser_command" and step.get("command") == "set" and step.get("args"):
            rec.setdefault("browser_settings", {})[step["args"][0]] = step
        if outcome.get("affects_page"):
            change = _observe_tabs(rec)
            if change and step:
                step["tabs"] = change
        if step:
            result.update(_record(rec, step))
            if step.get("action") == "native_step":
                result.update(replayable=False, warning=step["reason"])
        if outcome.get("affects_page"):
            result["snapshot"] = snapshot(rec)
            result = _after(rec, result)
        _save(rec)
    return result


def _send_target(token: str, found: dict) -> str:
    """Native CSS/ref target matching a richer recorder locator."""
    if token.startswith(("testid=", "text=", "label=", "placeholder=", "role=")):
        return next((loc["value"] for loc in found["locators"] if loc["by"] == "css"), token)
    return token


def _frame_observation(rec: dict, command: str, rest: list[str]):
    what, rest = (rest[0], rest[1:]) if rest else ("", [])
    if not rest or what in ("url", "title", "cdp-url"):
        return None
    spec = _spec(rec, rest[0], rich=True)
    locator = spec.get("locator")
    if locator is None:
        locator = _target(rec, rest[0], rich=True)["locators"][-1]
    script = """(() => {
      const items=(DOM_SOURCE)().find(LOCATOR), el=items[INDEX];
      if(WHAT==='count')return items.length;
      if(!el)return null;
      if(WHAT==='visible')return !!el.getClientRects().length && getComputedStyle(el).visibility!=='hidden';
      if(WHAT==='enabled')return !el.disabled && el.getAttribute('aria-disabled')!=='true';
      if(WHAT==='checked')return !!el.checked;
      if(WHAT==='text')return el.innerText==null?el.textContent:el.innerText;
      if(WHAT==='html')return el.innerHTML;
      if(WHAT==='value')return el.value;
      if(WHAT==='attr')return el.getAttribute(ARG);
      if(WHAT==='box'){const b=el.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height};}
      if(WHAT==='styles'){const style=getComputedStyle(el);return Object.fromEntries([...style].map(k=>[k,style.getPropertyValue(k)]));}
      return null;
    })()""".replace("DOM_SOURCE", DOM_JS).replace("LOCATOR", json.dumps(locator)).replace("INDEX", str(locator.get("nth", 0)))
    script = script.replace("WHAT", json.dumps(what)).replace("ARG", json.dumps(rest[1] if len(rest) > 1 else ""))
    if what not in ("text", "html", "value", "attr", "count", "box", "styles", "visible", "enabled", "checked"):
        raise RecordError("当前框架不支持此读取命令；先 browser frame main 使用原生命令")
    value = (actions.evaluate(session_name(), script) or {}).get("result")
    if what != "count" and value is None:
        raise RecordError("当前框架内找不到目标")
    return {"value" if what == "attr" else what: value}


def _dialog(rec: dict, rest: list[str]) -> dict:
    if rest == ["status"]:
        return {"result": agent.call(session_name(), ["dialog", "status"]),
                "prepared": rec.get("dialog_policy")}
    if not rest or rest[0] not in ("accept", "dismiss") or len(rest) > (2 if rest[0] == "accept" else 1):
        raise RecordError("用法：browser dialog accept [文字] / dismiss / status；在触发弹窗之前准备处理方式")
    if rec.get("dialog_policy"):
        raise RecordError("上一次 dialog 策略尚未被弹窗使用；先触发该弹窗，或 record undo 撤销策略")
    status = agent.call(session_name(), ["dialog", "status"]) or {}
    # Native accepts an already-open modal; this is not a reproducible preparation
    # unless its triggering action is known. Keep such recovery explicit.
    is_open = any(status.get(key) for key in ("open", "isOpen", "hasDialog", "dialog"))
    step = {"action": "dialog", "mode": rest[0]}
    if len(rest) == 2:
        step["text"] = _secret_ref(rec, _plain(rest[1]), "DIALOG_TEXT")
    if is_open:
        send = ["dialog", rest[0]]
        if "text" in step:
            send.append(secrets.resolve(ir.expand(step["text"])))
        value = agent.call(session_name(), send)
        blocked = {"action": "native_step", "command": "dialog", "args": [rest[0], *([step["text"]] if "text" in step else [])],
                   "reason": "恢复了已打开的弹窗，无法确定触发动作；请在触发之前录制 dialog 策略", "replayable": False}
        result = _record(rec, blocked)
        result.update(result=value, replayable=False, warning=blocked["reason"])
        return result
    rec["dialog_policy"] = {key: value for key, value in step.items() if key != "action"}
    result = _record(rec, step)
    result["prepared"] = "处理下一次对话框；请继续执行触发它的操作"
    return result


def browser(argv: list[str]) -> dict:
    """Run native browser abilities, recording every portable or blocking mutation."""
    if not argv:
        raise RecordError("缺少浏览器命令，例如：autoskill browser click @e3")
    # autoSkill always emits JSON; a redundant native prefix must not bypass
    # portable recording/frame bookkeeping.
    while argv and argv[0] in ("--json", "--json=true"):
        argv = argv[1:]
    if not argv:
        raise RecordError("--json 后缺少浏览器命令")
    aliases = {"goto": "open", "navigate": "open", "key": "press", "scrollinto": "scrollintoview"}
    argv = [aliases.get(argv[0], argv[0]), *argv[1:]]
    rec = current(required=False)
    if not rec:
        return _native(None, argv)
    from . import native

    native.guard_recording(argv, rec)
    if argv[0] == "dialog":
        return _dialog(rec, argv[1:])
    # Native lifecycle/admin guards run before attaching. They preserve an
    # active recorder instead of switching its browser or losing captured HAR.
    if argv[0] in ("close", "quit", "exit", "connect", "install", "upgrade", "update", "doctor", "skills",
                   "session", "sessions", "dashboard", "mcp") or argv[0].startswith("-"):
        return _native(rec, argv)
    try:
        return actions.with_dialogs(session_name(), rec, lambda: _browser(rec, argv))
    finally:
        _save(rec)  # consumed dialog policy and artifact ownership survive action errors


def _browser(rec: dict, argv: list[str]) -> dict:
    command, rest = argv[0], argv[1:]
    config = projects.config(rec["project_id"])
    _sync_tabs(rec)
    early_change = None
    if command == "tab":
        return _tab_command(rec, rest)
    if command == "snapshot":
        options = [value for value in rest if value != "--all"]
        if options:
            if rec.get("frame_path"):
                raise RecordError("选中的 iframe 使用局部快照；复杂原生 snapshot 选项请先 browser frame main")
            return _native(rec, ["snapshot", *options])
        tree = snapshot(rec, interactive="--all" not in rest)
        _save(rec)
        return {"snapshot": tree}
    if command == "describe":
        if not rest:
            raise RecordError("describe 需要目标，例如 @e5")
        found = _target(rec, rest[0], rich=True)
        return {"element": found.get("tag"), "role": found.get("role"), "name": found.get("name"),
                "locators": [ir.describe_locator(loc) for loc in found["locators"]],
                "use_as_target": [_as_target(loc) for loc in found["locators"]]}
    if command in ("get", "is"):
        result = _frame_observation(rec, command, rest) if rec.get("frame_path") else None
        if result is None:
            result = agent.call(session_name(), argv)
        return {"result": result}
    if command == "open":
        if not rest:
            raise RecordError("open 需要地址")
        if len(rest) != 1:
            return _native(rec, argv)
        url = ir.absolute_url(rest[0], config["base_url"])
        ir.check_origin(url, config["allowed_origins"])
        _open(url, float(config.get("page_load_seconds", 90)))
        step = {"action": "goto", "url": relative(url, config["base_url"])}
        rec["frame_path"] = []
    elif command in ("reload", "back", "forward"):
        if rest:
            return _native(rec, argv)
        agent.call(session_name(), [command], timeout=90)
        _wait_ready(float(config.get("page_load_seconds", 90)))
        step = {"action": command}
        rec["frame_path"] = []
    elif command == "press":
        if len(rest) != 1:
            raise RecordError("press 需要按键名，例如 Enter")
        focused = _describe({})
        sensitive = bool((focused or {}).get("secret")) and len(rest[0]) == 1
        typed, recorded = _secret_value(rec, _plain(rest[0]), sensitive)
        step = {"action": "keyboard", "operation": "press", "value": recorded} if sensitive else {"action": "press", "key": recorded}
        input_step = {**step, "value" if sensitive else "key": typed}
        if not actions.input_action(rec, input_step):
            early_change = _act(rec, ["press", typed])
        if focused and focused.get("locators"):
            step["locators"] = focused["locators"]
    elif command == "captcha":
        if len(rest) < 2:
            raise RecordError("captcha 需要两个目标：验证码图片和输入框，例如 autoskill browser captcha @e4 @e5")
        from .. import captcha

        picture, field = _target(rec, rest[0], rich=True), _target(rec, rest[1])
        recognized = captcha.solve(_element_png(picture["locators"][0]))
        step = {"action": "captcha", "locators": field["locators"], "image": picture["locators"]}
        if not actions.target_action(rec, {"action": "fill", "locators": field["locators"], "value": recognized}):
            early_change = _act(rec, ["fill", rest[1], recognized])
    elif command == "frame":
        if len(rest) != 1:
            raise RecordError("frame 需要 iframe 选择器、@引用或 main")
        selector = rest[0]
        if selector != "main":
            try:
                found = _target(rec, selector, rich=True)
            except RecordError:
                raise RecordError("录制中的 frame 需要 iframe 的 CSS 选择器或 @引用；原生名称/地址无法保留相对框架路径") from None
            if found.get("tag") not in ("iframe", "frame"):
                raise RecordError("frame 的目标必须是 iframe 或 frame 元素")
            selector = next((loc["value"] for loc in found["locators"] if loc["by"] == "css"), selector)
        if selector != "main":
            actions.frame_target(rec, selector)
        else:
            agent.call(session_name(), ["frame", "main"])
        rec["frame_path"] = [] if selector == "main" else [*rec.get("frame_path", []), selector]
        rec["refs"] = {}
        step = {"action": "frame", "selector": selector}
    elif command == "drag":
        if len(rest) != 2:
            if len(rest) > 2:
                return _native(rec, argv)
            raise RecordError("drag 需要起点和终点目标")
        source, target = _target(rec, rest[0], rich=True), _target(rec, rest[1], rich=True)
        step = {"action": "drag", "locators": source["locators"], "target_locators": target["locators"]}
        if not actions.target_action(rec, step):
            early_change = _act(rec, ["drag", _send_target(rest[0], source), _send_target(rest[1], target)])
    elif command == "eval":
        if len(rest) == 2 and rest[0] in ("-b", "--base64"):
            try:
                script = base64.b64decode(rest[1], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise RecordError("eval 的 base64 不是 UTF-8 JavaScript") from None
        elif len(rest) == 2 and rest[0] == "--script-file":
            script = Path(rest[1]).read_text(encoding="utf-8")
        elif len(rest) == 1 and rest[0] != "--stdin":
            script = rest[0]
        else:
            raise RecordError("eval 需要 JavaScript 表达式/函数、-b BASE64 或 --script-file 文件")
        script = _plain(script)
        # An entire named script is already opaque. Mixed ENV substitutions
        # are resolved into a local script value so literal credentials beside
        # the substitution cannot accidentally enter a shareable case.
        reference = script if secrets.REF.fullmatch(script) else _secret_ref(rec, secrets.resolve(script), "EVAL_SCRIPT")
        rec["secrets"] = sorted({*rec["secrets"], *secrets.references(reference)})
        value = actions.evaluate(session_name(), actions.expression(ir.expand(secrets.resolve(reference))))
        step = {"action": "eval", "script": reference}
    elif command in ("keyboard", "keydown", "keyup"):
        keyboard = rest if command == "keyboard" else ["down" if command == "keydown" else "up", *rest]
        if len(keyboard) != 2 or keyboard[0] not in ("type", "inserttext", "press", "down", "up"):
            return _native(rec, argv)
        operation, text = keyboard
        focused = _describe({})
        typed, recorded = _secret_value(rec, _plain(text), bool((focused or {}).get("secret")) and
                                        (operation in ("type", "inserttext") or len(text) == 1))
        send = ["keyboard", operation, typed] if operation in ("type", "inserttext") else [
            {"press": "press", "down": "keydown", "up": "keyup"}[operation], typed]
        step = {"action": "keyboard", "operation": operation, "value": recorded}
        if not actions.input_action(rec, {**step, "value": typed}):
            early_change = _act(rec, send)
    elif command == "mouse":
        if not rest:
            raise RecordError("mouse 需要 move、down、up 或 wheel")
        operation, values = rest[0], rest[1:]
        step = {"action": "mouse", "operation": operation}
        if operation == "move" and len(values) == 2:
            step.update(x=actions.number(values[0], "x"), y=actions.number(values[1], "y"))
        elif operation in ("down", "up") and len(values) <= 1:
            step["button"] = values[0] if values else "left"
            if step["button"] not in ("left", "right", "middle"):
                raise RecordError("鼠标按钮只能是 left、right 或 middle")
        elif operation == "wheel" and len(values) <= 2:
            step.update(delta_y=actions.number(values[0], "delta_y") if values else 100,
                        delta_x=actions.number(values[1], "delta_x") if len(values) == 2 else 0)
        else:
            return _native(rec, argv)
        if not actions.input_action(rec, step):
            early_change = _act(rec, argv)
    elif command == "scroll":
        direction, pixels, target_token = "down", 300, None
        parts = list(rest)
        if parts and not parts[0].startswith("-"):
            direction = parts.pop(0)
        if direction not in ("up", "down", "left", "right"):
            raise RecordError("滚动方向只能是 up、down、left 或 right")
        if parts and not parts[0].startswith("--"):
            pixels = actions.number(parts.pop(0), "滚动像素", minimum=0)
        if parts and parts[0] in ("--selector", "-s") and len(parts) == 2:
            target_token = parts[1]
        elif parts:
            return _native(rec, argv)
        step = {"action": "scroll", "direction": direction, "pixels": pixels}
        send = ["scroll", direction, str(pixels)]
        if target_token:
            found = _target(rec, target_token, rich=True)
            step["locators"] = found["locators"]
            send += ["--selector", _send_target(target_token, found)]
        if rec.get("frame_path"):
            from ..replay.primitives import scroll_delta

            dx, dy = scroll_delta(direction, pixels)
            if target_token:
                locator = next(loc for loc in found["locators"] if loc["by"] == "css")
                expression = "(" + DOM_JS + ")().find(" + json.dumps(locator) + ")[" + str(locator.get("nth", 0)) + "]"
            else:
                expression = "window"
            actions.evaluate(session_name(), expression + ".scrollBy(" + str(dx) + "," + str(dy) + ")")
        else:
            early_change = _act(rec, send)
    elif command == "wait":
        if not rest:
            raise RecordError("wait 需要毫秒数或目标 [--state visible|hidden|attached|detached]")
        operands, options, index = [], {}, 0
        while index < len(rest):
            token = rest[index]
            if token in ("--timeout", "--stable-for", "--state") and index + 1 < len(rest) and token not in options:
                options[token] = rest[index + 1]
                index += 2
            else:
                operands.append(token)
                index += 1
        timing = {}
        for flag, field in (("--timeout", "timeout_seconds"), ("--stable-for", "stable_for_seconds")):
            if flag in options:
                timing[field] = actions.number(options[flag], flag, minimum=0) / 1000
        if timing.get("timeout_seconds") == 0:
            raise RecordError("wait --timeout 必须大于 0 毫秒")
        if len(operands) == 1 and re.fullmatch(r"\d+(?:\.\d+)?", operands[0]) and "--state" not in options:
            step = {"action": "wait", "milliseconds": actions.number(operands[0], "等待毫秒", minimum=0), **timing}
            agent.call(session_name(), ["wait", operands[0]], timeout=max(60, step["milliseconds"] / 1000 + 5))
        elif len(operands) == 1 and not operands[0].startswith("-"):
            state = options.get("--state", "visible")
            if state not in ("visible", "hidden", "attached", "detached"):
                raise RecordError("wait --state 只能是 visible、hidden、attached 或 detached")
            token = operands[0]
            spec = _spec(rec, token, rich=True)
            # Hidden/detached waits must not require the element to exist now.
            if token.startswith("@"):
                found = _target(rec, token, rich=True)
                selector = next(loc["value"] for loc in found["locators"] if loc["by"] == "css")
            elif spec.get("locator", {}).get("by") == "css":
                selector = token
            else:
                found = _target(rec, token, rich=True)
                selector = _send_target(token, found)
            step = {"action": "wait", "selector": selector, "state": state, **timing}
            if not timing and not rec.get("frame_path"):
                agent.call(session_name(), ["wait", selector, "--state", state])
            else:
                timeout = float(timing.get("timeout_seconds", config.get("timeout_seconds", 20)))
                stability = float(timing.get("stable_for_seconds", 0))
                deadline, since = time.monotonic() + timeout, None
                probe, locator = PageProbe(), {"by": "css", "value": selector}
                while True:
                    exists = probe.count(locator) > 0
                    visible = exists and bool(probe.read(locator, "visible"))
                    met = {"attached": exists, "detached": not exists, "visible": visible, "hidden": not visible}[state]
                    now = time.monotonic()
                    since = (since if since is not None else now) if met else None
                    if since is not None and now - since >= stability:
                        break
                    if now >= deadline:
                        raise RecordError("等待条件在时限内没有持续成立：" + ir.describe(step))
                    time.sleep(min(0.1, max(0, deadline - now)))
        else:
            return _native(rec, argv)
    elif command == "upload":
        if len(rest) < 2:
            raise RecordError("upload 需要目标和一个或多个文件")
        from .. import artifacts

        found = _target(rec, rest[0], rich=True)
        files = artifacts.stage_upload(rec["project_id"], rest[1:])
        asset_root = projects.path(rec["project_id"])
        absolute = artifacts.resolve_upload_assets(asset_root, files)
        step = {"action": "upload", "locators": found["locators"], "files": files}
        if not actions.target_action(rec, {**step, "files": absolute}):
            early_change = _act(rec, ["upload", _send_target(rest[0], found), *absolute])
    elif command == "download":
        if len(rest) != 2:
            raise RecordError("download 需要目标和相对输出名，例如 download @e1 report.csv")
        from .. import artifacts

        found = _target(rec, rest[0], rich=True)
        filename = rest[1].replace("\\", "/")
        # Reuse IR's portable-path check before writing any machine output.
        step = {"action": "download", "locators": found["locators"], "filename": filename}
        ir.validate({"start_url": "/", "actions": [step], "assertions": []})
        output = artifacts.private_output(session_name(), filename, category="downloads", rec=rec)
        from ..replay.primitives import output_file

        output = output_file(output.parent, output.name)
        if not actions.target_action(rec, {**step, "output": str(output)}):
            agent.call(session_name(), ["download", _send_target(rest[0], found), str(output)])
        rec.setdefault("artifacts", []).append(artifacts.metadata(output, kind="download"))
    elif command in TARGETED:
        if not rest:
            raise RecordError(f"{command} 需要目标，例如 @e3")
        expected_args = 2 if command in ("fill", "type", "select") else 1
        text_args = rest[1:]
        if command in ("fill", "type") and text_args[:1] == ["--"]:
            text_args = text_args[1:]
        if len(text_args) != expected_args - 1:
            return _native(rec, argv)
        found = _target(rec, rest[0], rich=True)  # describe before acting: the element may be gone afterwards
        what = f'{found.get("role") or found.get("tag")}「{found.get("name", "")}」'
        if command in ("fill", "type") and not found.get("editable"):
            raise RecordError(f"{rest[0]} 是 {what}，不能输入文字；编号可能来自旧的 snapshot，请用最近一次返回的 snapshot 重新确认")
        if command == "select" and found.get("tag") != "select":
            raise RecordError(f"{rest[0]} 是 {what}，不是下拉框；自定义下拉请用 click 打开后再 click 选项")
        if command in ("check", "uncheck") and not found.get("checkable"):
            raise RecordError(f"{rest[0]} 是 {what}，不是复选框或单选框；请用最近一次返回的 snapshot 重新确认")
        step = {"action": "scroll_into_view" if command == "scrollintoview" else command, "locators": found["locators"]}
        send = [command, _send_target(rest[0], found)]
        if command in ("fill", "type", "select"):
            if len(rest) < 2:
                raise RecordError(f"{command} 需要内容")
            typed, step["value"] = _secret_value(rec, _plain(text_args[0]), bool(found.get("secret")) and command != "select")
            send += [*(["--"] if command in ("fill", "type") else []), typed]
        if not actions.target_action(rec, {**step, **({"value": typed} if command in ("fill", "type", "select") else {})}):
            early_change = _act(rec, send)
    else:
        return _native(rec, argv)
    change = _observe_tabs(rec, settle=0.6 if command in ("click", "dblclick", "press") else 0)
    if early_change:
        change = {"opened": early_change["opened"] + (change or {}).get("opened", []),
                  "closed": early_change["closed"] + (change or {}).get("closed", []),
                  "active": (change or early_change)["active"]}
    if change:
        step["tabs"] = change
        if change.get("opened"):
            _wait_ready(float(config.get("page_load_seconds", 90)))
    result = _record(rec, step)
    if command == "captcha":
        result["recognized"] = recognized
    if command == "eval":
        result["result"] = value
    if command == "download":
        result["artifacts"] = rec["artifacts"][-1:]
    result["snapshot"] = snapshot(rec)
    _save(rec)
    return _after(rec, result)


def check(kind: str, target: str | None, expected: str | None, attribute: str | None = None,
          *, timeout_seconds: float | None = None, stable_for_seconds: float | None = None) -> dict:
    rec = current()
    try:
        return actions.with_dialogs(session_name(), rec, lambda: _check(rec, kind, target, expected, attribute,
                                   timeout_seconds=timeout_seconds, stable_for_seconds=stable_for_seconds))
    finally:
        _save(rec)


def _check(rec: dict, kind: str, target: str | None, expected: str | None, attribute: str | None = None,
           *, timeout_seconds: float | None = None, stable_for_seconds: float | None = None) -> dict:
    """Verify an expectation on the live page now; only a passing one is recorded."""
    from ..replay import ui

    _sync_tabs(rec)
    if kind not in ir.ASSERTIONS:
        raise RecordError("断言类型只能是：" + " ".join(ir.ASSERTIONS))
    needs_target, required = ir.ASSERTIONS[kind]
    if not needs_target and expected is None:
        target, expected = None, target  # `check url_contains "/orders"`: the one value is the expectation
    step: dict = {"assertion": kind}
    if timeout_seconds is not None:
        step["timeout_seconds"] = timeout_seconds
    if stable_for_seconds is not None:
        step["stable_for_seconds"] = stable_for_seconds
    if needs_target:
        if not target:
            raise RecordError(f"{kind} 断言需要目标，例如 @e5、testid=toast 或 CSS 选择器")
        if kind in ("count", "min_count"):
            step["locators"] = [_spec(rec, target, rich=True).get("locator") or {"by": "css", "value": target}]
        else:
            found = _target(rec, target, rich=True)["locators"]
            explicit = _spec(rec, target, rich=True).get("locator")
            given_as_text = bool(explicit) and explicit["by"] == "text"
            if not given_as_text:  # locating an element by the very text being asserted proves nothing
                found = [loc for loc in found if loc["by"] != "text"] or found
            if explicit and explicit["by"] == "css" and all(loc != explicit for loc in found):
                found = [explicit, *found]  # the recorder chose this selector on purpose
            step["locators"] = found
    if "expected" in required:
        if expected is None:
            raise RecordError(f"{kind} 断言需要期望值")
        step["expected"] = int(expected) if kind in ("count", "min_count") and expected.isdigit() else expected
    elif kind == "checked" and expected is not None:
        step["expected"] = expected.lower() not in ("false", "0", "no")
    if kind == "attribute":
        if not attribute:
            raise RecordError("attribute 断言需要 --attribute 属性名")
        step["attribute"] = attribute
    ir.validate({"start_url": "/", "actions": [], "assertions": [step]})
    outcome = ui.check(PageProbe(), step, 5.0)
    if not outcome["passed"]:
        raise RecordError(f"断言现在不成立，没有记录：{ir.describe(step)}；页面实际为「{_redact(rec, outcome['actual'])}」")
    return {**_record(rec, step), "actual": _redact(rec, outcome["actual"])}


def stop(title: str, module: str = "", category: str = "", plan_case: str | None = None) -> dict:
    """Finish the recording: export traffic, close the browser, compile and save the cases."""
    from . import compile as compiler
    from . import native

    rec = current()
    if not (title or "").strip():
        raise RecordError("请用 --title 给这条用例起一个说明业务含义的名字")
    if not rec["steps"]:
        raise RecordError("还没有记录任何步骤；先用 autoskill browser 操作页面，或运行 autoskill record cancel 放弃")
    har_file, state_file = paths.local() / "recording.har", paths.local() / "recording-state.json"
    har, state = {"log": {"entries": []}}, None
    # A failed encoder/export keeps the recording and browser available for retry.
    native.finish_captures(session_name(), rec)
    native.stop_permission_holder(rec)
    _save(rec)
    try:
        if agent.call(session_name(), ["network", "har", "stop", str(har_file)], check=False) is not None and har_file.is_file():
            har = json.loads(har_file.read_text(encoding="utf-8", errors="replace"))
        if agent.call(session_name(), ["state", "save", str(state_file)], check=False) is not None and state_file.is_file():
            state = json.loads(state_file.read_text(encoding="utf-8"))
    finally:
        agent.call(session_name(), ["close"], check=False, timeout=30)
        har_file.unlink(missing_ok=True)  # raw traffic and cookies never stay on disk
        state_file.unlink(missing_ok=True)
    summary = compiler.build(rec["project_id"], rec, har, state, title.strip(), module, category)
    _file().unlink(missing_ok=True)
    summary["project"] = rec["project_id"]
    if rec.get("artifacts"):
        summary["artifacts"] = rec["artifacts"]
    if plan_case:  # this recording carried out a row of the case sheet: its replay result becomes that row's verdict
        from .. import plan

        summary["plan"] = plan.link(plan_case, rec["project_id"], summary["ui_case"]["id"], summary["ui_case"]["assertions"])
    return summary
