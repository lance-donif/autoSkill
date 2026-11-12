"""Small native-command adapters used by the recorder.

The native CLI resolves an *open* dialog.  Recording a next-dialog policy needs
a listener before the triggering command, otherwise a confirm can block that
command forever.  A short-lived CDP attachment provides that listener without
owning or closing the browser created by agent-browser.
"""
from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable

from . import agent
from .. import secrets
from ..replay import ir

_worker = threading.local()


def _page(browser, rec):
    pages = [page for context in browser.contexts for page in context.pages if not page.is_closed()]
    if not pages:
        raise agent.AgentError("录制浏览器没有可操作的页面")
    active_url = next((item.get("url") for item in rec.get("tab_info", []) if item.get("active")), None)
    candidates = [page for page in pages if page.url == active_url] or pages
    for page in candidates:
        if page.evaluate("document.hasFocus()"):
            return page
    return candidates[-1]


def _scope(page, rec):
    frame = page.main_frame
    for selector in rec.get("frame_path", []):
        element = frame.query_selector(selector)
        frame = element.content_frame() if element else None
        if frame is None:
            raise agent.AgentError("当前 iframe 已不存在；先 browser frame main 再重新选择")
    return frame


def evaluate(session: str, script: str):
    """Evaluate in the recorder's selected frame (native eval uses main only)."""
    control = getattr(_worker, "control", None)
    if control:
        return {"result": control(lambda browser, rec: _scope(_page(browser, rec), rec).evaluate(script))}
    return agent.call(session, ["eval", "--stdin"], stdin_text=script)


def frame_target(rec: dict, selector: str) -> str:
    """Validate a relative frame selection without native's main-only lookup."""
    control = getattr(_worker, "control", None)
    if not control:
        return selector

    def destination(browser, rec):
        page = _page(browser, rec)
        element = _scope(page, rec).query_selector(selector)
        frame = element.content_frame() if element else None
        if frame is None:
            raise agent.AgentError("目标不是可用的 iframe")
        return selector

    return control(destination)


def grant_permission(permission: str) -> None:
    control = getattr(_worker, "control", None)
    if control:
        def grant(browser, rec):
            rec["native_permissions"] = sorted({*rec.get("native_permissions", []), permission})
            for context in browser.contexts:
                context.grant_permissions(rec["native_permissions"])
        control(grant)


def frame_snapshot(rec: dict, dom_js: str, interactive: bool = True):
    control = getattr(_worker, "control", None)
    if control is None:
        return None
    script = """() => {
      const helper = (DOM_SOURCE)();
      const refs = {}, lines = [document.title || 'Iframe'];
      const escape = s => CSS.escape(s);
      const path = element => {
        const parts=[];
        for(let node=element;node && node.nodeType===1 && node!==document.body;node=node.parentElement){
          if(node.id){parts.unshift('#'+escape(node.id));break;}
          let part=node.tagName.toLowerCase();
          const siblings=node.parentElement ? [...node.parentElement.children].filter(item=>item.tagName===node.tagName):[];
          if(siblings.length>1)part+=':nth-of-type('+(siblings.indexOf(node)+1)+')';
          parts.unshift(part);
        }
        return parts.join(' > ');
      };
      let index=0;
      for(const node of document.querySelectorAll('body *')){
        if(/^(SCRIPT|STYLE|NOSCRIPT)$/.test(node.tagName) || !node.getClientRects().length)continue;
        if(INTERACTIVE && !node.matches('input,textarea,select,button,a[href],[contenteditable],iframe,frame,[role],h1,h2,h3'))continue;
        const selector=path(node);if(!selector)continue;
        const target=helper.target({locator:{by:'css',value:selector}});if(!target)continue;
        const ref='e'+(++index);
        refs[ref]={selector,role:target.role,name:target.name};
        lines.push('- '+(target.role||target.tag)+' '+JSON.stringify(target.name||'')+' [ref='+ref+']');
      }
      return {snapshot:lines.join('\\n'),refs};
    }""".replace("DOM_SOURCE", dom_js).replace("INTERACTIVE", "true" if interactive else "false")
    return control(lambda browser, rec: _scope(_page(browser, rec), rec).evaluate(script))


def target_action(rec: dict, step: dict) -> bool:
    """Native selector actions use main-only iframe lookup in 0.42."""
    control = getattr(_worker, "control", None)
    if control is None or not rec.get("frame_path"):
        return False

    def execute(browser, rec):
        page = _page(browser, rec)
        scope = _scope(page, rec)

        def locate(locators):
            locator = next((loc for loc in locators if loc["by"] == "css"), None)
            if locator is None:
                raise agent.AgentError("框架内操作缺少 CSS 备选定位")
            return scope.locator(locator["value"]).nth(locator.get("nth", 0))

        target = locate(step["locators"])
        op = step["action"]
        if op in ("fill", "type", "select"):
            if op == "fill":
                target.fill(step["value"])
            elif op == "type":
                target.press_sequentially(step["value"])
            else:
                try:
                    target.select_option(value=step["value"], timeout=1000)
                except Exception:
                    target.select_option(label=step["value"])
        elif op in ("check", "uncheck"):
            target.set_checked(op == "check")
        elif op == "scroll_into_view":
            target.scroll_into_view_if_needed()
        elif op == "drag":
            target.drag_to(locate(step["target_locators"]))
        elif op == "upload":
            target.set_input_files(step["files"])
        elif op == "download":
            with page.expect_download() as pending:
                target.click()
            pending.value.save_as(step["output"])
        elif op in ("click", "dblclick"):
            held = [key for key in rec.get("held_keys", []) if key in ("Alt", "Control", "Meta", "Shift")]
            getattr(target, op)(modifiers=held)
        else:
            getattr(target, op)()

    control(execute)
    return True


def input_action(rec: dict, step: dict) -> bool:
    """Dispatch real CDP input with state retained across CLI invocations."""
    control = getattr(_worker, "control", None)
    if control is None:
        return False

    def execute(browser, rec):
        page = _page(browser, rec)
        channel = page.context.new_cdp_session(page)
        held = list(rec.get("held_keys", []))
        aliases = {"Ctrl": "Control", "Cmd": "Meta", "Command": "Meta", "ControlOrMeta": "Control", "Esc": "Escape"}
        modifier_bits = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

        def modifiers(keys):
            return sum(bit for name, bit in modifier_bits.items() if name in keys)

        def key_event(value, kind, keys):
            key = aliases.get(value, value)
            codes = {"Enter": ("Enter", 13), "Tab": ("Tab", 9), "Escape": ("Escape", 27), "Backspace": ("Backspace", 8),
                     "Delete": ("Delete", 46), "ArrowLeft": ("ArrowLeft", 37), "ArrowUp": ("ArrowUp", 38),
                     "ArrowRight": ("ArrowRight", 39), "ArrowDown": ("ArrowDown", 40), "Home": ("Home", 36),
                     "End": ("End", 35), "PageUp": ("PageUp", 33), "PageDown": ("PageDown", 34),
                     "Shift": ("ShiftLeft", 16), "Control": ("ControlLeft", 17), "Alt": ("AltLeft", 18), "Meta": ("MetaLeft", 91),
                     "Space": ("Space", 32)}
            code, virtual = codes.get(key, ("Key" + key.upper(), ord(key.upper()) if len(key) == 1 and key.isascii() else 0))
            if key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
                code, virtual = key, 111 + int(key[1:])
            if len(key) == 1:
                if key.isdigit():
                    code = "Digit" + key
                if "Shift" in keys and key.isalpha():
                    key = key.upper()
            params = {"type": kind, "key": " " if key == "Space" else key, "code": code,
                      "windowsVirtualKeyCode": virtual, "modifiers": modifiers(keys)}
            if kind == "keyDown" and not any(name in keys for name in ("Control", "Meta", "Alt")):
                if len(key) == 1 or key == "Space":
                    params["text"] = " " if key == "Space" else key
                elif key == "Enter":
                    params["text"] = "\r"
            channel.send("Input.dispatchKeyEvent", params)

        try:
            if step["action"] in ("keyboard", "press"):
                operation = step.get("operation", "press")
                value = step.get("value", step.get("key", ""))
                if operation == "inserttext":
                    channel.send("Input.insertText", {"text": value})
                elif operation == "type":
                    for char in value:
                        key_event(char, "keyDown", held)
                        key_event(char, "keyUp", held)
                elif operation in ("down", "up"):
                    key = aliases.get(value, value)
                    if operation == "down" and key not in held:
                        held.append(key)
                    if operation == "up" and key in held:
                        held.remove(key)
                    key_event(key, "keyDown" if operation == "down" else "keyUp", held)
                    rec["held_keys"] = held
                else:
                    parts = [aliases.get(part, part) for part in value.split("+")]
                    # A literal '+' is also a valid key.
                    if value == "+":
                        parts = ["+"]
                    temporary = [key for key in parts[:-1] if key not in held]
                    active = [*held, *temporary]
                    for key in temporary:
                        key_event(key, "keyDown", active)
                    key_event(parts[-1], "keyDown", active)
                    key_event(parts[-1], "keyUp", active)
                    for key in reversed(temporary):
                        active.remove(key)
                        key_event(key, "keyUp", active)
            else:
                operation = step["operation"]
                x, y = rec.get("mouse_position", [0, 0])
                buttons = list(rec.get("mouse_buttons", []))
                button = step.get("button", "left")
                if operation == "move":
                    x, y = step["x"], step["y"]
                elif operation == "down" and button not in buttons:
                    buttons.append(button)
                elif operation == "up" and button in buttons:
                    buttons.remove(button)
                params = {"type": {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased", "wheel": "mouseWheel"}[operation],
                          "x": x, "y": y, "modifiers": modifiers(held),
                          "buttons": sum({"left": 1, "right": 2, "middle": 4}[name] for name in buttons)}
                if operation in ("down", "up"):
                    params.update(button=button, clickCount=1)
                if operation == "wheel":
                    params.update(deltaX=step.get("delta_x", 0), deltaY=step.get("delta_y", 100))
                channel.send("Input.dispatchMouseEvent", params)
                rec.update(mouse_position=[x, y], mouse_buttons=buttons)
        finally:
            channel.detach()

    control(execute)
    return True


def number(value: str, label: str, *, minimum: float | None = None) -> int | float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 必须是数字") from None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{label} 必须是有限数字" + (f"，且不小于 {minimum:g}" if minimum is not None else ""))
    return int(result) if result.is_integer() else result


def expression(script: str) -> str:
    """Native eval and Playwright evaluate agree on expression/function input."""
    script = script.rstrip().removesuffix(";")
    return "(async () => { const value = (\n" + script + "\n); return typeof value === 'function' ? await value() : await value; })()"


def _endpoint(value) -> str:
    if isinstance(value, str) and value.startswith(("ws://", "wss://", "http://", "https://")):
        return value
    if isinstance(value, dict):
        for item in value.values():
            found = _endpoint(item)
            if found:
                return found
    return ""


def _restore_native_settings(session: str, endpoint: str, rec: dict) -> None:
    """Restore daemon-owned overrides after the temporary CDP client detaches."""
    from . import native

    for setting in rec.get("browser_settings", {}).values():
        args = [ir.expand(item) for item in secrets.resolve(setting["args"])]
        agent.call(session, [setting["command"], *args])
    native.ensure_permissions(session, endpoint, rec)


def with_dialogs(session: str, rec: dict, operation: Callable[[], object]):
    """Pump real Chrome dialog events while a native command runs.

    A failed CDP attachment fails before dispatch.  Native command errors remain
    command errors, and disconnecting this attachment never closes Chrome.
    """
    from playwright.sync_api import sync_playwright

    endpoint = _endpoint(agent.call(session, ["get", "cdp-url"]))
    if not endpoint:
        raise agent.AgentError("无法取得录制浏览器的 CDP 地址；操作尚未执行")
    outcome: list[object] = []
    failures: list[BaseException] = []
    finished = threading.Event()
    requests = queue.Queue()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000, no_defaults=True)
            channel = browser.new_browser_cdp_session()
        except Exception as error:
            raise agent.AgentError("无法准备录制弹窗处理；操作尚未执行：" + str(error)[:200]) from None

        def handle(dialog) -> None:
            policy = rec.pop("dialog_policy", None) or {"mode": "accept"}
            try:
                if policy["mode"] == "dismiss":
                    dialog.dismiss()
                else:
                    dialog.accept(ir.expand(secrets.resolve(policy.get("text", ""))))
            except Exception as error:
                failures.append(error)

        for context in browser.contexts:
            context.on("dialog", handle)
            if rec.get("native_permissions"):
                context.grant_permissions(rec["native_permissions"])
            if rec.get("browser_settings"):
                from ..replay.primitives import PlaywrightCommands

                commands = PlaywrightCommands(context)
                for setting in rec["browser_settings"].values():
                    args = [ir.expand(item) for item in secrets.resolve(setting["args"])]
                    commands.execute(setting["command"], args, _page(browser, rec))

        def execute() -> None:
            def control(callback):
                task = {"callback": callback, "done": threading.Event()}
                requests.put(task)
                task["done"].wait()
                if "error" in task:
                    raise task["error"]
                return task.get("result")

            _worker.control = control
            try:
                outcome.append(operation())
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=execute, name="autoskill-native-action", daemon=True)
        worker.start()
        # Browser-level CDP stays usable when the action closes its own tab.
        # Each round-trip dispatches Playwright's pending dialog callbacks.
        while not finished.is_set():
            try:
                task = requests.get_nowait()
            except queue.Empty:
                task = None
            if task is not None:
                try:
                    task["result"] = task["callback"](browser, rec)
                except BaseException as error:
                    task["error"] = error
                finally:
                    task["done"].set()
            try:
                channel.send("Browser.getVersion")
            except Exception as error:
                # Do not leave an operation running after reporting completion.
                # agent.call has its own finite subprocess timeout.
                failures.append(error)
                finished.wait(0.05)
            time.sleep(0.02)
        worker.join()
        try:
            channel.detach()
        except Exception:
            pass
    try:
        _restore_native_settings(session, endpoint, rec)
    except Exception as error:
        failures.append(error)
    if failures:
        raise failures[0]
    return outcome[0] if outcome else None
