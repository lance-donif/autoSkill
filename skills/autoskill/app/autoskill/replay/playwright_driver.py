"""Playwright driver for the shared UI replay loop."""
from __future__ import annotations

from pathlib import Path

from .. import paths
from . import ir
from .tabs import playwright_tabs


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, config: dict, headless: bool = True):
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        self.config = config
        self.allowed = list(config.get("allowed_origins", []))
        self.timeout_ms = int(float(config.get("timeout_seconds", 20)) * 1000)
        self.load_ms = int(float(config.get("page_load_seconds", 90)) * 1000)
        self._pw = sync_playwright().start()
        self.headless = headless
        options = {"headless": headless, "args": [*paths.QUIET_CHROME_ARGS, *([] if headless else ["--start-maximized"])]}
        chosen, ours = paths.chrome(), paths.chrome(bundled=True)
        if chosen:
            options["executable_path"] = chosen
        try:
            try:
                self.browser = self._pw.chromium.launch(**options)
            except PlaywrightError:
                if not ours or ours == chosen:
                    raise
                self.browser = self._pw.chromium.launch(**{**options, "executable_path": ours})  # the user's Chrome would not start
        except Exception:
            self._pw.stop()
            raise
        self.context = None
        self.page = None
        self.blocked: list[str] = []

    # --- lifecycle ---------------------------------------------------
    def reset(self, state: dict | None = None) -> None:
        """Start a clean browser context, optionally with a saved login state."""
        if self.context:
            self.context.close()
        # a visible window is maximised and the page fills it; without a window the page is a full-size screen
        size = {"viewport": {"width": 1920, "height": 1080}} if self.headless else {"no_viewport": True}
        self.context = self.browser.new_context(
            storage_state=state, ignore_https_errors=bool(self.config.get("ignore_https_errors")), **size)
        self.context.on("dialog", lambda dialog: dialog.accept())  # confirm() before a delete must not cancel it
        self.context.set_default_timeout(self.timeout_ms)
        self.page = self.context.new_page()
        self.tabs = playwright_tabs(self.page)
        self.blocked = []

    def storage_state(self) -> dict:
        return self.context.storage_state()

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()

    # --- page --------------------------------------------------------
    def tab_action(self, step: dict, timeout: float) -> None:
        self.page = self.tabs.action(step, timeout)

    def sync_tabs(self, change: dict, timeout: float) -> None:
        self.page = self.tabs.sync(change, timeout)

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.load_ms)

    def reload(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self.load_ms)

    def url(self) -> str:
        return self.page.url

    def title(self) -> str:
        return self.page.title()

    def screenshot(self, path: Path) -> None:
        masks = [self.page.locator("input[type=password]")]
        masks += [self.page.locator(css) for css in self.config.get("screenshot_masks", [])]
        self.page.screenshot(path=str(path), full_page=True, mask=masks)

    # --- elements ----------------------------------------------------
    def _all(self, locator: dict):
        kind, value, page = locator["by"], locator.get("value"), self.page
        if kind == "testid":
            return page.get_by_test_id(value)
        if kind == "role":
            name = locator.get("name")
            return page.get_by_role(locator["role"], name=name, exact=True) if name else page.get_by_role(locator["role"])
        if kind == "label":
            return page.get_by_label(value, exact=True)
        if kind == "placeholder":
            return page.get_by_placeholder(value, exact=True)
        if kind == "text":
            return page.get_by_text(value, exact=True)
        return page.locator(value)

    def _one(self, locator: dict):
        return self._all(locator).nth(locator.get("nth", 0))

    def count(self, locator: dict) -> int:
        return self._all(locator).count()

    def read(self, locator: dict, what: str, arg: str | None = None):
        node = self._one(locator)
        if what == "visible":
            return node.is_visible()
        if what == "checked":
            return node.is_checked()
        if what == "value":
            return node.input_value()
        if what == "attribute":
            return node.get_attribute(arg)
        return node.evaluate("el => el.innerText ?? el.textContent ?? ''")

    def image(self, locator: dict) -> bytes:
        """The element as the user sees it (PNG), for reading a captcha."""
        return self._one(locator).screenshot()

    def act(self, locator: dict, op: str, value) -> None:
        node = self._one(locator)
        if op == "fill":
            node.fill(value)
        elif op == "type":
            node.press_sequentially(value)
        elif op == "select":
            node.select_option(value)
        else:
            getattr(node, op)()  # click, dblclick, hover, check, uncheck

    def press(self, locator: dict | None, key: str) -> None:
        if locator:
            self._one(locator).press(key)
        else:
            self.page.keyboard.press(key)
