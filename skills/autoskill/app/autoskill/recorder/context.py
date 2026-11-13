"""Inspect the recorder's owned browser in explicit frame and open-shadow scopes."""
from __future__ import annotations

import copy
from pathlib import Path

from ..replay.ui import LOCATOR, StepError
from . import agent

DOM_JS = (Path(__file__).resolve().parents[1] / "replay" / "dom.js").read_text(encoding="utf-8")


class AttachedPage:
    """Attach without launching, closing or replacing any page in the recording."""

    def __init__(self, session: str, rec: dict):
        self.session, self.rec = session, rec
        self.pw = None
        self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        endpoint = (agent.call(self.session, ["get", "cdp-url"]) or {}).get("cdpUrl")
        if not endpoint:
            raise StepError(LOCATOR, "当前录制浏览器没有提供连接地址，未连接其他浏览器")
        self.pw = sync_playwright().start()
        try:
            browser = self.pw.chromium.connect_over_cdp(endpoint)
            native = self.rec.get("active_tab")
            tabs = (agent.call(self.session, ["tab", "list"]) or {}).get("tabs", [])
            if not native:
                native = next((tab["tabId"] for tab in tabs if tab.get("active")), None)
            target_id = next((tab.get("targetId", tab["tabId"]) for tab in tabs if tab["tabId"] == native), None)
            for browser_context in browser.contexts:
                for page in browser_context.pages:
                    cdp = browser_context.new_cdp_session(page)
                    try:
                        identity = cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]
                    finally:
                        cdp.detach()
                    if identity == target_id:
                        self.page = page
                        self.page.set_default_timeout(20000)
                        return self
            raise StepError(LOCATOR, "当前录制标签页已经关闭或无法识别，未改用其他标签页")
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _kind, _error, _traceback):
        # Stopping the client disconnects CDP; Browser.close would stop the owned browser.
        if self.pw:
            self.pw.stop()
            self.pw = None

    def root(self, context: list[dict] | None = None):
        """Resolve afresh, including out-of-process frames, after every DOM replacement."""
        from ..replay.playwright_driver import PlaywrightDriver

        driver = PlaywrightDriver.__new__(PlaywrightDriver)
        driver.page = self.page
        _, root = driver._root(self.rec.get("context", []) if context is None else context)
        return root

    def evaluate(self, expression: str, argument=None, context: list[dict] | None = None):
        root = self.root(context)
        try:
            return root.evaluate(expression, argument)
        finally:
            root.dispose()

    def target(self, spec: dict) -> dict | None:
        return self.evaluate("(root, spec) => (" + DOM_JS + ")(root).target(spec)", spec)

    def read(self, locator: dict, what: str, arg: str | None = None, context: list[dict] | None = None):
        return self.evaluate("(root, args) => (" + DOM_JS + ")(root).read(...args)", [locator, what, arg], context)

    def element(self, locator: dict, context: list[dict] | None = None):
        root = self.root(context)
        try:
            handle = root.evaluate_handle("(root, loc) => (" + DOM_JS + ")(root).find(loc)[loc.nth || 0] || null", locator)
        finally:
            root.dispose()
        node = handle.as_element()
        if node is None:
            handle.dispose()
            raise StepError(LOCATOR, "当前定位上下文中找不到目标元素")
        return node

    def act(self, locator: dict, op: str, value=None) -> None:
        node = self.element(locator)
        try:
            if op in ("fill", "press"):
                getattr(node, op)(value)
            elif op == "type":
                node.type(value)
            elif op == "select":
                node.select_option(value)
            else:
                getattr(node, op)()
        finally:
            node.dispose()

    def image(self, locator: dict) -> bytes:
        node = self.element(locator)
        try:
            return node.screenshot()
        finally:
            node.dispose()

    def snapshot(self, interactive: bool = True) -> tuple[str, dict]:
        rows = self.evaluate("""(root, interactive) => {
          const dom = (""" + DOM_JS + """)(root);
          return Array.from(root.querySelectorAll('*')).flatMap(el => {
            if (/^(HTML|HEAD|BODY|SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName) || !el.getClientRects().length) return [];
            const item = dom.describe(el);
            const frame = /^(IFRAME|FRAME)$/.test(el.tagName), shadow = !!el.shadowRoot;
            const active = item.editable || item.checkable || /^(button|link|combobox|slider)$/.test(item.role || '');
            if (interactive ? !(active || frame || shadow) : !(active || frame || shadow || (item.name && (!el.children.length || item.role)))) return [];
            return item.locators.length ? [{...item, frame, shadow}] : [];
          });
        }""", interactive)
        refs, lines = {}, []
        for index, row in enumerate(rows, 1):
            key = "e" + str(index)
            refs[key] = {"role": row.get("role"), "name": row.get("name"), "locators": row["locators"],
                         "context": copy.deepcopy(self.rec.get("context", []))}
            kind = "iframe" if row["frame"] else "open shadow" if row["shadow"] else row.get("role") or row["tag"]
            lines.append(f'- {kind} "{row.get("name", "")}" [ref={key}]')
        return "\n".join(lines), refs
