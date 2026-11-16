"""Playwright driver for the shared UI replay loop."""
from __future__ import annotations

from pathlib import Path

from .. import paths
from . import ir
from .tabs import playwright_tabs

DOM_JS = Path(__file__).with_name("dom.js").read_text(encoding="utf-8")


class PlaywrightAction:
    """Let a recorded self-close reach tab synchronization and the surviving page's assertions."""

    def __init__(self, page, tabs, change):
        self.page, self.tabs, self.change = page, tabs, change or {}

    def __enter__(self):
        return self

    def __exit__(self, _kind, error, _traceback):
        from playwright.sync_api import Error as PlaywrightError

        if not isinstance(error, PlaywrightError) or type(error).__name__ != "TargetClosedError":
            return False
        closed = [self.tabs.known.get(tab) for tab in self.change.get("closed", [])]
        active = self.tabs.known.get(self.change.get("active"))
        browser = self.page.context.browser
        return (self.page in closed and self.page.is_closed() and browser is not None and browser.is_connected()
                and active is not None and active is not self.page and active in self.page.context.pages and not active.is_closed())


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
        runtime = config.get("_execution_runtime")
        chosen, ours = ((runtime.get("browser"), runtime.get("fallback_browser")) if runtime is not None
                        else (paths.chrome(), paths.chrome(bundled=True)))
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

    def action_scope(self, step: dict):
        return PlaywrightAction(self.page, self.tabs, step.get("tabs"))

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
    @staticmethod
    def _query(root, locator: dict):
        from .ui import LOCATOR, StepError

        try:
            result = root.evaluate_handle("(root, locator) => (" + DOM_JS + ")(root).find(locator)", locator)
            try:
                return [handle.as_element() for handle in result.get_properties().values()]
            finally:
                result.dispose()
        except Exception as error:
            message = str(error).splitlines()[0]
            if "业务目标不唯一：" in message:
                message = message[message.index("业务目标不唯一："):]
            raise StepError(LOCATOR, "定位读取失败：" + message) from error

    def _root(self, path: list):
        """Re-enter every frame/root from the active tab, never reusing a stale element."""
        from .ui import LOCATOR, StepError

        frame = self.page.main_frame
        root = frame.evaluate_handle("document")
        try:
            for index, segment in enumerate(path, 1):
                node = None
                for candidate in segment["locators"]:
                    matches = self._query(root, candidate)
                    wanted = candidate.get("nth", 0)
                    node = matches[wanted] if len(matches) > wanted else None
                    for match in matches:
                        if match is not node:
                            match.dispose()
                    if node is not None:
                        break
                if node is None:
                    raise StepError(LOCATOR, f"第 {index} 层 {segment['kind']} 上下文未找到，不能判断目标元素不存在")
                try:
                    if segment["kind"] == "frame":
                        frame = node.content_frame()
                        if frame is None:
                            raise StepError(LOCATOR, f"第 {index} 层上下文不是已加载的 iframe")
                        next_root = frame.evaluate_handle("document")
                    else:
                        next_root = node.evaluate_handle("el => el.shadowRoot")
                        if next_root.evaluate("root => root === null"):
                            next_root.dispose()
                            raise StepError(LOCATOR, f"第 {index} 层没有开放的 Shadow DOM；不支持 closed 模式")
                finally:
                    node.dispose()
                root.dispose()
                root = next_root
            return frame, root
        except Exception as error:
            root.dispose()
            if isinstance(error, StepError):
                raise
            raise StepError(LOCATOR, "定位上下文读取失败：" + type(error).__name__) from error

    def _all(self, locator: dict):
        _, root = self._root(locator.get("context", []))
        try:
            return self._query(root, locator)
        finally:
            root.dispose()

    def _one(self, locator: dict):
        from .ui import LOCATOR, StepError

        found = self._all(locator)
        index = locator.get("nth", 0)
        node = found[index] if len(found) > index else None
        for item in found:
            if item is not node:
                item.dispose()
        if node is None:
            raise StepError(LOCATOR, "页面上找不到：" + ir.describe_locator(locator))
        return node

    def count(self, locator: dict) -> int:
        found = self._all(locator)
        try:
            return len(found)
        finally:
            for node in found:
                node.dispose()

    def read(self, locator: dict, what: str, arg: str | None = None):
        node = self._one(locator)
        try:
            if what == "visible":
                return node.is_visible()
            if what == "checked":
                return node.is_checked()
            if what == "value":
                return node.input_value()
            if what == "attribute":
                return node.get_attribute(arg)
            return node.evaluate("el => el.innerText ?? el.textContent ?? ''")
        finally:
            node.dispose()

    def image(self, locator: dict) -> bytes:
        """The element as the user sees it (PNG), for reading a captcha."""
        node = self._one(locator)
        try:
            return node.screenshot()
        finally:
            node.dispose()

    def act(self, locator: dict, op: str, value) -> None:
        node = self._one(locator)
        try:
            if op == "fill":
                node.fill(value)
            elif op == "type":
                node.type(value)
            elif op == "select":
                node.select_option(value)
            else:
                getattr(node, op)()  # click, dblclick, hover, check, uncheck
        finally:
            node.dispose()

    def press(self, locator: dict | None, key: str) -> None:
        if locator:
            node = self._one(locator)
            try:
                node.press(key)
            finally:
                node.dispose()
        else:
            self.page.keyboard.press(key)
