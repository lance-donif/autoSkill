"""Selenium driver for the shared UI replay loop.

Chrome and chromedriver are always given by explicit path, so Selenium Manager
never runs and nothing is downloaded. Site traffic uses the browser's normal network.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from .. import media, paths
from . import ir
from .primitives import (DialogPolicy, SeleniumCommands, SeleniumDialogs, eval_expression,
                         output_file, project_asset, scroll_delta)
from .tabs import selenium_tabs

DOM_JS = (Path(__file__).with_name("dom.js")).read_text(encoding="utf-8")
FIND = "return (" + DOM_JS + ")().find(arguments[0]);"
KEYS = {"Enter": "ENTER", "Escape": "ESCAPE", "Tab": "TAB", "Backspace": "BACKSPACE", "Delete": "DELETE",
        "Space": "SPACE", "ArrowUp": "ARROW_UP", "ArrowDown": "ARROW_DOWN", "ArrowLeft": "ARROW_LEFT",
        "ArrowRight": "ARROW_RIGHT", "Home": "HOME", "End": "END", "PageUp": "PAGE_UP", "PageDown": "PAGE_DOWN",
        "Control": "CONTROL", "ControlOrMeta": "CONTROL", "Ctrl": "CONTROL", "Shift": "SHIFT", "Alt": "ALT",
        "Meta": "META", "Cmd": "META", "Command": "META", "Esc": "ESCAPE", "Insert": "INSERT",
        **{f"F{number}": f"F{number}" for number in range(1, 13)}}


def _bypass_proxy_for_loopback() -> None:
    """chromedriver is reached over loopback; that connection must never go through HTTP(S)_PROXY."""
    import os

    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    entries = [e.strip() for e in current.split(",") if e.strip()]
    for host in ("localhost", "127.0.0.1"):
        if host not in entries:
            entries.append(host)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(entries)


class SeleniumDriver:
    name = "selenium"

    def __init__(self, config: dict, headless: bool = True, asset_root=None, output_root=None):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        chrome, chromedriver = paths.chrome(bundled=True), paths.chromedriver()  # the driver must match the browser's version
        if not chrome or not chromedriver:
            raise ValueError("Selenium 回放需要随包的 Chrome 与 chromedriver；未找到，且不会联网下载")
        self.config = config
        self.asset_root = asset_root if asset_root is not None else config.get("_asset_root")
        self.output_root = output_root if output_root is not None else config.get("_output_root")
        self.downloads = []
        self.allowed = list(config.get("allowed_origins", []))
        options = webdriver.ChromeOptions()
        options.binary_location = chrome
        options.page_load_strategy = "eager"
        options.add_experimental_option("prefs", {"profile.default_content_setting_values.media_stream_mic":
                                                 1 if "microphone" in media.permissions(config) else 2})
        _bypass_proxy_for_loopback()
        # The independent CDP event channel answers real dialogs, including while
        # a WebDriver action is waiting for one. Never let WebDriver override an
        # explicitly prepared dismiss or prompt value.
        options.set_capability("unhandledPromptBehavior", "ignore")
        for arg in [*paths.QUIET_CHROME_ARGS, *media.chrome_args(config, self.asset_root),
                    "--window-size=1920,1080" if headless else "--start-maximized"]:
            options.add_argument(arg)
        if headless:
            options.add_argument("--headless=new")
        if config.get("ignore_https_errors"):
            options.add_argument("--ignore-certificate-errors")
        self.wd = webdriver.Chrome(options=options, service=Service(executable_path=chromedriver, env=paths.child_env()))
        self.wd.set_page_load_timeout(float(config.get("page_load_seconds", 90)))
        self.wd.set_script_timeout(float(config.get("timeout_seconds", 20)))
        self._seeds: dict[str, str] = {}
        self._origins = set(self.allowed)
        self._frame_path = []
        self._mouse_state = {"x": 0, "y": 0, "buttons": set()}
        self._held_modifiers = set()
        self.events = None

    # --- lifecycle ---------------------------------------------------
    def reset(self, state: dict | None = None) -> None:
        """Clean cookies and storage, then optionally seed a saved login state."""
        if self.events:
            self.events.close()
        self.tabs = selenium_tabs(self.wd)
        self.wd.switch_to.default_content()
        self._frame_path = []
        cdp = self.wd.execute_cdp_cmd
        # Selenium reuses its browser process. Undo settings from the previous
        # case before navigating; otherwise offline/headers leak into later cases.
        cdp("Network.enable", {})
        cdp("Network.setExtraHTTPHeaders", {"headers": {}})
        cdp("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
        cdp("Emulation.clearDeviceMetricsOverride", {})
        cdp("Emulation.clearGeolocationOverride", {})
        cdp("Emulation.setEmulatedMedia", {"features": []})
        cdp("Browser.resetPermissions", {})
        self.dialogs = DialogPolicy()
        self.events = SeleniumDialogs(self.wd, self.dialogs)
        if "microphone" in media.permissions(self.config):
            self.events.grant_permissions(["audioCapture"])
        self.commands = SeleniumCommands(self.wd, self.events)
        self.wd.get("about:blank")
        for identifier in self._seeds.values():
            cdp("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})
        self._seeds = {}
        cdp("Network.clearBrowserCookies", {})
        for origin in self._origins:
            cdp("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
        for cookie in (state or {}).get("cookies", []):
            item = {k: cookie[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly") if k in cookie}
            if cookie.get("sameSite") in ("Strict", "Lax", "None"):
                item["sameSite"] = cookie["sameSite"]
            if cookie.get("expires", -1) and cookie.get("expires", -1) > 0:
                item["expires"] = cookie["expires"]
            cdp("Network.setCookie", item)
        import json

        for entry in (state or {}).get("origins", []):
            pairs = {i["name"]: i["value"] for i in entry.get("localStorage", [])}
            if pairs:
                source = (f"if (location.origin === {json.dumps(entry['origin'])}) "
                          f"for (const [k, v] of Object.entries({json.dumps(pairs)})) localStorage.setItem(k, v);")
                added = cdp("Page.addScriptToEvaluateOnNewDocument", {"source": source})
                self._seeds[entry["origin"]] = added["identifier"]
        self.downloads = []
        self._mouse_state = {"x": 0, "y": 0, "buttons": set()}
        from selenium.webdriver.common.action_chains import ActionChains

        ActionChains(self.wd).reset_actions()
        self._held_modifiers = set()

    def storage_state(self) -> dict:
        from selenium.common.exceptions import NoSuchFrameException, NoSuchWindowException, StaleElementReferenceException
        from selenium.webdriver.common.by import By

        cookies = []
        for c in self.wd.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]:
            cookies.append({"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"],
                            "expires": c.get("expires", -1), "httpOnly": c.get("httpOnly", False),
                            "secure": c.get("secure", False), "sameSite": c.get("sameSite", "Lax")})
        selected_tab = self.wd.current_window_handle
        selected_frames = list(self._frame_path)
        origins = {}

        def collect():
            entry = self.wd.execute_script("try { return {origin:location.origin,localStorage:"
                                           "Object.entries(localStorage).map(([name,value])=>({name,value}))}; } catch { return null; }")
            if entry and entry["origin"].startswith(("http://", "https://")):
                origins[entry["origin"]] = entry
                self._origins.add(entry["origin"])
            for frame in self.wd.find_elements(By.CSS_SELECTOR, "iframe,frame"):
                switched = False
                try:
                    self.wd.switch_to.frame(frame)
                    switched = True
                    collect()
                except (NoSuchFrameException, StaleElementReferenceException):
                    continue  # a detached frame has no remaining storage to inspect
                finally:
                    if switched:
                        self.wd.switch_to.parent_frame()

        try:
            for handle in self.wd.window_handles:
                try:
                    self.wd.switch_to.window(handle)
                    self.wd.switch_to.default_content()
                    collect()
                except NoSuchWindowException:
                    continue
        finally:
            if selected_tab not in self.wd.window_handles:
                raise ValueError("保存登录状态时当前标签页已关闭")
            self.wd.switch_to.window(selected_tab)
            self.wd.switch_to.default_content()
            self._frame_path = []
            for selector in selected_frames:
                nodes = self.wd.find_elements(By.CSS_SELECTOR, selector)
                if len(nodes) != 1:
                    raise ValueError("保存登录状态时当前框架已变化")
                self.wd.switch_to.frame(nodes[0])
                self._frame_path.append(selector)
        return {"cookies": cookies, "origins": list(origins.values())}

    def close(self) -> None:
        try:
            if self.events:
                self.events.close()
        finally:
            self.wd.quit()

    # --- page --------------------------------------------------------
    def tab_action(self, step: dict, timeout: float) -> None:
        self.tabs.action(step, timeout)
        self.wd.switch_to.default_content()
        self._frame_path = []

    def sync_tabs(self, change: dict, timeout: float) -> None:
        self.tabs.sync(change, timeout)
        self.wd.switch_to.default_content()
        self._frame_path = []

    def goto(self, url: str) -> None:
        self.wd.switch_to.default_content()
        self._frame_path = []
        if url.startswith(("http://", "https://")):
            self._origins.add(ir.origin_of(url))
        self.wd.get(url)
        seeded = self._seeds.pop(ir.origin_of(url), None)
        if seeded:  # seed storage once, never overwrite what the app writes later
            self.wd.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", {"identifier": seeded})

    def reload(self) -> None:
        self.wd.switch_to.default_content()
        self._frame_path = []
        self.wd.refresh()

    def back(self) -> None:
        self.wd.switch_to.default_content()
        self._frame_path = []
        self.wd.back()

    def forward(self) -> None:
        self.wd.switch_to.default_content()
        self._frame_path = []
        self.wd.forward()

    def url(self) -> str:
        url = self.wd.current_url
        if url.startswith(("http://", "https://")):
            self._origins.add(ir.origin_of(url))
        return url

    def title(self) -> str:
        return self.wd.title

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)

    def screenshot(self, path: Path) -> None:
        from selenium.webdriver.common.by import By

        masks = ["input[type=password]", *self.config.get("screenshot_masks", [])]
        css = ",".join(masks) + "{background:#ff00ff!important;color:transparent!important;-webkit-text-fill-color:transparent!important}"
        selected = list(self._frame_path)

        def visit(install):
            if install:
                self.wd.execute_script("const s=document.createElement('style');s.id='__autoskill_mask';s.textContent=arguments[0];"
                                       "(document.head||document.documentElement).append(s);", css)
            else:
                self.wd.execute_script("const s=document.getElementById('__autoskill_mask');if(s)s.remove();")
            for frame in self.wd.find_elements(By.CSS_SELECTOR, "iframe,frame"):
                switched = False
                try:
                    self.wd.switch_to.frame(frame)
                    switched = True
                    visit(install)
                finally:
                    if switched:
                        self.wd.switch_to.parent_frame()

        self.wd.switch_to.default_content()
        try:
            visit(True)
            shot = self.wd.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            Path(path).write_bytes(base64.b64decode(shot["data"]))
        finally:
            self.wd.switch_to.default_content()
            try:
                visit(False)
            finally:
                self.wd.switch_to.default_content()
                self._frame_path = []
                for selector in selected:
                    nodes = self.wd.find_elements(By.CSS_SELECTOR, selector)
                    if len(nodes) != 1:
                        break
                    self.wd.switch_to.frame(nodes[0])
                    self._frame_path.append(selector)

    # --- elements ----------------------------------------------------
    def _all(self, locator: dict) -> list:
        query = {k: locator.get(k) for k in ("by", "value", "role", "name")}
        return self.wd.execute_script(FIND, query) or []

    def _one(self, locator: dict):
        found = self._all(locator)
        index = locator.get("nth", 0)
        if len(found) <= index:
            from .ui import LOCATOR, StepError

            raise StepError(LOCATOR, "页面上找不到：" + ir.describe_locator(locator))
        return found[index]

    def count(self, locator: dict) -> int:
        return len(self._all(locator))

    def read(self, locator: dict, what: str, arg: str | None = None):
        node = self._one(locator)
        if what == "visible":
            return node.is_displayed()
        if what == "checked":
            return node.is_selected()
        if what == "value":
            return node.get_property("value")
        if what == "attribute":
            return node.get_dom_attribute(arg)
        return self.wd.execute_script("return arguments[0].innerText ?? arguments[0].textContent ?? '';", node)

    def image(self, locator: dict) -> bytes:
        """The element as the user sees it (PNG), for reading a captcha."""
        return self._one(locator).screenshot_as_png

    def _insert(self, node, text: str, replace: bool) -> None:
        """Type text; `replace` first selects whatever the field already holds."""
        from selenium.webdriver.common.keys import Keys

        self.wd.execute_script("arguments[0].scrollIntoView({block:'center'});arguments[0].focus();", node)
        if replace:
            self.wd.execute_script("if (arguments[0].select) arguments[0].select();", node)
            if not text:
                node.send_keys(Keys.DELETE)
                return
        if any(ord(ch) > 0xFFFF for ch in text):  # chromedriver cannot type characters outside the BMP
            if replace:
                node.send_keys(Keys.DELETE)
            self.wd.execute_cdp_cmd("Input.insertText", {"text": text})
        else:
            node.send_keys(text)

    def act(self, locator: dict, op: str, value) -> None:
        from selenium.common.exceptions import NoSuchElementException
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.support.ui import Select

        node = self._one(locator)
        if op in ("fill", "type"):
            self._insert(node, value, replace=op == "fill")
        elif op == "select":
            try:
                Select(node).select_by_value(value)
            except NoSuchElementException:
                Select(node).select_by_visible_text(value)
        elif op == "click":
            node.click()
        elif op == "dblclick":
            ActionChains(self.wd).double_click(node).perform()
        elif op == "hover":
            ActionChains(self.wd).move_to_element(node).perform()
        elif op in ("check", "uncheck"):
            if node.is_selected() != (op == "check"):
                node.click()
        else:
            raise ValueError("不支持的动作：" + op)

    def press(self, locator: dict | None, key: str) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        parts = ["+"] if key == "+" else key.split("+")
        keys = [getattr(Keys, KEYS[part]) if part in KEYS else part for part in parts]
        if locator:
            self.wd.execute_script("arguments[0].scrollIntoView({block:'center'});arguments[0].focus();", self._one(locator))
        names = {Keys.ALT: "Alt", Keys.CONTROL: "Control", Keys.META: "Meta", Keys.SHIFT: "Shift"}
        temporary = [code for code in keys[:-1] if names.get(code) not in self._held_modifiers]
        chain = ActionChains(self.wd)
        for modifier in temporary:
            chain.key_down(modifier)
        chain.send_keys(keys[-1])
        for modifier in reversed(temporary):
            chain.key_up(modifier)
        chain.perform()
        if keys[-1] in names:
            self._held_modifiers.discard(names[keys[-1]])

    def check_health(self, final=False) -> None:
        self.events.check()
        if final and self.dialogs.pending():
            raise ValueError("已准备的 dialog 选择没有被页面对话框使用")

    def action(self, step: dict, locator=None, target=None, timeout=None):
        """Portable primitives; the selected WebDriver frame never owns the tab."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        self.events.check()
        op = step["action"]
        timeout = float(self.config.get("timeout_seconds", 20)) if timeout is None else timeout
        if op == "frame":
            selector = step["selector"]
            if selector == "main":
                self.wd.switch_to.default_content()
                self._frame_path = []
            else:
                from selenium.common.exceptions import NoSuchFrameException, StaleElementReferenceException

                deadline = time.monotonic() + timeout
                while True:
                    nodes = self.wd.find_elements(By.CSS_SELECTOR, selector)
                    if len(nodes) > 1 or (nodes and nodes[0].tag_name.lower() not in ("iframe", "frame")):
                        raise ValueError("frame 选择器必须匹配一个 iframe/frame 元素")
                    if nodes:
                        try:
                            self.wd.switch_to.frame(nodes[0])
                            break
                        except (NoSuchFrameException, StaleElementReferenceException):
                            pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("等待 frame 连接超时")
                    time.sleep(0.05)
                self._frame_path.append(selector)
                origin = self.wd.execute_script("return location.origin;")
                if origin and origin.startswith(("http://", "https://")):
                    self._origins.add(origin)
        elif op == "dialog":
            self.dialogs.arm(step["mode"], step.get("text"))
        elif op == "eval":
            result = self.wd.execute_async_script(
                "const done=arguments[arguments.length-1]; Promise.resolve(" + eval_expression(step["script"]) + ")"
                ".then(value=>done({ok:true,value}), error=>done({ok:false,error:error.name}));")
            if not result or not result.get("ok"):
                raise ValueError("eval 的 JavaScript 执行失败：" + str((result or {}).get("error", "Error")))
            return result.get("value")
        elif op == "keyboard":
            operation, value = step["operation"], step["value"]
            if operation == "inserttext":
                self.wd.execute_cdp_cmd("Input.insertText", {"text": value})
            elif operation == "type":
                self._insert(self.wd.switch_to.active_element, value, replace=False)
            elif operation == "press":
                self.press(None, value)
            elif operation in ("down", "up"):
                code = getattr(Keys, KEYS[value]) if value in KEYS else value
                getattr(ActionChains(self.wd), "key_" + operation)(code).perform()
                modifier = {Keys.ALT: "Alt", Keys.CONTROL: "Control", Keys.META: "Meta", Keys.SHIFT: "Shift"}.get(code)
                if modifier:
                    self._held_modifiers.add(modifier) if operation == "down" else self._held_modifiers.discard(modifier)
            else:
                raise ValueError("不支持的 keyboard 操作")
        elif op == "mouse":
            operation = step["operation"]
            old = self._mouse_state
            x, y, buttons = old["x"], old["y"], set(old["buttons"])
            button = step.get("button", "left")
            bits = {"left": 1, "right": 2, "middle": 4}
            if button not in bits:
                raise ValueError("mouse button 必须是 left/middle/right")
            if operation == "move":
                x, y = step["x"], step["y"]
                data = {"type": "mouseMoved", "x": x, "y": y}
            elif operation in ("down", "up"):
                buttons.add(button) if operation == "down" else buttons.discard(button)
                data = {"type": "mousePressed" if operation == "down" else "mouseReleased", "x": x, "y": y,
                        "button": button, "clickCount": 1}
            elif operation == "wheel":
                data = {"type": "mouseWheel", "x": x, "y": y, "deltaX": step.get("delta_x", 0), "deltaY": step.get("delta_y", 100)}
            else:
                raise ValueError("不支持的 mouse 操作")
            data["buttons"] = sum(bits[item] for item in buttons)
            data["modifiers"] = sum({"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}[key] for key in self._held_modifiers)
            self.wd.execute_cdp_cmd("Input.dispatchMouseEvent", data)
            self._mouse_state = {"x": x, "y": y, "buttons": buttons}
        elif op == "drag":
            ActionChains(self.wd).drag_and_drop(self._one(locator), self._one(target)).perform()
        elif op == "focus":
            self.wd.execute_script("arguments[0].focus();", self._one(locator))
        elif op == "scroll_into_view":
            self.wd.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center',behavior:'instant'});", self._one(locator))
        elif op == "scroll":
            dx, dy = scroll_delta(step.get("direction", "down"), step.get("pixels", 300))
            if locator:
                self.wd.execute_script("arguments[0].scrollBy(arguments[1],arguments[2]);", self._one(locator), dx, dy)
            else:
                self.wd.execute_script("window.scrollBy(arguments[0],arguments[1]);", dx, dy)
        elif op == "wait":
            if "milliseconds" in step:
                time.sleep(step["milliseconds"] / 1000)
            else:
                query = locator or {"by": "css", "value": step["selector"]}
                state = step.get("state", "visible")
                stable = float(step.get("stable_for_seconds", 0))
                deadline = time.monotonic() + timeout
                stable_since = None
                while True:
                    count = self.count(query)
                    attached = count > query.get("nth", 0)
                    visible = attached and self.read(query, "visible")
                    matched = {"attached": attached, "detached": not attached, "visible": visible, "hidden": not visible}[state]
                    now = time.monotonic()
                    if matched:
                        if stable_since is None:
                            stable_since = now
                        if now - stable_since >= stable and now <= deadline:
                            break
                    else:
                        stable_since = None
                    if now >= deadline:
                        raise TimeoutError("等待元素状态持续稳定超时")
                    self.events.check()
                    time.sleep(0.05)
        elif op == "upload":
            files = [str(project_asset(self.asset_root, name)) for name in step["files"]]
            self._one(locator).send_keys("\n".join(files))
        elif op == "download":
            destination = output_file(self.output_root, step["filename"])
            self.events.download(lambda: self._one(locator).click(), destination, timeout)
            from .. import artifacts

            self.downloads.append(artifacts.metadata(destination, root=self.output_root, kind="download"))
            return destination
        elif op == "clipboard":
            if step.get("operation") != "write":
                raise ValueError("clipboard 仅支持可回放的 write 操作")
            self.events.grant_permissions(["clipboardReadWrite", "clipboardSanitizedWrite"])
            success = self.wd.execute_async_script("const done=arguments[arguments.length-1]; "
                                                  "navigator.clipboard.writeText(arguments[0]).then(()=>done(true),()=>done(false));", step["text"])
            if not success:
                raise ValueError("浏览器拒绝写入剪贴板")
        elif op == "browser_command":
            self.commands.execute(step["command"], step["args"])
        else:
            raise ValueError("没有可移植实现的浏览器动作：" + op)
        self.events.check()
