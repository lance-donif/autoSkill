"""One recording per AI session: every browser action becomes a replayable step."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import db, paths, projects, secrets, task_context
from ..locking import project_lock
from ..replay import ir
from ..replay.tabs import Tabs
from . import agent


def session_name() -> str:
    """Separate browser daemons by workspace and task while preserving the legacy default name."""
    identity = task_context.session_id()
    if identity is None:
        return "autoskill-" + hashlib.sha1(str(paths.workspace()).lower().encode("utf-8")).hexdigest()[:10]
    value = json.dumps([str(paths.workspace()), identity], ensure_ascii=False).encode("utf-8")
    return "autoskill-" + hashlib.sha256(value).hexdigest()[:20]
DOM_JS = (Path(__file__).resolve().parents[1] / "replay" / "dom.js").read_text(encoding="utf-8")
TARGETED = ("click", "dblclick", "hover", "check", "uncheck", "fill", "type", "select")
READ_ONLY = ("snapshot", "get", "is", "wait", "screenshot", "scroll", "scrollintoview", "focus", "eval")
DENIED = ("install", "upgrade", "update", "doctor", "skills", "connect", "dashboard", "auth", "state", "cookies",
          "storage", "network", "close", "record", "flow", "mcp", "chat", "batch", "pdf", "download", "upload")
DENIED_FLAGS = ("--provider", "-p", "--proxy", "--cdp", "--auto-connect", "--config", "--executable-path", "--args",
                "--allowed-domains", "--state", "--profile", "--session", "--headers", "--extension", "--init-script",
                "--restore", "--capture", "--namespace", "--engine")


class RecordError(Exception):
    pass


def recording_dir() -> Path:
    folder = paths.local()
    if task_context.session_id() is not None:
        folder = folder / "recordings" / task_context.session_key()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _file() -> Path:
    return recording_dir() / "recording.json"


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
    for _attempt in (1, 2):
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
    data = agent.call(session_name(), ["snapshot", *(["-i"] if interactive else [])]) or {}
    rec["refs"] = data.get("refs", {})
    return data.get("snapshot", "")


def _describe(spec: dict) -> dict | None:
    script = "JSON.stringify((" + DOM_JS + ")().target(" + json.dumps(spec, ensure_ascii=False) + "))"
    result = (agent.call(session_name(), ["eval", "--stdin"], stdin_text=script) or {}).get("result")
    return json.loads(result) if isinstance(result, str) and result != "null" else None


def _spec(rec: dict, token: str, rich: bool = False) -> dict:
    if token.startswith("@"):
        ref = rec["refs"].get(token[1:])
        if not ref:
            raise RecordError(f"引用 {token} 已失效；先运行 autoskill browser snapshot 再操作")
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
        result = (agent.call(session_name(), ["eval", "--stdin"], stdin_text=script) or {}).get("result")
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
    result = (agent.call(session_name(), ["eval", "--stdin"], stdin_text=script, check=False) or {}).get("result")
    if isinstance(result, str) and result.startswith("data:image/png;base64,"):
        return base64.b64decode(result.split(",", 1)[1])
    raise RecordError("读不到验证码图片的像素（目标不是图片或画布，或图片来自其他站点）；这一次请用户手动输入验证码")


def start(project_id: str, login: bool = True, as_login: bool = False, url: str | None = None) -> dict:
    """Open this task's browser on the project's address and record actions and traffic."""
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
        state_file = recording_dir() / "recording-state.json"
        state_file.write_text(json.dumps(sessions.load(project_id).get("ui", {})), encoding="utf-8")
        used_login, note = True, f"已用登录用例 {auth['ui_case']} 自动登录"
    agent.call(session_name(), ["close"], check=False, timeout=30)
    try:
        browsers = list(dict.fromkeys(b for b in (paths.chrome(), paths.chrome(bundled=True)) if b)) or [None]
        for index, browser in enumerate(browsers):  # the user's own Chrome first, ours when it will not start
            launch = agent.launch_args(config["allowed_origins"], state_file, bool(config.get("ignore_https_errors")), browser)
            try:
                agent.call(session_name(), ["open", "about:blank"], launch=launch, timeout=90)
                break
            except agent.AgentError as error:
                agent.call(session_name(), ["close"], check=False, timeout=30)
                if index == len(browsers) - 1:
                    raise RecordError(agent.launch_problem(error)) from None
        _fit_window()
        agent.call(session_name(), ["network", "har", "start"])
        try:
            page = _open(start_url, float(config.get("page_load_seconds", 90)))
        except RecordError:
            agent.call(session_name(), ["close"], check=False, timeout=30)
            raise
    finally:
        if state_file:
            state_file.unlink(missing_ok=True)
    rec = {"project_id": project_id, "session_id": task_context.session_id(), "started_at": db.now(), "start_url": relative(start_url, config["base_url"]),
           "steps": [], "refs": {}, "secrets": [], "used_login": used_login, "as_login": as_login,
           "rand": uuid.uuid4().hex[:6]}
    ir.set_rand(rec["rand"])
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
    agent.call(session_name(), ["close"], check=False, timeout=30)
    existed = _file().is_file()
    _file().unlink(missing_ok=True)
    return {"cancelled": existed}


def _plain(text: str) -> str:
    """`secret:NAME` is a shell-friendly spelling of ${ENV:NAME}."""
    return "${ENV:" + text[7:] + "}" if text.startswith("secret:") and secrets.NAME.fullmatch(text[7:]) else text


def _targeted_step(rec: dict, command: str, rest: list[str], argv: list[str]) -> tuple[dict, dict | None]:
    """Describe and validate a target before sending its recorded action."""
    if not rest:
        raise RecordError(f"{command} 需要目标，例如 @e3")
    found = _target(rec, rest[0])  # describe before acting: the element may be gone afterwards
    what = f'{found.get("role") or found.get("tag")}「{found.get("name", "")}」'
    if command in ("fill", "type") and not found.get("editable"):
        raise RecordError(f"{rest[0]} 是 {what}，不能输入文字；编号可能来自旧的 snapshot，请用最近一次返回的 snapshot 重新确认")
    if command == "select" and found.get("tag") != "select":
        raise RecordError(f"{rest[0]} 是 {what}，不是下拉框；自定义下拉请用 click 打开后再 click 选项")
    if command in ("check", "uncheck") and not found.get("checkable"):
        raise RecordError(f"{rest[0]} 是 {what}，不是复选框或单选框；请用最近一次返回的 snapshot 重新确认")
    step = {"action": command, "locators": found["locators"]}
    send = list(argv)
    if command in ("fill", "type", "select"):
        if len(rest) < 2:
            raise RecordError(f"{command} 需要内容")
        typed, step["value"] = _secret_value(rec, _plain(rest[1]), bool(found.get("secret")) and command != "select")
        send = [command, rest[0], typed]
    early_change = _act(rec, send)
    return step, early_change


def _browser_action(rec: dict, command: str, rest: list[str], argv: list[str], config: dict) -> tuple[dict, dict | None, str | None]:
    """Execute a changing command and retain the step and any early tab changes."""
    early_change = None
    recognized = None
    if command == "open":
        if not rest:
            raise RecordError("open 需要地址")
        url = ir.absolute_url(rest[0], config["base_url"])
        ir.check_origin(url, config["allowed_origins"])
        _open(url, float(config.get("page_load_seconds", 90)))
        step = {"action": "goto", "url": relative(url, config["base_url"])}
    elif command == "reload":
        agent.call(session_name(), ["reload"], timeout=90, check=False)
        _wait_ready(float(config.get("page_load_seconds", 90)))
        step = {"action": "reload"}
    elif command == "press":
        if not rest:
            raise RecordError("press 需要按键名，例如 Enter")
        focused = _describe({})
        early_change = _act(rec, ["press", rest[0]])
        step = {"action": "press", "key": rest[0]}
        if focused and focused.get("locators"):
            step["locators"] = focused["locators"]
    elif command == "captcha":
        if len(rest) < 2:
            raise RecordError("captcha 需要两个目标：验证码图片和输入框，例如 autoskill browser captcha @e4 @e5")
        from .. import captcha

        picture, field = _target(rec, rest[0], rich=True), _target(rec, rest[1])
        recognized = captcha.solve(_element_png(picture["locators"][0]))
        early_change = _act(rec, ["fill", rest[1], recognized])
        step = {"action": "captcha", "locators": field["locators"], "image": picture["locators"]}
    elif command in TARGETED:
        step, early_change = _targeted_step(rec, command, rest, argv)
    else:
        raise RecordError(f"录制不支持 {command}；可用：open snapshot describe tab click dblclick hover fill type select check uncheck press reload captcha dialog get is wait")
    return step, early_change, recognized


def browser(argv: list[str]) -> dict:
    """Run one agent-browser command inside the recording and record it when it changes the page."""
    rec = current()
    if not argv:
        raise RecordError("缺少浏览器命令，例如：autoskill browser click @e3")
    command, rest = argv[0], argv[1:]
    if command in DENIED or any(a.split("=")[0] in DENIED_FLAGS for a in argv):
        raise RecordError(f"录制中不能使用 {command}：安装、升级、远程浏览器、网络与登录态由 autoskill 统一管理")
    config = projects.config(rec["project_id"])
    _sync_tabs(rec)
    if command == "tab":
        return _tab_command(rec, rest)
    if command == "snapshot":
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
    if command in ("get", "is", "wait", "screenshot", "scroll", "scrollintoview", "dialog"):
        result = agent.call(session_name(), argv)
        if command == "wait" and rest and rest[0].isdigit():
            result = {"waited_ms": int(rest[0])}
        return {"result": result}
    step, early_change, recognized = _browser_action(rec, command, rest, argv, config)
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
    result["snapshot"] = snapshot(rec)
    _save(rec)
    return _after(rec, result)


def check(kind: str, target: str | None, expected: str | None, attribute: str | None = None) -> dict:
    """Verify an expectation on the live page now; only a passing one is recorded."""
    from ..replay import ui

    rec = current()
    _sync_tabs(rec)
    if kind not in ir.ASSERTIONS:
        raise RecordError("断言类型只能是：" + " ".join(ir.ASSERTIONS))
    needs_target, required = ir.ASSERTIONS[kind]
    if not needs_target and expected is None:
        target, expected = None, target  # `check url_contains "/orders"`: the one value is the expectation
    step: dict = {"assertion": kind}
    if needs_target:
        if not target:
            raise RecordError(f"{kind} 断言需要目标，例如 @e5、testid=toast 或 CSS 选择器")
        explicit = _spec(rec, target, rich=True).get("locator")
        if kind in ("count", "min_count") or (kind == "hidden" and explicit):
            step["locators"] = [explicit or {"by": "css", "value": target}]
        else:
            found = _target(rec, target, rich=True)["locators"]
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
        raise RecordError(f"断言现在不成立，没有记录：{ir.describe(step)}；页面实际为「{outcome['actual']}」")
    return {**_record(rec, step), "actual": outcome["actual"]}


def stop(title: str, module: str = "", category: str = "", plan_case: str | None = None) -> dict:
    """Finish the recording: export traffic, close the browser, compile and save the cases."""
    from . import compile as compiler

    rec = current()
    if not (title or "").strip():
        raise RecordError("请用 --title 给这条用例起一个说明业务含义的名字")
    if not rec["steps"]:
        raise RecordError("还没有记录任何步骤；先用 autoskill browser 操作页面，或运行 autoskill record cancel 放弃")
    har_file, state_file = recording_dir() / "recording.har", recording_dir() / "recording-state.json"
    har, state = {"log": {"entries": []}}, None
    try:
        if agent.call(session_name(), ["network", "har", "stop", str(har_file)], check=False) is not None and har_file.is_file():
            har = json.loads(har_file.read_text(encoding="utf-8", errors="replace"))
        if agent.call(session_name(), ["state", "save", str(state_file)], check=False) is not None and state_file.is_file():
            state = json.loads(state_file.read_text(encoding="utf-8"))
    finally:
        agent.call(session_name(), ["close"], check=False, timeout=30)
        har_file.unlink(missing_ok=True)  # raw traffic and cookies never stay on disk
        state_file.unlink(missing_ok=True)
    with project_lock(rec["project_id"]):
        summary = compiler.build(rec["project_id"], rec, har, state, title.strip(), module, category)
    _file().unlink(missing_ok=True)
    summary["project"] = rec["project_id"]
    if plan_case:  # this recording carried out a row of the case sheet: its replay result becomes that row's verdict
        from .. import plan

        summary["plan"] = plan.link(plan_case, rec["project_id"], summary["ui_case"]["id"], summary["ui_case"]["assertions"])
    return summary
