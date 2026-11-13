"""Export UI cases as standalone scripts that need only Playwright or Selenium, not autoSkill.

Each script runs its prerequisites (for example the login) first, in the same
browser. Secrets are read from environment variables of the same name.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from .. import projects, runner
from . import ir, ui
from .playwright_driver import PlaywrightAction
from .tabs import Tabs, playwright_tabs, selenium_tabs

SECRET = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
KEYS = {"Enter": "ENTER", "Escape": "ESCAPE", "Tab": "TAB", "Backspace": "BACKSPACE", "Delete": "DELETE", "Space": "SPACE"}


def lit(value) -> str:
    """A Python literal; ${ENV:NAME} becomes os.environ["NAME"]."""
    if isinstance(value, dict):
        return "{" + ", ".join(repr(key) + ": " + lit(item) for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(lit(item) for item in value) + "]"
    if not isinstance(value, str):
        return repr(value)
    if ir.RAND in value:
        return " + RAND + ".join(lit(part) for part in value.split(ir.RAND))
    parts, last = [], 0
    for match in SECRET.finditer(value):
        if match.start() > last:
            parts.append(json.dumps(value[last:match.start()], ensure_ascii=False))
        parts.append(f'os.environ["{match[1]}"]')
        last = match.end()
    if last < len(value) or not parts:
        parts.append(json.dumps(value[last:], ensure_ascii=False))
    return " + ".join(parts)


def url_expr(url: str) -> str:
    return lit(url) if "://" in url else f"BASE_URL + {lit(url if url.startswith('/') else '/' + url)}"


def pw_locator(locator: dict) -> str:
    kind, value = locator["by"], locator.get("value")
    if kind == "testid":
        code = f"page.get_by_test_id({lit(value)})"
    elif kind == "role":
        name = f", name={lit(locator['name'])}, exact=True" if locator.get("name") else ""
        code = f"page.get_by_role({lit(locator['role'])}{name})"
    elif kind in ("label", "placeholder", "text"):
        code = f"page.get_by_{kind}({lit(value)}, exact=True)"
    else:
        code = f"page.locator({lit(value)})"
    return code + (f".nth({locator['nth']})" if locator.get("nth") else "")


def css_for(step: dict) -> tuple[str, int]:
    """Selenium scripts use plain CSS: the recorded CSS candidate, or one derived from a test id or placeholder."""
    candidates = ir.locators(step)
    for locator in candidates:
        if locator["by"] == "css":
            return locator["value"], locator.get("nth", 0)
    for locator in candidates:
        if locator["by"] in ("testid", "placeholder"):
            attribute = "data-testid" if locator["by"] == "testid" else "placeholder"
            return f'[{attribute}="{locator["value"]}"]', locator.get("nth", 0)
    raise ValueError("这一步没有可用于 Selenium 独立脚本的 CSS 定位：" + ir.describe(step))


def tab_step_expr(step: dict) -> str:
    return "{" + ", ".join(lit(k) + ": " + (url_expr(v) if k == "url" else lit(v))
                           for k, v in step.items() if k in ("action", "tab", "active", "url")) + "}"


def tab_change_expr(change: dict) -> str:
    opened = ["{" + f"'id': {lit(item['id'])}, 'url': {url_expr(item['url']) if item.get('url') else repr('')}" + "}"
              for item in change.get("opened", [])]
    return "{" + f"'active': {lit(change['active'])}, 'closed': {lit(change.get('closed', []))}, 'opened': [" + ", ".join(opened) + "]}"


def pw_step(step: dict) -> str:
    target = pw_locator(ir.locators(step)[0]) if ir.locators(step) else None
    if "action" in step:
        op = step["action"]
        if op.startswith("tab_"):
            return f"page = tabs.action({tab_step_expr(step)}, TIMEOUT)"
        if op == "goto":
            return f"page.goto({url_expr(step['url'])})"
        if op == "reload":
            return "page.reload()"
        if op == "press":
            return f"{target}.press({lit(step['key'])})" if target else f"page.keyboard.press({lit(step['key'])})"
        if op == "fill":
            return f"{target}.fill({lit(step['value'])})"
        if op == "type":
            return f"{target}.press_sequentially({lit(step['value'])})"
        if op == "select":
            return f"{target}.select_option({lit(step['value'])})"
        if op == "captcha":  # a standalone script has no captcha model: a person types it
            return f'{target}.fill(input("请输入验证码："))'
        if op in ("wait_visible", "wait_hidden"):
            return f"{target}.wait_for(state={lit(op[5:])})"
        return f"{target}.{op}()"
    return _pw_assertion(step)


def _pw_assertion(step: dict) -> str:
    """Use the same poller as replay, including zero counts and temporal expectations."""
    return _assertion_call(step, "page")


def _assertion_call(step: dict, browser: str) -> str:
    definition = {key: value for key, value in step.items() if key != "recording_result"}
    definition.update(name=step.get("name") or ir.describe(step), locators=ir.locators(step))
    if step.get("transition_from") and ir.locators(step["transition_from"]):
        definition["transition_from"] = {**step["transition_from"], "locators": ir.locators(step["transition_from"])}
    return f"verify(AssertionDriver({browser}), {lit(definition)})"


KEY_NAMES = {**KEYS, "Control": "CONTROL", "Shift": "SHIFT", "Alt": "ALT", "Meta": "META", "ArrowUp": "ARROW_UP",
             "ArrowDown": "ARROW_DOWN", "ArrowLeft": "ARROW_LEFT", "ArrowRight": "ARROW_RIGHT", "Home": "HOME", "End": "END"}


def key_expr(key: str) -> str:
    return ", ".join(f"Keys.{KEY_NAMES[part]}" if part in KEY_NAMES else lit(part) for part in key.split("+"))


def se_step(step: dict) -> str:
    if "assertion" in step:
        return _assertion_call(step, "driver")
    has_target = bool(ir.locators(step))
    css, nth = css_for(step) if has_target else ("", 0)
    at = f"{lit(css)}, {nth}"
    message = json.dumps(ir.describe(step), ensure_ascii=False)  # never expands secrets
    op = step["action"]
    if op.startswith("tab_"):
        return f"tabs.action({tab_step_expr(step)}, TIMEOUT)"
    if op == "goto":
        return f"driver.get({url_expr(step['url'])})"
    if op == "reload":
        return "driver.refresh()"
    if op == "press":
        keys = key_expr(step["key"])
        return f"find(driver, {at}).send_keys({keys})" if has_target else f"ActionChains(driver).send_keys({keys}).perform()"
    if op == "fill":
        return f"fill(driver, {at}, {lit(step['value'])})"
    if op == "type":
        return f"find(driver, {at}).send_keys({lit(step['value'])})"
    if op == "select":
        return f"Select(find(driver, {at})).select_by_value({lit(step['value'])})"
    if op == "click":
        return f"find(driver, {at}).click()"
    if op == "dblclick":
        return f"ActionChains(driver).double_click(find(driver, {at})).perform()"
    if op == "hover":
        return f"ActionChains(driver).move_to_element(find(driver, {at})).perform()"
    if op in ("check", "uncheck"):
        return f"set_checked(driver, {at}, {op == 'check'})"
    if op == "captcha":
        return f'fill(driver, {at}, input("请输入验证码："))'
    return f"wait_until(lambda: shown(driver, {at}) is {op == 'wait_visible'}, {message})"


PW_HEADER = r'''import os
import re
import time

from playwright.sync_api import expect, sync_playwright

BASE_URL = os.environ.get("BASE_URL", @@BASE@@).rstrip("/")
CLASS_L, CLASS_R = r"(^|\s)", r"(\s|$)"
TIMEOUT = float(os.environ.get("TIMEOUT", "90"))
RAND = os.environ.get("RAND") or os.urandom(3).hex()  # unique text for the data this run creates
'''
PW_MAIN = '''

def main():
    with sync_playwright() as p:
        options = {"headless": os.environ.get("HEADED") != "1"}
        if os.environ.get("CHROME_BINARY"):
            options["executable_path"] = os.environ["CHROME_BINARY"]
        browser = p.chromium.launch(**options)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        context.on("dialog", lambda dialog: dialog.accept())
        page = context.new_page()
        try:
@@CALLS@@
        finally:
            browser.close()
    print("PASS @@ID@@")


if __name__ == "__main__":
    main()
'''
SE_HEADER = r'''import os
import time

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

BASE_URL = os.environ.get("BASE_URL", @@BASE@@).rstrip("/")
TIMEOUT = float(os.environ.get("TIMEOUT", "10"))
RAND = os.environ.get("RAND") or os.urandom(3).hex()  # unique text for the data this run creates


def wait_until(test, message):
    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            if test():
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.1)


def find(driver, css, nth=0):
    found = []

    def ready():
        found[:] = driver.find_elements(By.CSS_SELECTOR, css)
        return len(found) > nth

    try:
        wait_until(ready, "找不到元素 " + css)
    except AssertionError as error:
        # Keep the actual page identity when a navigation or window selection failed.
        try:
            detail = ("；当前页面：" + Tabs.destination(driver.current_url)
                      + "；标题：" + driver.title
                      + "；加载状态：" + str(driver.execute_script("return document.readyState")))
        except Exception as diagnostic:
            detail = "；页面诊断不可用：" + type(diagnostic).__name__
        raise AssertionError(str(error) + detail) from None
    return found[nth]


def shown(driver, css, nth=0):
    found = driver.find_elements(By.CSS_SELECTOR, css)
    return len(found) > nth and found[nth].is_displayed()


def read(driver, css, nth, what):
    element = driver.find_elements(By.CSS_SELECTOR, css)[nth]
    if what == "text":
        return driver.execute_script("return arguments[0].innerText ?? arguments[0].textContent ?? '';", element)
    if what == "value":
        return element.get_property("value")
    if what == "checked":
        return element.is_selected()
    return element.get_dom_attribute(what[5:])


def norm(text):
    return " ".join(str(text).split())


def fill(driver, css, nth, text):
    element = find(driver, css, nth)
    driver.execute_script("arguments[0].focus(); if (arguments[0].select) arguments[0].select();", element)
    element.send_keys(text if text else Keys.DELETE)


def set_checked(driver, css, nth, wanted):
    element = find(driver, css, nth)
    if element.is_selected() != wanted:
        element.click()
'''
SE_MAIN = '''

def main():
    options = webdriver.ChromeOptions()
    if os.environ.get("HEADED") != "1":
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.set_capability("unhandledPromptBehavior", "accept")
    if os.environ.get("CHROME_BINARY"):
        options.binary_location = os.environ["CHROME_BINARY"]
    if os.environ.get("CHROMEDRIVER"):
        service = webdriver.ChromeService(executable_path=os.environ["CHROMEDRIVER"])
        driver = webdriver.Chrome(options=options, service=service)
    else:
        driver = webdriver.Chrome(options=options)
    try:
@@CALLS@@
    finally:
        driver.quit()
    print("PASS @@ID@@")


if __name__ == "__main__":
    main()
'''


def _assertion_support(engine: str) -> str:
    """Embed the shared checks and primitive reads without importing autoSkill in the script."""
    from .playwright_driver import PlaywrightDriver
    from .selenium_driver import FIND, SeleniumDriver

    driver = PlaywrightDriver if engine == "playwright" else SeleniumDriver
    field = "page" if engine == "playwright" else "wd"
    support = '''
class _IR:
    expand = staticmethod(lambda value: value)
    locators = staticmethod(lambda step: step.get("locators", []))
    describe = staticmethod(lambda step: step.get("name", step.get("assertion", "断言")))
    describe_locator = staticmethod(str)


class _Secrets:
    resolve = staticmethod(lambda value: value)


ir, secrets = _IR(), _Secrets()
POLL, PRIMARY_GRACE, LOCATOR = 0.1, 1.5, "locator_not_found"
'''
    support += "\n\n" + inspect.getsource(ui.StepError)
    for function in (ui.choose_locator, ui._same, ui.evaluate, ui._prepare_assertion, ui._read_assertion, ui._poll_assertion, ui.check):
        support += "\n\n" + inspect.getsource(function)
    support += "\n\nFIND = " + repr(FIND if engine == "selenium" else "")
    support += f"\n\nclass AssertionDriver:\n    def __init__(self, browser):\n        self.{field} = browser\n\n"
    support += "    def _one(self, locator):\n        return self._all(locator).nth(locator.get('nth', 0))\n" if engine == "playwright" else (
        "    def _one(self, locator):\n        return self._all(locator)[locator.get('nth', 0)]\n")
    for method in ("_all", "count", "read", "url", "title"):
        support += "\n" + inspect.getsource(getattr(driver, method))
    support += '''

def verify(driver, step):
    result = check(driver, step, TIMEOUT)
    if not result["passed"]:
        raise AssertionError(f"{result['name']}：期望 {result['expected']!r}，实际 {result['actual']!r}；"
                             f"阶段 {result['phase']}，已等待 {result['elapsed_ms']} ms")
    return result
'''
    return support


def _function(case: dict, engine: str) -> tuple[str, str]:
    """(function name, source) replaying one case on an already open browser."""
    automation = case["extensions"]["automation"]
    name = re.sub(r"[^a-z0-9]+", "_", case["id"].lower()).strip("_")
    arg, opener, emit = ("page", "page.goto", pw_step) if engine == "playwright" else ("driver", "driver.get", se_step)
    factory = "playwright_tabs" if engine == "playwright" else "selenium_tabs"
    body = [f"def {name}({arg}):", f"    # {case['id']} {case['title']}",
            f"    tabs = {factory}({arg})", f"    {opener}({url_expr(automation['start_url'])})"]
    for step in [*automation.get("actions", []), *automation.get("assertions", [])]:
        body.append(f"    # {ir.describe(step)}")
        if engine == "playwright" and step.get("action") and step.get("tabs", {}).get("closed"):
            body += [f"    with PlaywrightAction(page, tabs, {tab_change_expr(step['tabs'])}):", f"        {emit(step)}"]
        else:
            body.append(f"    {emit(step)}")
        if step.get("tabs"):
            prefix = "page = " if engine == "playwright" else ""
            body += [f"    {prefix}tabs.sync({tab_change_expr(step['tabs'])}, TIMEOUT)"]
    if engine == "playwright":
        body.append("    return page")
    return name, "\n".join(body)


def script(chain: list[dict], base_url: str, engine: str) -> str:
    """A complete standalone script: prerequisites first, then the case itself."""
    header, main, arg, pad = (PW_HEADER, PW_MAIN, "page", " " * 12) if engine == "playwright" else (SE_HEADER, SE_MAIN, "driver", " " * 8)
    functions = [_function(case, engine) for case in chain]
    wanted = sorted({name for case in chain for name in SECRET.findall(json.dumps(case, ensure_ascii=False))})
    notes = ['"""' + f"{chain[-1]['id']} {chain[-1]['title']}", "",
             "由 autoSkill 根据录制生成的独立回放脚本；运行它不需要 autoSkill。",
             "可用环境变量：BASE_URL、HEADED=1、CHROME_BINARY" + ("、CHROMEDRIVER、TIMEOUT" if engine == "selenium" else "") + "。"]
    notes += ["断言使用与内置回放相同的轮询、来源状态和持续后置条件语义。",
              "差异：独立脚本遇到首个失败立即停止；内置回放会继续收集业务断言。动作定位只使用导出时选定的候选。",
              "验证码需要人工输入；登录态复用、运行历史和截图由内置执行器管理，不随脚本导出。"]
    if wanted:
        notes.append("运行前需要设置凭据环境变量：" + "、".join(wanted) + "。")
    calls = "\n".join(f"{pad}{'page = ' if engine == 'playwright' else ''}{name}({arg})" for name, _ in functions)
    tab_support = inspect.getsource(Tabs) + "\n\n" + inspect.getsource(playwright_tabs if engine == "playwright" else selenium_tabs)
    if engine == "playwright":
        tab_support += "\n\n" + inspect.getsource(PlaywrightAction)
    return ("\n".join(notes) + '\n"""\n' + header.replace("@@BASE@@", json.dumps(base_url)) + "\n\n"
            + tab_support + "\n\n" + _assertion_support(engine) + "\n\n" + "\n\n\n".join(source for _, source in functions)
            + main.replace("@@CALLS@@", calls).replace("@@ID@@", chain[-1]["id"]))


def export(project_id: str, out: str | None = None) -> dict:
    cases = {c["id"]: c for c in projects.cases(project_id)}
    base_url = projects.config(project_id)["base_url"]
    folder = Path(out) if out else projects.path(project_id) / "scripts"
    folder.mkdir(parents=True, exist_ok=True)
    written, notes = [], []
    for case in cases.values():
        if case["kind"] != "ui" or not ir.is_replayable(case):
            continue
        chain = [cases[cid] for cid, _ in runner.build_plan([case["id"]], cases)]
        if any(c["kind"] != "ui" for c in chain):
            notes.append(f"{case['id']} 的前置里有接口用例，独立脚本只包含页面步骤")
            chain = [c for c in chain if c["kind"] == "ui"]
        for engine in ("playwright", "selenium"):
            try:
                source = script(chain, base_url, engine)
            except ValueError as error:
                notes.append(f"{case['id']} 未生成 {engine} 脚本：{error}")
                continue
            file = folder / f"{case['id'].lower()}_{engine}.py"
            file.write_text(source, encoding="utf-8", newline="\n")
            written.append(file.name)
    return {"folder": str(folder), "scripts": written, "notes": notes,
            "run": "python <脚本>；需要本机已安装 playwright 或 selenium，凭据通过同名环境变量提供"}
