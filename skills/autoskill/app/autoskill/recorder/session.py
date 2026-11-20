"""One recording per AI session: every browser action becomes a replayable step."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import data_refs, db, execution_inputs, paths, projects, secrets, task_context
from ..locking import project_lock
from ..replay import ir
from ..replay.tabs import Tabs
from . import agent, capture, screenshots
from .context import AttachedPage


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
        if required and rec.get("setup_status", "ready") != "ready":
            raise RecordError("录制尚未就绪。请用 record status 核对前置回执和现场后再处理")
        ir.set_rand(rec.get("rand", ""))
        return rec
    if required:
        raise RecordError("当前没有进行中的录制；先运行 autoskill record start <项目>")
    return None


def _save(rec: dict) -> None:
    execution_inputs.write_json(_file(), rec)


def _retain_recording(rec: dict) -> str:
    """Keep interrupted steps and source references outside the active recording slot."""
    file = recording_dir() / ("recording-retained-" + uuid.uuid4().hex + ".json")
    file.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return data_refs.store(file)


def _intent(rec: dict, kind: str, payload: dict, *, side_effect: bool = True) -> dict | None:
    from .. import workflow

    binding = rec.get("workflow_binding")
    if not binding:
        return None
    intent = workflow.operation_intent(kind, {"project_id": rec["project_id"], **payload}, binding=binding, side_effect=side_effect)
    rec.setdefault("operations", []).append(intent["operation_id"])
    _save(rec)
    return intent


def _receipt(intent: dict | None, observation: dict, *, status: str = "succeeded") -> None:
    from .. import workflow

    workflow.operation_receipt(intent, observation, status=status)


def _operation(rec: dict, command: str, payload: dict, action):
    """Journal the actual dispatch once; an uncertain outcome is never retried by this wrapper."""
    intent = _intent(rec, command, payload)
    try:
        result = action()
    except Exception as error:
        _receipt(intent, {"error": type(error).__name__, "command_completed": False}, status="unknown")
        raise
    _receipt(intent, {"command_completed": True, "result": result})
    return result


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
    if active != old_active:
        rec["context"] = []
        rec["refs"] = {}
    return change if opened or closed or active != old_active else None


def _sync_tabs(rec: dict) -> None:
    change = _observe_tabs(rec)
    if change:
        _record(rec, {"action": "tab_sync", "tabs": change})
        snapshot(rec)


def _act(rec: dict, argv: list[str]) -> dict | None:
    payload = {"command": argv[0], "target": argv[1] if len(argv) > 1 else None}
    return _operation(rec, "browser_action", payload, lambda: _dispatch(rec, argv))


def _dispatch(rec: dict, argv: list[str]) -> dict | None:
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
        _operation(rec, "browser_tab", {"command": "new"}, lambda: agent.call(session_name(), ["tab", "new", "about:blank"]))
        _observe_tabs(rec)
        tab = rec["tab_map"][rec["active_tab"]]
        if url != "about:blank":
            _operation(rec, "browser_open", {"url": url}, lambda: _open(url, float(config.get("page_load_seconds", 90))))
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
        _operation(rec, "browser_tab", {"command": "close" if closing else "switch", "tab": tab},
                   lambda: agent.call(session_name(), ["tab", *(["close"] if closing else []), live[tab]]))
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
    if rec.get("context"):
        with AttachedPage(session_name(), rec) as attached:
            tree, rec["refs"] = attached.snapshot(interactive)
        return tree
    data = agent.call(session_name(), ["snapshot", *(["-i"] if interactive else [])]) or {}
    rec["refs"] = data.get("refs", {})
    return data.get("snapshot", "")


def _describe(spec: dict, rec: dict | None = None) -> dict | None:
    if rec and rec.get("context"):
        with AttachedPage(session_name(), rec) as attached:
            return attached.target(spec)
    script = "JSON.stringify((" + DOM_JS + ")().target(" + json.dumps(spec, ensure_ascii=False) + "))"
    result = (agent.call(session_name(), ["eval", "--stdin"], stdin_text=script) or {}).get("result")
    return json.loads(result) if isinstance(result, str) and result != "null" else None


def _spec(rec: dict, token: str, rich: bool = False) -> dict:
    if token.startswith("@"):
        ref = rec["refs"].get(token[1:])
        if not ref:
            raise RecordError(f"引用 {token} 已失效；先运行 autoskill browser snapshot 再操作")
        if "locators" in ref:
            if ref.get("context", []) != rec.get("context", []):
                raise RecordError(f"引用 {token} 来自其他定位上下文；请重新运行 browser snapshot")
            return {"locator": ref["locators"][0]}
        box = agent.call(session_name(), ["get", "box", token], check=False)
        return {"ref": {"role": ref.get("role"), "name": ref.get("name"), "box": box}}
    if rich:
        kind, _, value = token.partition("=")
        if kind in ("testid", "text", "label", "placeholder", "xpath", "css") and value:
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
    spec = _spec(rec, token, rich)
    found = _describe(spec, rec)
    if not found or not found.get("locators"):
        hint = "先运行 autoskill browser snapshot --all 找到它的 @编号，或改用 CSS / xpath= 定位"
        if token.startswith("text="):
            hint = "text= 要求与元素的完整文字完全一致（不是包含）；" + hint
        elif token.startswith("testid="):
            hint = "页面上没有 data-testid 为这个值的元素；" + hint
        raise RecordError(f"页面上找不到 {token}；{hint}")
    explicit = spec.get("locator") if not token.startswith("@") else None
    if explicit and explicit["by"] == "xpath":
        found["locators"] = [explicit, *[loc for loc in found["locators"] if loc != explicit]]
    return found


def _secret_value(rec: dict, text: str, is_secret_field: bool) -> tuple[str, str]:
    """Return (text to type, text to record). Secrets are recorded as ${ENV:NAME} only."""
    if secrets.references(text):
        rec["secrets"] = sorted({*rec["secrets"], *secrets.references(text)})
        return secrets.resolve(ir.expand(text)), text
    if not is_secret_field:
        return ir.expand(text), text  # {{RAND}} is typed as this recording's value and recorded as written
    base = rec["project_id"].upper().replace("-", "_") + "_PASSWORD"
    if not re.match(r"[A-Za-z_]", base):
        base = "P_" + base
    name, n = base, 2
    while secrets.get_secret(name) not in (None, text):
        name, n = f"{base}_{n}", n + 1
    secrets.set_secret(name, text)
    rec["secrets"] = sorted({*rec["secrets"], name})
    return text, "${ENV:" + name + "}"


class PageProbe:
    """Reads the live page through agent-browser so assertions are judged by the replay rules."""

    def __init__(self, rec: dict | None = None, attached=None):
        self.rec, self.attached = rec or {}, attached
        self.context = copy.deepcopy(self.rec.get("context", []))

    def _read(self, locator: dict, what: str, arg: str | None = None):
        context = locator.get("context", self.context)
        if self.attached:
            return self.attached.read(locator, what, arg, context)
        if context:
            with AttachedPage(session_name(), self.rec) as attached:
                return attached.read(locator, what, arg, context)
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


def _element_png(locator: dict, rec: dict | None = None) -> bytes:
    """The pixels of an image or canvas as shown, without requesting it again (that would draw a new captcha)."""
    if rec and rec.get("context"):
        with AttachedPage(session_name(), rec) as attached:
            return attached.image(locator)
    script = ("(() => { const el = (" + DOM_JS + ")().find(" + json.dumps(locator, ensure_ascii=False) + ")[" + str(locator.get("nth", 0)) + "];"
              "const c = document.createElement('canvas'); c.width = el.naturalWidth || el.width; c.height = el.naturalHeight || el.height;"
              "c.getContext('2d').drawImage(el, 0, 0, c.width, c.height); return c.toDataURL('image/png'); })()")
    result = (agent.call(session_name(), ["eval", "--stdin"], stdin_text=script, check=False) or {}).get("result")
    if isinstance(result, str) and result.startswith("data:image/png;base64,"):
        return base64.b64decode(result.split(",", 1)[1])
    raise RecordError("读不到验证码图片的像素（目标不是图片或画布，或图片来自其他站点）；这一次请用户手动输入验证码")


def _launch_browser(config: dict, state_file: Path | None) -> None:
    if problem := paths.browser_display_problem(headed=paths.headed()):
        raise RecordError(problem)
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


def _reuse_stages(project_id: str, case_id: str, login_case: str | None, login: bool) -> tuple[list[dict], str | None]:
    """Freeze the selected active case and earlier pinned stages before opening a browser."""
    case = projects.case(project_id, case_id)
    extensions = case.get("extensions", {})
    if case["kind"] != "ui" or case["lifecycle"] != "active":
        raise RecordError("同页前置需要当前项目中有效的可复用页面用例")
    if (extensions.get("postconditions") or extensions.get("oracle", {}).get("source") == "observation"
            or extensions.get("functional_design", {}).get("state") == "unmapped"):
        raise RecordError("此用例需要独立工作流验证，不能仅执行其页面步骤来准备现场")
    dependencies = projects.depends_on(case)
    if any(dep != login_case for dep in dependencies):
        raise RecordError("此用例还有登录之外的用例依赖。请先使用原工作流执行这些依赖")
    if dependencies and not login:
        raise RecordError("此用例依赖项目登录，不能在 --no-login 下执行")
    automation = extensions.get("automation")
    ir.validate(automation)
    flow = {key: value for key, value in automation.items() if key != "setup_cases"}
    ir.validate_setup(flow)
    stages = [*automation.get("setup_cases", []), {"case_id": case_id, "revision": case["revision"],
              "content_hash": execution_inputs.content_hash(case), "automation": flow}]
    managed_login = login_case if dependencies and all(stage["case_id"] != login_case for stage in stages) else None
    return stages, managed_login


def _prepare_reuse(rec: dict, config: dict) -> dict:
    """Journal the setup and retain the recording window at the last verified page."""
    from ..replay import ui
    from ..replay.playwright_driver import PlaywrightDriver

    stages = rec["setup_cases"]
    receipts_path = recording_dir() / "setups" / uuid.uuid4().hex
    rec.update(setup_status="running", setup_evidence=data_refs.store(receipts_path))
    _save(rec)
    sources = [{key: item[key] for key in ("case_id", "revision", "content_hash")} for item in stages]
    intent = _intent(rec, "browser_setup", {"cases": sources})
    with AttachedPage(session_name(), rec) as attached:
        driver = PlaywrightDriver.attached(attached.page, config)
        receipts = ui.run_setup(driver, stages, config, receipts_path, "recording.png")
        if receipts[-1]["status"] == "Pass" and len(attached.page.context.pages) != 1:
            receipts[-1].update(status="Blocked", reason="前置改变了标签页数量，无法确认原页面的接续状态")
    execution_inputs.write_json(receipts_path / "receipt.json", receipts)
    rec["setup_results"] = [{key: item[key] for key in ("case_id", "revision", "content_hash", "status")} for item in receipts]
    last = receipts[-1]
    if last["status"] != "Pass":
        _receipt(intent, {"status": last["status"], "evidence": rec["setup_evidence"]}, status="failed")
        _file().unlink(missing_ok=True)
        raise RecordError(f"前置用例 {last['case_id']} 未通过：{last['reason'] or last['status']}。执行证据：{rec['setup_evidence']}")
    rec["setup_status"] = "verified"
    _save(rec)
    _receipt(intent, {"status": "Pass", "evidence": rec["setup_evidence"]})
    return {"url": PageProbe().url(), "slow": False, "seconds": 0}


def _setup_summary(rec: dict) -> dict:
    return ({"setup": {"status": rec["setup_status"], "cases": rec.get("setup_results", []), "evidence": rec["setup_evidence"]}}
            if rec.get("setup_cases") else {})


def _open_recording(rec: dict, config: dict, start_url: str, state_file: Path | None) -> dict:
    """Open the owned browser and begin capture after the reusable setup, when selected."""
    agent.call(session_name(), ["close"], check=False, timeout=30)
    network_capture = None
    try:
        _launch_browser(config, state_file)
        _fit_window()
        reuse = bool(rec.get("setup_cases"))
        page = _prepare_reuse(rec, config) if reuse else None
        network_capture = capture.start(session_name(), recording_dir())
        agent.call(session_name(), ["network", "har", "start"])
        if not reuse:
            page = _open(start_url, float(config.get("page_load_seconds", 90)))
    except Exception:
        capture.stop(network_capture)
        agent.call(session_name(), ["close"], check=False, timeout=30)
        if rec.get("setup_status") == "verified":
            rec["setup_status"] = "interrupted"
            _save(rec)
        raise
    finally:
        if state_file:
            state_file.unlink(missing_ok=True)
    rec["network_capture"] = network_capture
    if reuse:
        _save(rec)
    return page


def start(project_id: str, login: bool = True, as_login: bool = False, url: str | None = None, reuse: str | None = None) -> dict:
    """Open this task's browser on the project's address and record actions and traffic."""
    from .. import plan, workflow

    binding = workflow.current_binding()
    try:
        sheet = plan.show(recording=True)
        plan_at_start = {key: sheet[key] for key in ("task_id", "draft_task_id", "design_version", "excel", "project", "cases") if key in sheet}
    except (ValueError, OSError):
        plan_at_start = None
    notes = []
    previous = current(required=False)
    if previous:
        if previous.get("setup_status") in ("running", "verified", "interrupted"):
            raise RecordError("上次录制尚未就绪。请先用 record status 核对前置回执和现场，避免重复操作")
        if _page_state()["url"].startswith(WEB):
            raise RecordError("已有进行中的录制；运行 autoskill browser snapshot 接续当前浏览器；完成后再运行 record stop")
        previous["conversation_evidence"] = capture.stop(previous.get("network_capture"))
        retained = _retain_recording(previous)
        _file().unlink(missing_ok=True)
        notes.append(f"上次录制的浏览器已关闭；原 {len(previous['steps'])} 步和网络来源已保留：{retained}")
    info, config, auth = projects.get(project_id), projects.config(project_id), projects.auth(project_id)
    if reuse and (url or as_login):
        raise RecordError("使用 --reuse 时从已有用例开始，不能同时指定 --url 或 --as-login")
    login_case = auth.get("ui_case")
    stages, managed_login = _reuse_stages(project_id, reuse, login_case, login) if reuse else ([], login_case if login and not as_login else None)
    start_url = ir.absolute_url(stages[0]["automation"]["start_url"] if stages else url or info["start_url"], config["base_url"])
    ir.check_origin(start_url, config["allowed_origins"])
    used_login, note, state_file = False, "", None
    if managed_login:
        from .. import runner, sessions

        run = runner.create(project_id, [managed_login], "playwright", trigger="login")
        if runner.execute(run["id"])["status"] != "Pass":
            reason = (db.one("SELECT reason FROM results WHERE run_id = ? ORDER BY id DESC", (run["id"],)) or {}).get("reason", "")
            raise RecordError("自动登录没有成功，录制未开始：" + (reason or "请检查登录用例和凭据"))
        state_file = recording_dir() / "recording-state.json"
        state_file.write_text(json.dumps(sessions.load(project_id).get("ui", {})), encoding="utf-8")
        used_login, note = True, f"已用登录用例 {managed_login} 自动登录"
    rec = {"project_id": project_id, "session_id": task_context.session_id(), "started_at": db.now(), "start_url": relative(start_url, config["base_url"]),
           "steps": [], "refs": {}, "secrets": [], "used_login": used_login, "as_login": as_login, "setup_cases": stages,
           "rand": uuid.uuid4().hex[:6], "workflow_binding": binding, "plan_at_start": plan_at_start, "headed": paths.headed()}
    ir.set_rand(rec["rand"])
    page = _open_recording(rec, config, start_url, state_file)
    if stages:
        rec["setup_status"] = "ready"
        if any(stage["case_id"] == login_case for stage in stages):
            note = f"已在当前窗口执行登录用例 {login_case}"
    _observe_tabs(rec)
    tree = _settled_snapshot(rec)
    _save(rec)
    page = {**page, **_page_state()}  # the app may have moved on (for example to its sign-in page) while drawing
    result = {"recording": True, "project": project_id, "url": page["url"], "title": PageProbe().title(),
              "login": note or ("本次录制的就是登录流程" if as_login else "未登录状态"), "snapshot": tree,
              **_identity(rec), **_setup_summary(rec)}
    if page.get("slow"):
        notes.append(f"页面加载很慢，等了 {page['seconds']:.0f} 秒仍未完全加载，已继续；元素可能还没出齐，必要时再运行 autoskill browser snapshot")
    elif page["seconds"] >= 20:
        notes.append(f"页面加载用了 {page['seconds']:.0f} 秒")
    if not stages and relative(page["url"], config["base_url"]).rstrip("/") != rec["start_url"].rstrip("/"):
        result["redirected"] = f"打开的是 {start_url}，页面跳转到了 {page['url']}"
    if page["password"] and not as_login and not used_login:
        result["needs_login"] = ("页面上有密码输入框，看起来要先登录才能继续。保留当前浏览器和录制会话，"
                                 "用户可以在此窗口手动登录；完成后保持相同 --session 运行 autoskill browser snapshot，"
                                 "必要时用 browser wait 或 browser tab list 确认登录后的页面，再继续操作。"
                                 "如果需要自动登录用例，按 SKILL.md「登录」一节另行录制；账号密码不要猜。"
                                 "如果要测的就是这个登录页本身，可以直接继续")
    if notes:
        result["notes"] = notes
    return result


def _record(rec: dict, step: dict) -> dict:
    rec["steps"].append(step)
    _save(rec)
    return {"recorded": len(rec["steps"]), "step": ir.describe(step)}


def _identity(rec: dict) -> dict:
    """Return the recording's original task and browser mode without resolving a new default."""
    result = {"session_id": rec.get("session_id"), "browser_session": session_name(), "headed": rec.get("headed")}
    if rec.get("active_tab"):
        result["tab"] = rec["tab_map"][rec["active_tab"]]
    return result


def _after(rec: dict, result: dict) -> dict:
    """Report the active page and its stable recording tab id."""
    url = PageProbe().url()
    result.update(url=url, **_identity(rec))
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
            "start_url": rec["start_url"], "steps": [ir.describe(s) for s in rec["steps"]], "context": rec.get("context", []),
            **_identity(rec), **_setup_summary(rec)}


def cancel() -> dict:
    rec = current(required=False)
    handle = rec.get("network_capture") if rec else None
    try:
        evidence = capture.stop(handle)
    finally:
        agent.call(session_name(), ["close"], check=False, timeout=30)
    existed = _file().is_file()
    retained = _retain_recording({**rec, "conversation_evidence": evidence}) if rec and evidence.get("error") else None
    _file().unlink(missing_ok=True)
    if not retained:
        capture.discard(handle, recording_dir())
    return {"cancelled": existed, **({"retained_recording": retained, "notes": [evidence["error"]]} if retained else {})}


def _plain(text: str) -> str:
    """`secret:NAME` is a shell-friendly spelling of ${ENV:NAME}."""
    return "${ENV:" + text[7:] + "}" if text.startswith("secret:") and secrets.NAME.fullmatch(text[7:]) else text


def _rich_target(token: str) -> bool:
    return token.partition("=")[0] in ("xpath", "css", "testid", "text", "label", "placeholder", "role")


def _scoped_act(rec: dict, command: str, locator: dict, value=None) -> dict | None:
    """Dispatch once on the exact recorded target, without switching the daemon's implicit frame."""
    from playwright.sync_api import Error as PlaywrightError

    active = rec.get("active_tab")

    def dispatch():
        try:
            with AttachedPage(session_name(), rec) as attached:
                attached.act(locator, command, value)
        except PlaywrightError:
            change = _observe_tabs(rec)
            if not change or rec["tab_map"].get(active) not in change["closed"]:
                raise
            return change
        return None

    return _operation(rec, "browser_action", {"command": command, "locator": locator,
                                             "context": rec.get("context", [])}, dispatch)


def _context_command(rec: dict, command: str, rest: list[str]) -> dict:
    path = copy.deepcopy(rec.get("context", []))
    if command == "context":
        if len(rest) > 1 or (rest and rest[0] not in ("show", "parent", "top")):
            raise RecordError("用法：browser context show / parent / top")
        if rest == ["top"]:
            path = []
        elif rest == ["parent"]:
            path = path[:-1]
    else:
        if len(rest) != 1:
            raise RecordError(f"用法：browser {command} <目标>，例如 browser {command} '#container'")
        found = _target(rec, rest[0], rich=True)
        path.append({"kind": command, "locators": found["locators"]})
    trial = {**rec, "context": path}
    with AttachedPage(session_name(), trial) as attached:
        root = attached.root()
        root.dispose()
    rec["context"], rec["refs"] = path, {}
    tree = snapshot(rec)
    _save(rec)
    return {"context": path, "snapshot": tree,
            "note": "后续操作和断言使用此定位上下文；browser context top 返回主文档"}


def _scoped_read(rec: dict, command: str, rest: list[str]) -> dict:
    """Read-only browser commands must inspect the same scope as actions and assertions."""
    if command in ("get", "is"):
        if len(rest) < 2:
            raise RecordError(f"用法：browser {command} <属性> <目标>")
        what, target = rest[:2]
    else:
        if len(rest) != 1:
            raise RecordError(f"用法：browser {command} <目标>")
        what, target = command, rest[0]
    spec = _spec(rec, target, rich=True)
    with AttachedPage(session_name(), rec) as attached:
        locator = spec.get("locator")
        if locator is None:
            found = attached.target(spec)
            if not found:
                raise RecordError("当前定位上下文中找不到目标：" + target)
            locator = found["locators"][0]
        if command == "get" and what in ("text", "value", "count", "attribute", "attr"):
            attribute = rest[2] if len(rest) > 2 else None
            if what in ("attribute", "attr") and not attribute:
                raise RecordError("get attr 需要属性名")
            return {what: attached.read(locator, "attribute" if what == "attr" else what, attribute)}
        if command == "is" and what in ("visible", "checked"):
            return {what: bool(attached.read(locator, what))}
        if command == "wait":
            from ..replay import ui

            outcome = ui.check(PageProbe(rec, attached), {"assertion": "visible", "locators": [locator],
                                                        "context": rec.get("context", [])}, 20)
            if not outcome["passed"]:
                raise RecordError("等待当前上下文中的目标出现超时：" + target)
            return {"visible": True}
        node = attached.element(locator)
        try:
            if command == "get" and what == "html":
                return {"html": node.inner_html()}
            if command == "get" and what == "box":
                return {"box": node.bounding_box()}
            if command == "is" and what in ("enabled", "editable"):
                return {what: getattr(node, "is_" + what)()}
            if command == "scrollintoview":
                node.scroll_into_view_if_needed()
                return {"scrolled": True}
            raise RecordError("当前上下文不支持这个读取参数；请使用 get text/value/count/attr/html/box 或 is visible/checked/enabled/editable")
        finally:
            node.dispose()


def _targeted_step(rec: dict, command: str, rest: list[str], argv: list[str]) -> tuple[dict, dict | None]:
    """Describe and validate a target before sending its recorded action."""
    if not rest:
        raise RecordError(f"{command} 需要目标，例如 @e3")
    found = _target(rec, rest[0], rich=True)  # describe before acting: the element may be gone afterwards
    what = f'{found.get("role") or found.get("tag")}「{found.get("name", "")}」'
    if command in ("fill", "type") and not found.get("editable"):
        raise RecordError(f"{rest[0]} 是 {what}，不能输入文字；编号可能来自旧的 snapshot，请用最近一次返回的 snapshot 重新确认")
    if command == "select" and found.get("tag") != "select":
        raise RecordError(f"{rest[0]} 是 {what}，不是下拉框；自定义下拉请用 click 打开后再 click 选项")
    if command in ("check", "uncheck") and not found.get("checkable"):
        raise RecordError(f"{rest[0]} 是 {what}，不是复选框或单选框；请用最近一次返回的 snapshot 重新确认")
    step = {"action": command, "locators": found["locators"]}
    if rec.get("context"):
        step["context"] = copy.deepcopy(rec["context"])
    send = list(argv)
    if command in ("fill", "type", "select"):
        if len(rest) < 2:
            raise RecordError(f"{command} 需要内容")
        typed, step["value"] = _secret_value(rec, _plain(rest[1]), bool(found.get("secret")) and command != "select")
        send = [command, rest[0], typed]
    locator = found["locators"][0]
    early_change = (_scoped_act(rec, command, locator, send[2] if len(send) > 2 else None)
                    if rec.get("context") or _rich_target(rest[0]) or locator.get("unique") else _act(rec, send))
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
        _operation(rec, "browser_open", {"url": url}, lambda: _open(url, float(config.get("page_load_seconds", 90))))
        rec["context"] = []
        rec["refs"] = {}
        step = {"action": "goto", "url": relative(url, config["base_url"])}
    elif command == "reload":
        _operation(rec, "browser_reload", {}, lambda: agent.call(session_name(), ["reload"], timeout=90))
        _wait_ready(float(config.get("page_load_seconds", 90)))
        step = {"action": "reload"}
    elif command == "press":
        if not rest:
            raise RecordError("press 需要按键名，例如 Enter")
        script = "(" + DOM_JS + ")().focusHint()"
        try:
            observation = agent.call(session_name(), ["eval", "--stdin"], stdin_text=script, check=False, timeout=2)
        except agent.AgentError:  # An unavailable timing hint must not suppress the user's key.
            observation = None
        early_change = _act(rec, ["press", rest[0]])
        step = {"action": "press", "key": rest[0], "key_target": "active"}
        if observation and observation.get("result"):
            step["focus_hint"] = observation["result"]
    elif command == "captcha":
        if len(rest) < 2:
            raise RecordError("captcha 需要两个目标：验证码图片和输入框，例如 autoskill browser captcha @e4 @e5")
        from .. import captcha

        picture, field = _target(rec, rest[0], rich=True), _target(rec, rest[1], rich=True)
        recognized = captcha.solve(_element_png(picture["locators"][0], rec))
        early_change = (_scoped_act(rec, "fill", field["locators"][0], recognized)
                        if rec.get("context") or _rich_target(rest[1]) else _act(rec, ["fill", rest[1], recognized]))
        step = {"action": "captcha", "locators": field["locators"], "image": picture["locators"]}
        if rec.get("context"):
            step["context"] = copy.deepcopy(rec["context"])
    elif command in TARGETED:
        step, early_change = _targeted_step(rec, command, rest, argv)
    else:
        raise RecordError(f"录制不支持 {command}；可用：open snapshot describe tab frame shadow context click dblclick hover fill type select check uncheck press reload captcha dialog get is wait")
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
    if command in ("frame", "shadow", "context"):
        return _context_command(rec, command, rest)
    if command == "snapshot":
        tree = snapshot(rec, interactive="--all" not in rest)
        _save(rec)
        return _after(rec, {"snapshot": tree})
    if command == "describe":
        if not rest:
            raise RecordError("describe 需要目标，例如 @e5")
        found = _target(rec, rest[0], rich=True)
        return {"element": found.get("tag"), "role": found.get("role"), "name": found.get("name"),
                "locators": [ir.describe_locator(loc) for loc in found["locators"]],
                "locator_definitions": found["locators"], "context": rec.get("context", []),
                "use_as_target": [_as_target(loc.get("anchor", loc)) for loc in found["locators"] if not loc.get("xpath_anchor") and not loc.get("nth")]}
    if command in ("get", "is", "wait", "screenshot", "scroll", "scrollintoview", "dialog"):
        is_targeted = ((command in ("get", "is") and rest and rest[0] not in ("url", "title", "cdp-url"))
                       or command == "scrollintoview" or (command == "wait" and rest and not rest[0].isdigit()))
        if is_targeted and (rec.get("context") or any(_rich_target(token) for token in rest)):
            return _after(rec, {"result": _scoped_read(rec, command, rest)})
        result = screenshots.capture(session_name(), rest) if command == "screenshot" else agent.call(session_name(), argv)
        if command == "wait" and rest and rest[0].isdigit():
            result = {"waited_ms": int(rest[0])}
        return _after(rec, {"result": result})
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


def _assertion_locators(rec: dict, kind: str, target: str) -> list[dict]:
    specified = _spec(rec, target, rich=True).get("locator")
    explicit = specified if not target.startswith("@") else None
    if kind in ("count", "min_count") or (kind == "hidden" and specified):
        return [specified or {"by": "css", "value": target}]
    try:
        found = _target(rec, target, rich=True)["locators"]
    except RecordError:
        if not explicit:
            raise
        found = [explicit]  # a missing explicit target is an assertion failure, not a lost assertion
    if not (explicit and explicit["by"] == "text"):
        found = [loc for loc in found if loc["by"] != "text"] or found
    if explicit and explicit["by"] in ("css", "xpath") and all(loc != explicit for loc in found):
        found = [explicit, *found]
    return found


def _check_live(rec: dict, step: dict, timeout: float) -> dict:
    from ..replay import ui

    if rec.get("context"):
        with AttachedPage(session_name(), rec) as attached:
            return ui.check(PageProbe(rec, attached), step, timeout)
    return ui.check(PageProbe(), step, timeout)


def check(kind: str, target: str | None, expected: str | None, attribute: str | None = None, *,
          timeout_seconds: float | None = None, stable_for_seconds: float = 0, mode: str = "eventually",
          transition_from: dict | None = None) -> dict:
    """Persist the declared oracle and its observation even when the live business result fails."""
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
        step["locators"] = _assertion_locators(rec, kind, target)
        if rec.get("context"):
            step["context"] = copy.deepcopy(rec["context"])
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
    if timeout_seconds is not None:
        step["timeout_seconds"] = timeout_seconds
    if stable_for_seconds:
        step["stable_for_seconds"] = stable_for_seconds
    if mode != "eventually":
        step["mode"] = mode
    if transition_from is not None:
        step["transition_from"] = transition_from
    step["oracle"] = {"source": "explicit", "declared_at": db.now()}
    ir.validate({"start_url": "/", "actions": [], "assertions": [step]})
    timeout = float(projects.config(rec["project_id"]).get("timeout_seconds", 20))
    intent = _intent(rec, "check", {"check": step}, side_effect=False)
    outcome = _check_live(rec, step, timeout)
    step["recording_result"] = {**outcome, "observed_at": db.now()}
    recorded = _record(rec, step)
    _receipt(intent, {"check": step, **outcome, **recorded}, status="succeeded" if outcome["passed"] else "failed")
    return {**recorded, **outcome, "status": "Pass" if outcome["passed"] else "Fail",
            "note": "已保存期望和现场结果；回放仍使用声明的期望"}


def _plan_design(rec: dict, plan_case: str | None) -> str | None:
    """Attach the actual design row before compilation and its frozen task report are produced."""
    if not plan_case:
        return None
    from .. import plan, plan_execution

    rec.pop("test_case_document", None)
    try:
        sheet = rec["plan_at_start"] if "plan_at_start" in rec else plan.show(recording=True)
        if not sheet:
            raise ValueError("开始录制时还没有设计表，未将后来创建的表绑定到这次录制")
        if sheet.get("project") and sheet["project"] != rec["project_id"]:
            raise ValueError("用例设计表属于其他项目，未将其功能描述套用到本次录制")
        row = next((item for item in sheet["cases"] if item["id"] == plan_case), None)
        if row is None:
            raise ValueError("设计表中未找到所选用例，录制仍保存，功能设计待补充")
        document = {key: row[key] for key in ("preconditions", "steps", "expected", "module", "priority", "author", "remarks", "prefix", "requirement_ids") if row.get(key)}
        document["description"] = row.get("description") or row["title"]
        if row.get("business_path") or row.get("biz_path"):
            document["business_path"] = row.get("business_path") or row["biz_path"]
        elif row.get("module"):
            document["business_path"] = [projects.meta(rec["project_id"])["name"], row["module"]]
        document["source"] = {"kind": "case_plan", "file": sheet["excel"], "case_id": plan_case,
                              "task_id": sheet.get("task_id") or sheet.get("draft_task_id"), "business_row_hash": plan._source_hash(row),
                              "design_hash": plan_execution._design_hash(row),
                              **({"design_version": sheet["design_version"]} if "design_version" in sheet else {}),
                              "hash_scope": "功能字段、业务层级和来源引用；不含 design_sha256 自身",
                              **({"upstream": row["source"]} if row.get("source") else {})}
        document["source"]["design_sha256"] = execution_inputs.content_hash(document)
        rec["test_case_document"] = document
        return None
    except (ValueError, OSError) as error:
        return "功能设计未能关联：" + str(error)


def stop(title: str, module: str = "", category: str = "", plan_case: str | None = None) -> dict:
    """Retain recording candidates, then validate them through the existing execution queue."""
    from .. import candidate_publication, conversation_capture, plan, runner, task_reports
    from . import compile as compiler

    rec = current()
    if not (title or "").strip():
        raise RecordError("请用 --title 给这条用例起一个说明业务含义的名字")
    if not rec["steps"]:
        raise RecordError("还没有记录任何步骤；先用 autoskill browser 操作页面，或运行 autoskill record cancel 放弃")
    design_note = _plan_design(rec, plan_case)
    har_file, state_file = recording_dir() / "recording.har", recording_dir() / "recording-state.json"
    har, state, captures = {"log": {"entries": []}}, None, None
    try:
        rec["conversation_evidence"] = capture.stop(rec.get("network_capture"))
        _save(rec)
        agent.call(session_name(), ["network", "har", "stop", str(har_file)], check=False)
        if har_file.is_file():
            har = json.loads(har_file.read_text(encoding="utf-8", errors="replace"))
        if agent.call(session_name(), ["state", "save", str(state_file)], check=False) is not None and state_file.is_file():
            state = json.loads(state_file.read_text(encoding="utf-8"))
        captures = conversation_capture.capture(rec["project_id"], rec, har, source_har=har_file.read_bytes() if har_file.is_file() else None)
    finally:
        agent.call(session_name(), ["close"], check=False, timeout=30)
        if captures is not None:
            har_file.unlink(missing_ok=True)  # remove the temporary HAR only after the durable source evidence exists
        state_file.unlink(missing_ok=True)
    with project_lock(rec["project_id"]):
        replay_source = conversation_capture.compiler_source(har, rec.get("conversation_evidence"))
        summary = compiler.build(rec["project_id"], rec, replay_source, state, title.strip(), module, category)
        candidate_publication.stage(rec["project_id"], summary["candidates"], summary["candidate_auth"],
                                    summary["candidate_source"], candidate_id=summary["candidate_id"])
        summary["test_report"] = task_reports.for_recording(rec["project_id"], rec, summary["candidates"])
        if plan_case and not design_note:
            summary["plan"] = plan.bind_execution(plan_case, rec["project_id"], summary["candidates"][0], None,
                                                  folder=runner.plan_folder(rec["test_case_document"]["source"]["file"]).parent,
                                                  candidate_id=summary["candidate_id"])
        elif design_note:
            summary["plan"] = {"case": plan_case, "error": design_note}
    if rec.get("conversation_evidence", {}).get("error"):
        retained = _retain_recording(rec)
        summary["network_capture"] = {"available": rec["conversation_evidence"].get("available", False),
                                      "error": rec["conversation_evidence"]["error"], "retained_recording": retained}
        summary.setdefault("notes", []).append(rec["conversation_evidence"]["error"] + "；原录制保留在 " + retained)
    else:
        capture.discard(rec.get("network_capture"), recording_dir())
    _file().unlink(missing_ok=True)
    summary["project"] = rec["project_id"]
    summary["conversation_captures"] = captures
    if design_note:
        summary.setdefault("notes", []).append(design_note)
    if rec.get("workflow_binding"):
        summary["next"] = "候选已保留，请将 candidates 提交到当前工作流的 materialize；本次未额外启动执行"
    else:
        summary.update(candidate_publication.queue(summary["candidate_id"], headed=rec.get("headed", paths.headed())))
    return summary
