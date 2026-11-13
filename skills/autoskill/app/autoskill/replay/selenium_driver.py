"""Selenium driver for the shared UI replay loop.

Chrome and chromedriver are always given by explicit path, so Selenium Manager
never runs and nothing is downloaded. Site traffic uses the browser's normal network.
"""
from __future__ import annotations

import base64
from pathlib import Path

from .. import paths
from . import ir
from .tabs import selenium_tabs

DOM_JS = (Path(__file__).with_name("dom.js")).read_text(encoding="utf-8")
FIND = "return (" + DOM_JS + ")(arguments[1] ? arguments[1].shadowRoot : document).find(arguments[0]);"
KEYS = {"Enter": "ENTER", "Escape": "ESCAPE", "Tab": "TAB", "Backspace": "BACKSPACE", "Delete": "DELETE",
        "Space": "SPACE", "ArrowUp": "ARROW_UP", "ArrowDown": "ARROW_DOWN", "ArrowLeft": "ARROW_LEFT",
        "ArrowRight": "ARROW_RIGHT", "Home": "HOME", "End": "END", "PageUp": "PAGE_UP", "PageDown": "PAGE_DOWN",
        "Control": "CONTROL", "Shift": "SHIFT", "Alt": "ALT", "Meta": "META"}


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

    def __init__(self, config: dict, headless: bool = True):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        runtime = config.get("_execution_runtime")
        chrome, chromedriver = ((runtime.get("browser"), runtime.get("chromedriver")) if runtime is not None
                                else (paths.chrome(bundled=True), paths.chromedriver()))  # the driver must match the browser's version
        if not chrome or not chromedriver:
            raise ValueError("Selenium 回放需要随包的 Chrome 与 chromedriver；未找到，且不会联网下载")
        self.config = config
        self.allowed = list(config.get("allowed_origins", []))
        options = webdriver.ChromeOptions()
        options.binary_location = chrome
        options.page_load_strategy = "eager"
        _bypass_proxy_for_loopback()
        options.set_capability("unhandledPromptBehavior", "accept")  # confirm() before a delete must not cancel it
        for arg in [*paths.QUIET_CHROME_ARGS, "--window-size=1920,1080" if headless else "--start-maximized"]:
            options.add_argument(arg)
        if headless:
            options.add_argument("--headless=new")
        if config.get("ignore_https_errors"):
            options.add_argument("--ignore-certificate-errors")
        self.wd = webdriver.Chrome(options=options, service=Service(executable_path=chromedriver, env=paths.child_env()))
        self.wd.set_page_load_timeout(float(config.get("page_load_seconds", 90)))
        self._seeds: dict[str, str] = {}

    # --- lifecycle ---------------------------------------------------
    def reset(self, state: dict | None = None) -> None:
        """Clean cookies and storage, then optionally seed a saved login state."""
        self.tabs = selenium_tabs(self.wd)
        cdp = self.wd.execute_cdp_cmd
        self.wd.get("about:blank")
        for identifier in self._seeds.values():
            cdp("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})
        self._seeds = {}
        cdp("Network.clearBrowserCookies", {})
        for origin in self.allowed:
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

    def storage_state(self) -> dict:
        self.wd.switch_to.default_content()
        cookies = [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"],
                    "expires": c.get("expires", -1), "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False), "sameSite": c.get("sameSite", "Lax")}
                   for c in self.wd.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]]
        items = self.wd.execute_script("return Object.entries(localStorage).map(([name, value]) => ({name, value}));")
        return {"cookies": cookies, "origins": [{"origin": ir.origin_of(self.wd.current_url), "localStorage": items}]}

    def close(self) -> None:
        self.wd.quit()

    # --- page --------------------------------------------------------
    def tab_action(self, step: dict, timeout: float) -> None:
        self.tabs.action(step, timeout)

    def sync_tabs(self, change: dict, timeout: float) -> None:
        self.tabs.sync(change, timeout)

    def goto(self, url: str) -> None:
        self.wd.get(url)
        seeded = self._seeds.pop(ir.origin_of(url), None)
        if seeded:  # seed storage once, never overwrite what the app writes later
            self.wd.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", {"identifier": seeded})

    def reload(self) -> None:
        self.wd.switch_to.default_content()
        self.wd.refresh()

    def url(self) -> str:
        return self.wd.current_url

    def title(self) -> str:
        self.wd.switch_to.default_content()
        return self.wd.title

    def screenshot(self, path: Path) -> None:
        self.wd.switch_to.default_content()
        masks = ["input[type=password]", *self.config.get("screenshot_masks", [])]
        css = ",".join(masks) + "{background:#ff00ff!important;color:transparent!important;-webkit-text-fill-color:transparent!important}"
        self.wd.execute_script("const s=document.createElement('style');s.id='__autoskill_mask';s.textContent=arguments[0];document.head.append(s);", css)
        try:
            shot = self.wd.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            Path(path).write_bytes(base64.b64decode(shot["data"]))
        finally:
            self.wd.execute_script("const s=document.getElementById('__autoskill_mask');if(s)s.remove();")

    # --- elements ----------------------------------------------------
    def _query(self, locator: dict, root=None) -> list:
        from .ui import LOCATOR, StepError

        try:
            return self.wd.execute_script(FIND, locator, root) or []
        except Exception as error:
            raise StepError(LOCATOR, "定位读取失败：" + str(error).splitlines()[0]) from error

    def _root(self, path: list):
        """Return the last open shadow host after entering the ordered frame/root path."""
        from .ui import LOCATOR, StepError

        self.wd.switch_to.default_content()
        root = None
        for index, segment in enumerate(path, 1):
            node = None
            for candidate in segment["locators"]:
                found = self._query(candidate, root)
                wanted = candidate.get("nth", 0)
                if len(found) > wanted:
                    node = found[wanted]
                    break
            if node is None:
                raise StepError(LOCATOR, f"第 {index} 层 {segment['kind']} 上下文未找到，不能判断目标元素不存在")
            if segment["kind"] == "frame":
                if node.tag_name.lower() not in ("iframe", "frame"):
                    raise StepError(LOCATOR, f"第 {index} 层上下文不是 iframe")
                self.wd.switch_to.frame(node)
                root = None
            else:
                if not self.wd.execute_script("return arguments[0].shadowRoot !== null;", node):
                    raise StepError(LOCATOR, f"第 {index} 层没有开放的 Shadow DOM；不支持 closed 模式")
                root = node
        return root

    def _all(self, locator: dict) -> list:
        from .ui import LOCATOR, StepError

        try:
            return self._query(locator, self._root(locator.get("context", [])))
        except Exception as error:
            if isinstance(error, StepError):
                raise
            raise StepError(LOCATOR, "定位上下文读取失败：" + type(error).__name__) from error

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
            # WebDriver refocuses nodes across frame/shadow boundaries; select after that focus.
            node.send_keys(Keys.CONTROL, "a", Keys.NULL, Keys.BACKSPACE)
            if not text:
                return
        if any(ord(ch) > 0xFFFF for ch in text):  # chromedriver cannot type characters outside the BMP
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

        keys = [getattr(Keys, KEYS[part]) if part in KEYS else part for part in key.split("+")]
        if locator:
            self._one(locator).send_keys(*keys)
            return
        chain = ActionChains(self.wd)
        for modifier in keys[:-1]:
            chain.key_down(modifier)
        chain.send_keys(keys[-1])
        for modifier in reversed(keys[:-1]):
            chain.key_up(modifier)
        chain.perform()
