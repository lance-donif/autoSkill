"""Playwright driver for the shared UI replay loop."""
from __future__ import annotations

from pathlib import Path
import time

from .. import media, paths
from . import ir
from .primitives import DialogPolicy, PlaywrightCommands, eval_expression, output_file, project_asset, scroll_delta
from .tabs import playwright_tabs


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, config: dict, headless: bool = True, asset_root=None, output_root=None):
        from playwright.sync_api import sync_playwright

        self.config = config
        self.asset_root = asset_root if asset_root is not None else config.get("_asset_root")
        self.output_root = output_root if output_root is not None else config.get("_output_root")
        self.downloads = []
        self.allowed = list(config.get("allowed_origins", []))
        self.timeout_ms = int(float(config.get("timeout_seconds", 20)) * 1000)
        self.load_ms = int(float(config.get("page_load_seconds", 90)) * 1000)
        self._pw = sync_playwright().start()
        self.headless = headless
        options = {"headless": headless, "args": [*paths.QUIET_CHROME_ARGS, *media.chrome_args(config, self.asset_root),
                                                  *([] if headless else ["--start-maximized"])]}
        if paths.chrome():
            options["executable_path"] = paths.chrome()
        try:
            self.browser = self._pw.chromium.launch(**options)
        except Exception:
            self._pw.stop()
            raise
        self.context = None
        self.page = None
        self.scope = None
        self.blocked: list[str] = []

    # --- lifecycle ---------------------------------------------------
    def reset(self, state: dict | None = None) -> None:
        """Start a clean browser context, optionally with a saved login state."""
        if self.context:
            self.context.close()
        # a visible window is maximised and the page fills it; without a window the page is a full-size screen
        size = {"viewport": {"width": 1920, "height": 1080}} if self.headless else {"no_viewport": True}
        self.context = self.browser.new_context(
            storage_state=state, ignore_https_errors=bool(self.config.get("ignore_https_errors")),
            permissions=media.permissions(self.config), **size)
        self.dialogs = DialogPolicy()
        self.context.on("dialog", self._handle_dialog)
        self.context.set_default_timeout(self.timeout_ms)
        self.commands = PlaywrightCommands(self.context)
        self.context.on("page", self._watch_page)
        self.page = self.context.new_page()
        self.scope = self.page.main_frame
        self.tabs = playwright_tabs(self.page)
        self.blocked = []
        self.downloads = []

    def _watch_page(self, page) -> None:
        def navigated(frame):
            if page is self.page and frame == page.main_frame:
                self.scope = page.main_frame

        page.on("framenavigated", navigated)

    def _handle_dialog(self, dialog) -> None:
        mode, text = self.dialogs.take()
        if mode == "dismiss":
            dialog.dismiss()
        elif text is None:
            dialog.accept()
        else:
            dialog.accept(prompt_text=text)

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
        self.scope = self.page.main_frame

    def sync_tabs(self, change: dict, timeout: float) -> None:
        self.page = self.tabs.sync(change, timeout)
        self.scope = self.page.main_frame

    def goto(self, url: str) -> None:
        self.scope = self.page.main_frame
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.load_ms)

    def reload(self) -> None:
        self.scope = self.page.main_frame
        self.page.reload(wait_until="domcontentloaded", timeout=self.load_ms)

    def back(self) -> None:
        self.scope = self.page.main_frame
        self.page.go_back(wait_until="domcontentloaded", timeout=self.load_ms)

    def forward(self) -> None:
        self.scope = self.page.main_frame
        self.page.go_forward(wait_until="domcontentloaded", timeout=self.load_ms)

    def url(self) -> str:
        return self.page.url

    def title(self) -> str:
        return self.page.title()

    def pause(self, seconds: float) -> None:
        # Pump Playwright's event loop while waiting for asynchronous URL/dialog
        # events; Python time.sleep alone leaves cached page.url unchanged.
        self.page.wait_for_timeout(seconds * 1000)

    def screenshot(self, path: Path) -> None:
        masks = [frame.locator(css) for frame in self.page.frames
                 for css in ["input[type=password]", *self.config.get("screenshot_masks", [])]]
        self.page.screenshot(path=str(path), full_page=True, mask=masks)

    # --- elements ----------------------------------------------------
    def _all(self, locator: dict):
        kind, value, page = locator["by"], locator.get("value"), self.scope
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
        blocked_by_modal = op == "type" and node.evaluate(
            "el => [...el.ownerDocument.querySelectorAll('dialog:modal')].some(dialog => !dialog.contains(el))")
        if op in ("fill", "type", "select"):
            # A native modal can leave a visible background field inert. Focus-
            # based input may otherwise report success while going elsewhere.
            # Probe actionability before input; never retry an already sent value.
            node.click(trial=True)
        if op == "fill":
            node.fill(value)
        elif op == "type":
            if blocked_by_modal:
                # Chrome may restore DOM focus after closing a native modal but
                # keep its text editor inactive. Re-establish focus before any
                # key is sent; no value or click is retried.
                node.evaluate("el => { el.blur(); el.focus(); }")
            node.focus()
            if not node.evaluate("el => { let active=el.ownerDocument.activeElement; "
                                 "while(active?.shadowRoot?.activeElement) active=active.shadowRoot.activeElement; "
                                 "return active===el || (active?.isContentEditable && active.contains(el)); }"):
                raise ValueError("目标元素暂时不能接收键盘输入")
            node.press_sequentially(value)
        elif op == "select":
            node.select_option(value)
        else:
            getattr(node, op)()  # click, dblclick, hover, check, uncheck

    def press(self, locator: dict | None, key: str) -> None:
        if locator:
            node = self._one(locator)
            node.click(trial=True)
            node.press(key)
        else:
            self.page.keyboard.press(key)

    def check_health(self, final=False) -> None:
        if final and self.dialogs.pending():
            raise ValueError("已准备的 dialog 选择没有被页面对话框使用")

    def action(self, step: dict, locator=None, target=None, timeout=None):
        """Execute a portable primitive with page ownership separate from frame scope."""
        op = step["action"]
        milliseconds = self.timeout_ms if timeout is None else timeout * 1000
        if op == "frame":
            if step["selector"] == "main":
                self.scope = self.page.main_frame
            else:
                node = self.scope.locator(step["selector"])
                node.wait_for(state="attached", timeout=milliseconds)
                if node.count() != 1:
                    raise ValueError("frame 选择器必须匹配一个 iframe/frame 元素")
                element = node.element_handle(timeout=milliseconds)
                frame = element.content_frame() if element else None
                if frame is None:
                    raise ValueError("frame 目标不是已连接的 iframe/frame")
                self.scope = frame
        elif op == "dialog":
            self.dialogs.arm(step["mode"], step.get("text"))
        elif op == "eval":
            return self.scope.evaluate(eval_expression(step["script"]))
        elif op == "keyboard":
            operation = step["operation"]
            method = {"type": "type", "inserttext": "insert_text", "press": "press", "down": "down", "up": "up"}.get(operation)
            if method is None:
                raise ValueError("不支持的 keyboard 操作")
            getattr(self.page.keyboard, method)(step["value"])
        elif op == "mouse":
            operation = step["operation"]
            if operation == "move":
                self.page.mouse.move(step["x"], step["y"])
            elif operation in ("down", "up"):
                getattr(self.page.mouse, operation)(button=step.get("button", "left"))
            elif operation == "wheel":
                self.page.mouse.wheel(step.get("delta_x", 0), step.get("delta_y", 100))
            else:
                raise ValueError("不支持的 mouse 操作")
        elif op == "drag":
            self._one(locator).drag_to(self._one(target), timeout=milliseconds)
        elif op == "focus":
            node = self._one(locator)
            node.click(trial=True, timeout=milliseconds)
            node.focus(timeout=milliseconds)
        elif op == "scroll_into_view":
            self._one(locator).scroll_into_view_if_needed(timeout=milliseconds)
        elif op == "scroll":
            delta = scroll_delta(step.get("direction", "down"), step.get("pixels", 300))
            if locator:
                self._one(locator).evaluate("(el, d) => el.scrollBy(d[0], d[1])", delta)
            else:
                self.scope.evaluate("d => window.scrollBy(d[0], d[1])", delta)
        elif op == "wait":
            if "milliseconds" in step:
                self.page.wait_for_timeout(step["milliseconds"])
            else:
                node = self._one(locator) if locator else self.scope.locator(step["selector"])
                state = step.get("state", "visible")
                stable = float(step.get("stable_for_seconds", 0))
                deadline = time.monotonic() + milliseconds / 1000
                stable_since = None
                while True:
                    attached = node.count() > 0
                    visible = attached and node.is_visible()
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
                    self.page.wait_for_timeout(min(0.05, deadline - now) * 1000)
        elif op == "upload":
            self._one(locator).set_input_files([str(project_asset(self.asset_root, name)) for name in step["files"]], timeout=milliseconds)
        elif op == "download":
            destination = output_file(self.output_root, step["filename"])
            with self.page.expect_download(timeout=milliseconds) as event:
                self._one(locator).click(timeout=milliseconds)
            download = event.value
            if download.failure():
                raise ValueError("浏览器未能完成下载")
            download.save_as(str(destination))
            from .. import artifacts

            self.downloads.append(artifacts.metadata(destination, root=self.output_root, kind="download"))
            return destination
        elif op == "clipboard":
            if step.get("operation") != "write":
                raise ValueError("clipboard 仅支持可回放的 write 操作")
            self.context.grant_permissions(["clipboard-read", "clipboard-write"])
            self.scope.evaluate("text => navigator.clipboard.writeText(text)", step["text"])
        elif op == "browser_command":
            self.commands.execute(step["command"], step["args"], self.page, self.scope)
        else:
            raise ValueError("没有可移植实现的浏览器动作：" + op)
