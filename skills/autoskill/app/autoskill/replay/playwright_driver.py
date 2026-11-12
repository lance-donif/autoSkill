"""Playwright driver for the shared UI replay loop."""
from __future__ import annotations

from pathlib import Path

from .. import paths
from . import ir


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, config: dict, headless: bool = True):
        from playwright.sync_api import sync_playwright

        self.config = config
        self.allowed = list(config.get("allowed_origins", []))
        self.timeout_ms = int(float(config.get("timeout_seconds", 20)) * 1000)
        self._pw = sync_playwright().start()
        options = {"headless": headless, "args": [*paths.QUIET_CHROME_ARGS, *paths.fence_args(self.allowed)]}
        if paths.chrome():
            options["executable_path"] = paths.chrome()
        try:
            self.browser = self._pw.chromium.launch(**options)
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
        self.context = self.browser.new_context(
            service_workers="block", storage_state=state, viewport={"width": 1366, "height": 860}
        )
        self.context.set_default_timeout(self.timeout_ms)
        self.context.route("**/*", self._gate)
        self.page = self.context.new_page()
        self.blocked = []

    def _gate(self, route) -> None:
        try:
            ir.check_origin(route.request.url, self.allowed)
        except PermissionError:
            self.blocked.append(ir.origin_of(route.request.url))
            route.abort()
        else:
            route.continue_()

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
    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")

    def reload(self) -> None:
        self.page.reload(wait_until="domcontentloaded")

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
