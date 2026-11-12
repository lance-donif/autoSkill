"""Selenium driver for the shared UI replay loop.

Chrome and chromedriver are always given by explicit path, so Selenium Manager
never runs and nothing is downloaded. WebDriver cannot intercept requests, so
the allow-list is enforced by Chrome itself (see paths.fence_args).
"""
from __future__ import annotations

import base64
from pathlib import Path

from .. import paths
from . import ir

DOM_JS = (Path(__file__).with_name("dom.js")).read_text(encoding="utf-8")
FIND = "return (" + DOM_JS + ")().find(arguments[0]);"
KEYS = {"Enter": "ENTER", "Escape": "ESCAPE", "Tab": "TAB", "Backspace": "BACKSPACE", "Delete": "DELETE",
        "Space": "SPACE", "ArrowUp": "ARROW_UP", "ArrowDown": "ARROW_DOWN", "ArrowLeft": "ARROW_LEFT",
        "ArrowRight": "ARROW_RIGHT", "Home": "HOME", "End": "END", "PageUp": "PAGE_UP", "PageDown": "PAGE_DOWN",
        "Control": "CONTROL", "Shift": "SHIFT", "Alt": "ALT", "Meta": "META"}


class SeleniumDriver:
    name = "selenium"

    def __init__(self, config: dict, headless: bool = True):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        chrome, chromedriver = paths.chrome(), paths.chromedriver()
        if not chrome or not chromedriver:
            raise ValueError("Selenium 回放需要随包的 Chrome 与 chromedriver；未找到，且不会联网下载")
        self.config = config
        self.allowed = list(config.get("allowed_origins", []))
        options = webdriver.ChromeOptions()
        options.binary_location = chrome
        options.page_load_strategy = "eager"
        for arg in [*paths.QUIET_CHROME_ARGS, *paths.fence_args(self.allowed), "--window-size=1366,860"]:
            options.add_argument(arg)
        if headless:
            options.add_argument("--headless=new")
        self.wd = webdriver.Chrome(options=options, service=Service(executable_path=chromedriver, env=paths.child_env()))
        self.wd.set_page_load_timeout(float(config.get("timeout_seconds", 20)))
        self._seeds: dict[str, str] = {}

    # --- lifecycle ---------------------------------------------------
    def reset(self, state: dict | None = None) -> None:
        """Clean cookies and storage, then optionally seed a saved login state."""
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
        cookies = []
        for c in self.wd.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]:
            cookies.append({"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"],
                            "expires": c.get("expires", -1), "httpOnly": c.get("httpOnly", False),
                            "secure": c.get("secure", False), "sameSite": c.get("sameSite", "Lax")})
        items = self.wd.execute_script("return Object.entries(localStorage).map(([name, value]) => ({name, value}));")
        return {"cookies": cookies, "origins": [{"origin": ir.origin_of(self.wd.current_url), "localStorage": items}]}

    def close(self) -> None:
        self.wd.quit()

    # --- page --------------------------------------------------------
    def goto(self, url: str) -> None:
        self.wd.get(url)
        seeded = self._seeds.pop(ir.origin_of(url), None)
        if seeded:  # seed storage once, never overwrite what the app writes later
            self.wd.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", {"identifier": seeded})

    def reload(self) -> None:
        self.wd.refresh()

    def url(self) -> str:
        return self.wd.current_url

    def title(self) -> str:
        return self.wd.title

    def screenshot(self, path: Path) -> None:
        masks = ["input[type=password]", *self.config.get("screenshot_masks", [])]
        css = ",".join(masks) + "{background:#ff00ff!important;color:transparent!important;-webkit-text-fill-color:transparent!important}"
        self.wd.execute_script("const s=document.createElement('style');s.id='__autoskill_mask';s.textContent=arguments[0];document.head.append(s);", css)
        try:
            shot = self.wd.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            Path(path).write_bytes(base64.b64decode(shot["data"]))
        finally:
            self.wd.execute_script("const s=document.getElementById('__autoskill_mask');if(s)s.remove();")

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
