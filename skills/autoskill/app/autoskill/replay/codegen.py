"""Export UI cases as scripts needing only Playwright or Selenium, not autoSkill.

The source embeds its browser helpers and staged upload assets travel with it.
Secrets remain environment references and each prerequisite runs in the same browser.
"""
from __future__ import annotations

import inspect
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .. import media, projects, runner
from . import ir
from .tabs import Tabs, playwright_tabs, selenium_tabs

SECRET = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
DOM_JS = Path(__file__).with_name("dom.js").read_text(encoding="utf-8")


def lit(value) -> str:
    """A Python literal; ENV references and RAND resolve only when the script runs."""
    if not isinstance(value, str):
        return repr(value)
    if ir.RAND in value:
        return " + RAND + ".join(lit(part) for part in value.split(ir.RAND))
    parts, last = [], 0
    for match in SECRET.finditer(value):
        if match.start() > last:
            parts.append(json.dumps(value[last:match.start()], ensure_ascii=False))
        parts.append(f'os.environ["{match[1]}"].replace("{{{{RAND}}}}", RAND)')
        last = match.end()
    if last < len(value) or not parts:
        parts.append(json.dumps(value[last:], ensure_ascii=False))
    return " + ".join(parts)


def data_expr(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(lit(k) + ": " + data_expr(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(data_expr(v) for v in value) + "]"
    return lit(value)


def url_expr(url: str) -> str:
    return lit(url) if urlsplit(url).scheme else f"BASE_URL + {lit(url if url.startswith('/') else '/' + url)}"


def _step_data(step: dict) -> dict:
    result = dict(step)
    candidates = ir.locators(step)
    if candidates:
        result["locators"] = candidates
    return result


def pw_step(step: dict) -> str:
    return "runtime.step(" + data_expr(_step_data(step)) + ")"


def se_step(step: dict) -> str:
    return "runtime.step(" + data_expr(_step_data(step)) + ")"


class Standalone:
    """State shared across prerequisites, without importing the recording product."""

    def absolute(self, url):
        from urllib.parse import urljoin, urlsplit

        return url if urlsplit(url).scheme else urljoin(BASE_URL.rstrip("/") + "/", url.lstrip("/"))

    def wait_until(self, test, message, stable_for=0):
        deadline = time.monotonic() + self.timeout
        error = None
        satisfied_since = None
        while True:
            try:
                if test():
                    if satisfied_since is None:
                        satisfied_since = time.monotonic()
                    if time.monotonic() - satisfied_since >= stable_for:
                        return
                else:
                    satisfied_since = None
            except Exception as caught:
                error = caught
                satisfied_since = None
            if time.monotonic() >= deadline:
                detail = type(error).__name__ if error else "condition not satisfied"
                raise AssertionError(message + " (" + detail + ")") from None
            self.pause(0.05)

    def choose(self, candidates, required=True):
        if not candidates:
            raise ValueError("Step requires locator candidates")
        chosen = []

        def locate():
            for candidate in candidates:
                if self.count(candidate) > candidate.get("nth", 0):
                    chosen[:] = [candidate]
                    return True
            return False

        if required:
            self.wait_until(locate, "Cannot locate element")
        else:
            locate()
        return chosen[0] if chosen else candidates[0]

    def start(self, url, timeout_seconds=None, assertion_timeout_seconds=None):
        self.case_timeout = float(timeout_seconds) if timeout_seconds is not None else TIMEOUT
        self.assertion_timeout = float(assertion_timeout_seconds) if assertion_timeout_seconds is not None else ASSERTION_TIMEOUT
        self.timeout = self.case_timeout
        self.configure_timeout()
        self.begin_tabs()
        self.scope_main()
        self.navigate("goto", self.absolute(url))
        self.apply_settings()

    def step(self, step):
        previous = self.timeout
        self.timeout = float(step.get("timeout_seconds", self.assertion_timeout if "assertion" in step else self.case_timeout))
        self.stable_for = float(step.get("stable_for_seconds", 0))
        self.configure_timeout()
        try:
            self._step(step)
            self.runtime_check()
        finally:
            self.timeout = previous
            self.configure_timeout()

    def _step(self, step):
        if "assertion" in step:
            self.check(step)
        else:
            op = step["action"]
            if op == "native_step":
                raise ValueError("Nonportable recorded command: " + step["command"] + ": " + step["reason"])
            if op in ("goto", "reload", "back", "forward"):
                self.scope_main()
                self.navigate(op, self.absolute(step["url"]) if op == "goto" else None)
            elif op.startswith("tab_"):
                item = dict(step)
                if "url" in item:
                    item["url"] = self.absolute(item["url"])
                self.tab_action(item)
                self.scope_main()
                self.apply_settings()
            elif op == "frame":
                self.frame(step["selector"])
            elif op == "dialog":
                self.dialogs.arm(step["mode"], step.get("text"))
            elif op == "browser_command":
                self.browser_command(step["command"], step["args"])
            elif op == "wait":
                if "milliseconds" in step:
                    self.pause(step["milliseconds"] / 1000)
                else:
                    self.wait_state(step["locators"], step.get("state", "visible"))
            elif op in ("wait_visible", "wait_hidden"):
                self.wait_state(step["locators"], op[5:])
            else:
                self.perform(step)
        if step.get("tabs"):
            change = dict(step["tabs"])
            change["opened"] = [{**item, "url": self.absolute(item["url"]) if item.get("url") else ""}
                                for item in change.get("opened", [])]
            self.tab_sync(change)
            self.scope_main()
            self.apply_settings()

    def apply_settings(self):
        pass  # Command helpers apply context state to existing and future tabs.

    def runtime_check(self):
        pass

    def wait_state(self, candidates, state):
        def ready():
            locator = self.choose(candidates, required=False)
            present = self.count(locator) > locator.get("nth", 0)
            if state in ("attached", "detached"):
                return present == (state == "attached")
            visible = present and self.visible(locator)
            return visible == (state == "visible")

        self.wait_until(ready, "Waiting for element state " + state, self.stable_for)

    def check(self, step):
        op, expected = step["assertion"], step.get("expected")

        def matches():
            if op in ("url", "url_contains", "title"):
                actual = self.title() if op == "title" else self.url()
                if op == "title":
                    return " ".join(str(actual).split()) == " ".join(str(expected).split())
                return expected in actual if op == "url_contains" else actual == expected
            locator = self.choose(step["locators"], required=False)
            count = self.count(locator)
            if op in ("count", "min_count"):
                return count >= expected if op == "min_count" else count == expected
            present = count > locator.get("nth", 0)
            if op in ("visible", "hidden"):
                visible = present and self.visible(locator)
                return visible if op == "visible" else not visible
            if not present:
                return False
            if op == "checked":
                return self.read(locator, "checked") is bool(step.get("expected", True))
            if op == "attribute":
                actual = self.read(locator, "attribute", step["attribute"])
                return expected in (actual or "").split() if step["attribute"] == "class" else actual == expected
            actual = self.read(locator, "value" if op == "value" else "text")
            if op == "contains":
                return expected in actual
            if op == "text":
                return " ".join(str(actual).split()) == " ".join(str(expected).split())
            return actual == expected

        self.wait_until(matches, "Assertion failed: " + op, self.stable_for)


class StandalonePlaywright(Standalone):
    def __init__(self, page, dialogs):
        self.page, self.scope, self.dialogs = page, page, dialogs
        self.commands = PlaywrightCommands(page.context)
        self.timeout = TIMEOUT
        page.context.on("page", self.watch_page)
        self.watch_page(page)

    def watch_page(self, page):
        def navigated(frame):
            if self.page == page and frame == page.main_frame:
                self.scope = page

        page.on("framenavigated", navigated)

    def configure_timeout(self):
        self.page.set_default_timeout(self.timeout * 1000)

    def begin_tabs(self):
        self.tabs = playwright_tabs(self.page)

    def scope_main(self):
        self.scope = self.page

    def tab_action(self, step):
        self.page = self.tabs.action(step, self.timeout)

    def tab_sync(self, change):
        self.page = self.tabs.sync(change, self.timeout)

    def navigate(self, op, url):
        if op == "goto":
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        else:
            method = {"reload": "reload", "back": "go_back", "forward": "go_forward"}[op]
            getattr(self.page, method)(wait_until="domcontentloaded", timeout=self.timeout * 1000)

    def pause(self, seconds):
        self.page.wait_for_timeout(seconds * 1000)

    def all(self, locator):
        kind, value = locator["by"], locator.get("value")
        if kind == "testid":
            return self.scope.get_by_test_id(value)
        if kind == "role":
            return self.scope.get_by_role(locator["role"], **({"name": locator["name"], "exact": True} if locator.get("name") else {}))
        if kind in ("label", "placeholder", "text"):
            return getattr(self.scope, "get_by_" + kind)(value, exact=True)
        return self.scope.locator(value)

    def one(self, locator):
        return self.all(locator).nth(locator.get("nth", 0))

    def count(self, locator):
        return self.all(locator).count()

    def visible(self, locator):
        return self.one(locator).is_visible()

    def read(self, locator, what, arg=None):
        node = self.one(locator)
        if what == "checked":
            return node.is_checked()
        if what == "value":
            return node.input_value()
        if what == "attribute":
            return node.get_attribute(arg)
        return node.evaluate("el => el.innerText ?? el.textContent ?? ''")

    def url(self):
        return self.page.url

    def title(self):
        return self.page.title()

    def frame(self, selector):
        if selector == "main":
            self.scope_main()
            return
        node = self.scope.locator(selector)
        node.first.wait_for(state="attached", timeout=self.timeout * 1000)
        if node.count() != 1 or node.evaluate("el => !['IFRAME','FRAME'].includes(el.tagName)"):
            raise ValueError("Frame selector must identify exactly one iframe")
        selected = []

        def ready():
            element = node.element_handle()
            frame = element.content_frame() if element else None
            selected[:] = [frame] if frame is not None else []
            return bool(selected)

        self.wait_until(ready, "Waiting for frame content")
        self.scope = selected[0]

    def perform(self, step):
        op = step["action"]
        if op == "eval":
            self.scope.evaluate(eval_expression(step["script"]))
            return
        if op == "keyboard":
            operation = step["operation"]
            method = "insert_text" if operation == "inserttext" else operation
            getattr(self.page.keyboard, method)(step["value"])
            return
        if op == "mouse":
            operation = step["operation"]
            if operation == "move":
                self.page.mouse.move(step["x"], step["y"])
            elif operation == "wheel":
                self.page.mouse.wheel(step.get("delta_x", 0), step.get("delta_y", 100))
            else:
                getattr(self.page.mouse, operation)(button=step.get("button", "left"))
            return
        if op == "clipboard":
            self.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            self.scope.evaluate("text => navigator.clipboard.writeText(text)", step["text"])
            return
        if op == "scroll":
            delta = scroll_delta(step["direction"], step["pixels"])
            if step.get("locators"):
                node = self.one(self.choose(step["locators"]))
                node.evaluate("(el, d) => el.scrollBy(d[0], d[1])", list(delta))
            else:
                self.scope.evaluate("d => window.scrollBy(d[0], d[1])", list(delta))
            return
        candidates = step.get("locators")
        node = self.one(self.choose(candidates)) if candidates else None
        if node is not None and op in ("fill", "type", "select", "focus", "captcha", "press"):
            # Filling/focusing a background control can appear to succeed while a
            # modal still owns keyboard focus. Trial checks dispatch no click.
            node.click(trial=True, timeout=self.timeout * 1000)
        if op == "press":
            (node.press if node else self.page.keyboard.press)(step["key"])
        elif op == "fill":
            node.fill(step["value"])
        elif op == "type":
            node.press_sequentially(step["value"])
        elif op == "select":
            try:
                node.select_option(value=step["value"], timeout=min(self.timeout * 1000, 2000))
            except Exception:
                node.select_option(label=step["value"])
        elif op == "captcha":
            node.fill(input("请输入验证码："))
        elif op == "drag":
            node.drag_to(self.one(self.choose(step["target_locators"])))
        elif op == "scroll_into_view":
            node.scroll_into_view_if_needed()
        elif op == "upload":
            node.set_input_files([str(project_asset(PROJECT_ASSETS, name)) for name in step["files"]])
        elif op == "download":
            target = output_file(DOWNLOAD_ROOT, step["filename"])
            with self.page.expect_download(timeout=self.timeout * 1000) as pending:
                node.click()
            pending.value.save_as(str(target))
        elif op in ("click", "dblclick", "hover", "focus", "check", "uncheck"):
            getattr(node, op)()
        else:
            raise ValueError("Unsupported action: " + op)

    def browser_command(self, command, args):
        self.commands.execute(command, args, self.page, self.scope)


class StandaloneSelenium(Standalone):
    def __init__(self, driver, dialogs):
        self.driver, self.dialogs = driver, dialogs
        self.mouse_x = self.mouse_y = 0
        self.mouse_buttons = 0
        self.held_modifiers = set()
        self.dialog_manager = SeleniumDialogs(driver, dialogs)
        self.commands = SeleniumCommands(driver, self.dialog_manager)
        if "microphone" in permissions(LAUNCH_CONFIG):
            self.dialog_manager.grant_permissions(["audioCapture"])
        self.timeout = TIMEOUT

    def configure_timeout(self):
        self.driver.set_page_load_timeout(self.timeout)
        self.driver.set_script_timeout(self.timeout)

    def close(self):
        self.dialog_manager.close()

    def runtime_check(self):
        self.dialog_manager.check()

    def begin_tabs(self):
        self.tabs = selenium_tabs(self.driver)

    def scope_main(self):
        self.driver.switch_to.default_content()

    def tab_action(self, step):
        self.tabs.action(step, self.timeout)

    def tab_sync(self, change):
        self.tabs.sync(change, self.timeout)

    def navigate(self, op, url):
        if op == "goto":
            self.driver.get(url)
        else:
            getattr(self.driver, {"reload": "refresh", "back": "back", "forward": "forward"}[op])()

    def pause(self, seconds):
        self.dialog_manager.check()
        time.sleep(seconds)

    def all(self, locator):
        return self.driver.execute_script("return (" + DOM_JS + ")().find(arguments[0]);", locator) or []

    def one(self, locator):
        return self.all(locator)[locator.get("nth", 0)]

    def count(self, locator):
        return len(self.all(locator))

    def visible(self, locator):
        return self.one(locator).is_displayed()

    def read(self, locator, what, arg=None):
        node = self.one(locator)
        if what == "checked":
            return node.is_selected()
        if what == "value":
            return node.get_property("value")
        if what == "attribute":
            return node.get_dom_attribute(arg)
        return self.driver.execute_script("return arguments[0].innerText ?? arguments[0].textContent ?? '';", node)

    def url(self):
        return self.driver.current_url

    def title(self):
        return self.driver.title

    def frame(self, selector):
        if selector == "main":
            self.scope_main()
        else:
            from selenium.common.exceptions import NoSuchFrameException, StaleElementReferenceException

            locator = {"by": "css", "value": selector}

            def ready():
                found = self.all(locator)
                if not found:
                    return False
                if len(found) != 1 or found[0].tag_name.lower() not in ("iframe", "frame"):
                    raise ValueError("Frame selector must identify exactly one iframe")
                try:
                    self.driver.switch_to.frame(found[0])
                    return True
                except (NoSuchFrameException, StaleElementReferenceException):
                    return False

            self.wait_until(ready, "Waiting for frame content")

    def key(self, value):
        names = {"Enter": "ENTER", "Escape": "ESCAPE", "Esc": "ESCAPE", "Tab": "TAB", "Backspace": "BACKSPACE", "Delete": "DELETE", "Space": "SPACE", "Control": "CONTROL", "ControlOrMeta": "CONTROL", "Ctrl": "CONTROL", "Shift": "SHIFT", "Alt": "ALT", "Meta": "META", "Cmd": "META", "Command": "META", "ArrowUp": "ARROW_UP", "ArrowDown": "ARROW_DOWN", "ArrowLeft": "ARROW_LEFT", "ArrowRight": "ARROW_RIGHT", "Home": "HOME", "End": "END", "PageUp": "PAGE_UP", "PageDown": "PAGE_DOWN", "Insert": "INSERT"}
        if value in names:
            return getattr(Keys, names[value])
        if re.fullmatch(r"F(?:[1-9]|1[0-2])", value):
            return getattr(Keys, value)
        return value

    def modifier(self, key):
        return {Keys.ALT: "Alt", Keys.CONTROL: "Control", Keys.META: "Meta", Keys.SHIFT: "Shift"}.get(key)

    def press(self, value):
        parts = ["+"] if value == "+" else value.split("+")
        keys = [self.key(part) for part in parts]
        chain = ActionChains(self.driver)
        temporary, active = [], set(self.held_modifiers)
        for key in keys[:-1]:
            modifier = self.modifier(key)
            if modifier and modifier in active:
                continue
            chain.key_down(key)
            temporary.append(key)
            if modifier:
                active.add(modifier)
        chain.send_keys(keys[-1])
        for key in reversed(temporary):
            chain.key_up(key)
        chain.perform()
        # A terminal modifier is pressed and released by send_keys, including
        # when it was held before this press. Temporary chord modifiers are gone.
        terminal = self.modifier(keys[-1])
        if terminal:
            self.held_modifiers.discard(terminal)

    def perform(self, step):
        op, driver = step["action"], self.driver
        if op == "eval":
            driver.execute_script("return (" + eval_expression(step["script"]) + ");")
            return
        if op == "keyboard":
            operation, value = step["operation"], step["value"]
            if operation == "inserttext" or (operation == "type" and any(ord(ch) > 0xFFFF for ch in value)):
                driver.execute_cdp_cmd("Input.insertText", {"text": value})
            elif operation == "type":
                ActionChains(driver).send_keys(value).perform()
            elif operation == "press":
                self.press(value)
            else:
                chain = ActionChains(driver)
                key = self.key(value)
                getattr(chain, "key_down" if operation == "down" else "key_up")(key).perform()
                modifier = self.modifier(key)
                if modifier:
                    self.held_modifiers.add(modifier) if operation == "down" else self.held_modifiers.discard(modifier)
            return
        if op == "mouse":
            operation = step["operation"]
            if operation == "move":
                self.mouse_x, self.mouse_y = step["x"], step["y"]
            button = step.get("button", "left")
            mask = {"left": 1, "right": 2, "middle": 4}[button]
            if operation == "down":
                self.mouse_buttons |= mask
            elif operation == "up":
                self.mouse_buttons &= ~mask
            data = {"type": {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased", "wheel": "mouseWheel"}[operation], "x": self.mouse_x, "y": self.mouse_y, "buttons": self.mouse_buttons,
                    "modifiers": sum({"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}[key] for key in self.held_modifiers)}
            if operation in ("down", "up"):
                data.update(button=step.get("button", "left"), clickCount=1)
            if operation == "wheel":
                data.update(deltaX=step.get("delta_x", 0), deltaY=step.get("delta_y", 100))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", data)
            return
        if op == "clipboard":
            self.dialog_manager.grant_permissions(["clipboardReadWrite", "clipboardSanitizedWrite"])
            error = driver.execute_async_script("const done=arguments[arguments.length-1]; navigator.clipboard.writeText(arguments[0]).then(()=>done(null),e=>done(String(e)));", step["text"])
            if error:
                raise ValueError("Clipboard write failed")
            return
        if op == "scroll":
            delta = scroll_delta(step["direction"], step["pixels"])
            if step.get("locators"):
                node = self.one(self.choose(step["locators"]))
                driver.execute_script("arguments[0].scrollBy(arguments[1],arguments[2]);", node, *delta)
            else:
                driver.execute_script("window.scrollBy(arguments[0],arguments[1]);", *delta)
            return
        candidates = step.get("locators")
        node = self.one(self.choose(candidates)) if candidates else None
        if op == "press":
            if node:
                driver.execute_script("arguments[0].focus();", node)
            self.press(step["key"])
        elif op in ("fill", "type", "captcha"):
            value = input("请输入验证码：") if op == "captcha" else step["value"]
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", node)
            if op in ("fill", "captcha"):
                driver.execute_script("if(arguments[0].select) arguments[0].select();", node)
                if not value or any(ord(ch) > 0xFFFF for ch in value):
                    node.send_keys(Keys.DELETE)
            if any(ord(ch) > 0xFFFF for ch in value):
                driver.execute_cdp_cmd("Input.insertText", {"text": value})
            elif value:
                node.send_keys(value)
        elif op == "select":
            from selenium.common.exceptions import NoSuchElementException

            try:
                Select(node).select_by_value(step["value"])
            except NoSuchElementException:
                Select(node).select_by_visible_text(step["value"])
        elif op == "click":
            node.click()
        elif op == "dblclick":
            ActionChains(driver).double_click(node).perform()
        elif op == "hover":
            ActionChains(driver).move_to_element(node).perform()
        elif op == "focus":
            driver.execute_script("arguments[0].focus();", node)
        elif op in ("check", "uncheck"):
            if node.is_selected() != (op == "check"):
                node.click()
        elif op == "drag":
            ActionChains(driver).drag_and_drop(node, self.one(self.choose(step["target_locators"]))).perform()
        elif op == "scroll_into_view":
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", node)
        elif op == "upload":
            node.send_keys("\n".join(str(project_asset(PROJECT_ASSETS, name)) for name in step["files"]))
        elif op == "download":
            self.download(node, step["filename"])
        else:
            raise ValueError("Unsupported action: " + op)
        self.dialog_manager.check()

    def download(self, node, filename):
        self.dialog_manager.download(node.click, output_file(DOWNLOAD_ROOT, filename), self.timeout)

    def browser_command(self, command, args):
        self.commands.execute(command, args)


HEADER = r'''import os
import re
import time
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", @@BASE@@).rstrip("/")
TIMEOUT = float(os.environ.get("TIMEOUT", @@TIMEOUT@@))
ASSERTION_TIMEOUT = float(os.environ.get("ASSERTION_TIMEOUT", @@ASSERTION_TIMEOUT@@))
RAND = os.environ.get("RAND") or os.urandom(3).hex()
PROJECT_ASSETS = Path(__file__).resolve().parent / "assets"
_DOWNLOAD_RUN = Path(__file__).stem + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_ROOT", str(Path(__file__).resolve().parent / "downloads" / _DOWNLOAD_RUN))).resolve()
LAUNCH_CONFIG = @@MEDIA@@
'''
PW_MAIN = '''

def main():
    with sync_playwright() as p:
        options = {"headless": os.environ.get("HEADED") != "1", "args": chrome_args(LAUNCH_CONFIG, PROJECT_ASSETS)}
        if os.environ.get("CHROME_BINARY"):
            options["executable_path"] = os.environ["CHROME_BINARY"]
        browser = p.chromium.launch(**options)
        try:
            context = browser.new_context(viewport={"width": 1920, "height": 1080}, accept_downloads=True,
                                          permissions=permissions(LAUNCH_CONFIG))
            context.set_default_timeout(TIMEOUT * 1000)
            dialogs = DialogPolicy()

            def handle_dialog(dialog):
                mode, text = dialogs.take()
                if mode == "dismiss":
                    dialog.dismiss()
                else:
                    dialog.accept(text) if text is not None and dialog.type == "prompt" else dialog.accept()

            context.on("dialog", handle_dialog)
            runtime = StandalonePlaywright(context.new_page(), dialogs)
@@CALLS@@
            if dialogs.pending():
                raise ValueError("A prepared dialog choice was not consumed")
        finally:
            browser.close()
    print(@@PASS@@)


if __name__ == "__main__":
    main()
'''
SE_MAIN = '''

def main():
    options = webdriver.ChromeOptions()
    if os.environ.get("HEADED") != "1":
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    for argument in chrome_args(LAUNCH_CONFIG, PROJECT_ASSETS):
        options.add_argument(argument)
    options.add_experimental_option("prefs", {"profile.default_content_setting_values.media_stream_mic":
                                             1 if "microphone" in permissions(LAUNCH_CONFIG) else 2})
    options.set_capability("unhandledPromptBehavior", "ignore")
    if os.environ.get("CHROME_BINARY"):
        options.binary_location = os.environ["CHROME_BINARY"]
    if os.environ.get("CHROMEDRIVER"):
        service = webdriver.ChromeService(executable_path=os.environ["CHROMEDRIVER"])
        driver = webdriver.Chrome(options=options, service=service)
    else:
        driver = webdriver.Chrome(options=options)
    runtime = None
    try:
        driver.set_page_load_timeout(TIMEOUT)
        driver.set_script_timeout(TIMEOUT)
        runtime = StandaloneSelenium(driver, DialogPolicy())
@@CALLS@@
        if runtime.dialogs.pending():
            raise ValueError("A prepared dialog choice was not consumed")
    finally:
        if runtime is not None:
            runtime.close()
        driver.quit()
    print(@@PASS@@)


if __name__ == "__main__":
    main()
'''


def _function(case: dict, engine: str, index: int = 0) -> tuple[str, str]:
    automation = case["extensions"]["automation"]
    name = "case_" + str(index) + "_" + re.sub(r"[^a-z0-9]+", "_", case["id"].lower()).strip("_")
    comment = (str(case["id"]) + " " + str(case["title"])).replace("\r", " ").replace("\n", " ")
    body = [f"def {name}(runtime):", "    # " + comment,
            f"    runtime.start({lit(automation['start_url'])}, {lit(automation.get('timeout_seconds'))}, {lit(automation.get('assertion_timeout_seconds'))})"]
    emit = pw_step if engine == "playwright" else se_step
    for step in [*automation.get("actions", []), *automation.get("assertions", [])]:
        body += ["    # " + ir.describe(step).replace("\n", " ").replace("\r", " "), "    " + emit(step)]
    return name, "\n".join(body)


def script(chain: list[dict], base_url: str, engine: str, config: dict | None = None) -> str:
    """Build a complete independent script; reject every nonportable prerequisite."""
    from . import primitives

    if engine not in ("playwright", "selenium"):
        raise ValueError("Unsupported export engine: " + engine)
    if not chain:
        raise ValueError("Cannot export an empty case chain")
    for case in chain:
        if case.get("kind") != "ui":
            raise ValueError("前置接口用例不能省略；独立页面脚本尚不支持这些前置：" + str(case.get("id", "?")))
        automation = case.get("extensions", {}).get("automation")
        ir.validate(automation)
        for step in automation.get("actions", []):
            if step.get("action") == "native_step":
                raise ValueError("原生命令无法独立回放：" + step["command"] + "：" + step["reason"])
    functions = [_function(case, engine, index) for index, case in enumerate(chain)]
    wanted = sorted({name for case in chain for name in SECRET.findall(json.dumps(case, ensure_ascii=False))})
    description = (f"{chain[-1]['id']} {chain[-1]['title']}\n\n"
                   "autoSkill 独立回放脚本；运行不需要 autoSkill。\n"
                   "环境变量：BASE_URL、HEADED=1、CHROME_BINARY、CHROMEDRIVER、TIMEOUT、DOWNLOAD_ROOT。\n"
                   "上传文件随脚本保存在 assets/，每次下载保存在 downloads/<run>/ 或 DOWNLOAD_ROOT。")
    if wanted:
        description += "\n需要凭据环境变量：" + "、".join(wanted) + "。"
    support = [media.chrome_args, media.permissions]
    support += [getattr(primitives, name) for name in ("project_asset", "output_file", "scroll_delta", "eval_expression",
                                                     "DialogPolicy", "normalize_browser_command", "cookie_from_command")]
    imports = "from playwright.sync_api import sync_playwright\n" if engine == "playwright" else (
        "from selenium import webdriver\nfrom selenium.webdriver.common.action_chains import ActionChains\n"
        "from selenium.webdriver.common.keys import Keys\nfrom selenium.webdriver.support.ui import Select\n")
    if engine == "selenium":
        support += [primitives.SeleniumDialogs, primitives.SeleniumCommands]
    else:
        support.append(primitives.PlaywrightCommands)
    config = config or {}
    timeout = config.get("timeout_seconds", 90)
    assertion_timeout = config.get("assertion_timeout_seconds", timeout)
    header = (HEADER.replace("@@BASE@@", json.dumps(base_url)).replace("@@TIMEOUT@@", repr(str(timeout)))
              .replace("@@ASSERTION_TIMEOUT@@", repr(str(assertion_timeout)))
              .replace("@@MEDIA@@", data_expr({"media": config.get("media", {})})))
    source = [repr(description), header, imports]
    if engine == "selenium":
        source.append("DOM_JS = " + repr(DOM_JS))
    source += [inspect.getsource(item) for item in support]
    source += [inspect.getsource(Tabs), inspect.getsource(playwright_tabs if engine == "playwright" else selenium_tabs),
               inspect.getsource(Standalone), inspect.getsource(StandalonePlaywright if engine == "playwright" else StandaloneSelenium)]
    source += [body for _, body in functions]
    calls = "\n".join(" " * (12 if engine == "playwright" else 8) + f"{name}(runtime)" for name, _ in functions)
    main = PW_MAIN if engine == "playwright" else SE_MAIN
    source.append(main.replace("@@CALLS@@", calls).replace("@@PASS@@", repr("PASS " + str(chain[-1]["id"]))))
    return "\n\n".join(source)


def _upload_assets(chain: list[dict], asset_root: Path, config: dict | None = None) -> list[tuple[str, Path]]:
    assets = {}
    root = asset_root.resolve()
    config = config or {}
    media.chrome_args(config, root)  # Validate fixture bytes before writing any export.
    audio = (config.get("media") or {}).get("audio_input_file")
    if audio:
        assets[audio] = (root / audio).resolve()
    for case in chain:
        automation = case.get("extensions", {}).get("automation")
        ir.validate(automation)
        for step in automation.get("actions", []):
            if step.get("action") != "upload":
                continue
            for name in step["files"]:
                if not ir._relative_file(name):
                    raise ValueError("上传资源不是项目相对路径")
                source = (root / name).resolve()
                if not source.is_relative_to(root) or not source.is_file():
                    raise ValueError("上传资源不存在或超出项目目录：" + name)
                assets[name] = source
    return sorted(assets.items())


def export(project_id: str, out: str | None = None, asset_root: str | Path | None = None) -> dict:
    cases = {case["id"]: case for case in projects.cases(project_id)}
    config = projects.config(project_id)
    base_url = config["base_url"]
    folder = Path(out) if out else projects.path(project_id) / "scripts"
    folder.mkdir(parents=True, exist_ok=True)
    written, copied, notes = [], set(), []
    for case in cases.values():
        if case["kind"] != "ui":
            continue
        try:
            chain = [cases[cid] for cid, _ in runner.build_plan([case["id"]], cases)]
            if any(item["kind"] != "ui" for item in chain):
                required = ", ".join(item["id"] for item in chain if item["kind"] != "ui")
                raise ValueError("前置接口用例不能省略；独立页面脚本尚不支持这些前置：" + required)
            assets = _upload_assets(chain, Path(asset_root) if asset_root is not None else projects.path(project_id), config)
        except (ValueError, KeyError) as error:
            stem = re.sub(r"[^a-z0-9_.-]", "_", case["id"].lower())
            for engine in ("playwright", "selenium"):
                previous = folder / f"{stem}_{engine}.py"
                if previous.is_file():
                    previous.unlink()
            notes.append(f"{case['id']} 未生成脚本：{error}")
            continue
        for engine in ("playwright", "selenium"):
            stem = re.sub(r"[^a-z0-9_.-]", "_", case["id"].lower())
            file = folder / f"{stem}_{engine}.py"
            try:
                source = script(chain, base_url, engine, config)
                for name, original in assets:
                    destination = (folder / "assets" / name).resolve()
                    root = (folder / "assets").resolve()
                    if not root.is_relative_to(folder.resolve()) or not destination.is_relative_to(root):
                        raise ValueError("导出资源超出 assets 目录")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if original != destination:
                        shutil.copy2(original, destination)
                    copied.add("assets/" + name)
                file.write_text(source, encoding="utf-8", newline="\n")
            except (ValueError, OSError) as error:
                # A previously generated file must not keep claiming that this case exports.
                if file.is_file():
                    file.unlink()
                notes.append(f"{case['id']} 未生成 {engine} 脚本：{error}")
                continue
            written.append(file.name)
    return {"folder": str(folder), "scripts": written, "assets": sorted(copied), "notes": notes,
            "run": "python <脚本>；需要已安装 playwright 或 selenium，凭据通过同名环境变量提供；上传 assets/ 随脚本移动"}
