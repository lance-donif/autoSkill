"""Export UI cases as standalone scripts that need only Playwright or Selenium, not autoSkill.

Each script runs its prerequisites (for example the login) first, in the same
browser. Secrets are read from environment variables of the same name.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path

from .. import projects, runner
from . import ir, ui
from .playwright_driver import PlaywrightAction
from .tabs import Tabs, playwright_tabs, selenium_tabs

SECRET = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")


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


def tab_step_expr(step: dict) -> str:
    return "{" + ", ".join(lit(k) + ": " + (url_expr(v) if k == "url" else lit(v))
                           for k, v in step.items() if k in ("action", "tab", "active", "url")) + "}"


def tab_change_expr(change: dict) -> str:
    opened = ["{" + f"'id': {lit(item['id'])}, 'url': {url_expr(item['url']) if item.get('url') else repr('')}" + "}"
              for item in change.get("opened", [])]
    return "{" + f"'active': {lit(change['active'])}, 'closed': {lit(change.get('closed', []))}, 'opened': [" + ", ".join(opened) + "]}"


def pw_step(step: dict) -> str:
    if "action" in step:
        op = step["action"]
        if op.startswith("tab_"):
            return f"page = tabs.action({tab_step_expr(step)}, TIMEOUT)"
        if op == "goto":
            return f"page.goto({url_expr(step['url'])})"
        if op == "reload":
            return "page.reload()"
        return _action_call(step, "page")
    return _pw_assertion(step)


def _pw_assertion(step: dict) -> str:
    """Use the same poller as replay, including zero counts and temporal expectations."""
    return _assertion_call(step, "page")


def _assertion_call(step: dict, browser: str) -> str:
    definition = _step_definition(step)
    if step.get("transition_from") and ir.locators(step["transition_from"]):
        definition["transition_from"] = _step_definition(step["transition_from"])
    return f"verify(AssertionDriver({browser}), {lit(definition)})"


def _step_definition(step: dict) -> dict:
    """Keep runtime data intact while normalizing legacy locator aliases exactly once."""
    definition = {key: value for key, value in step.items() if key not in ("recording_result", "selector", "placeholder", "role")}
    definition.update(name=step.get("name") or ir.describe(step), locators=ir.locators(step))
    return definition


def _action_call(step: dict, browser: str) -> str:
    return f"execute(AssertionDriver({browser}), {lit(_step_definition(step))})"


def se_step(step: dict) -> str:
    if "assertion" in step:
        return _assertion_call(step, "driver")
    op = step["action"]
    if op.startswith("tab_"):
        return f"tabs.action({tab_step_expr(step)}, TIMEOUT)"
    if op == "goto":
        return f"driver.get({url_expr(step['url'])})"
    if op == "reload":
        return "driver.refresh()"
    return _action_call(step, "driver")


PW_HEADER = r'''import os
import time
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", @@BASE@@).rstrip("/")
TIMEOUT = float(os.environ.get("TIMEOUT", "90"))
RAND = os.environ.get("RAND") or os.urandom(3).hex()  # unique text for the data this run creates
'''
PW_MAIN = '''

def main():
    with sync_playwright() as p:
        options = {"headless": os.environ.get("HEADED") == "0"}
        if os.environ.get("CHROME_BINARY"):
            options["executable_path"] = os.environ["CHROME_BINARY"]
        browser = p.chromium.launch(**options)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        context.on("dialog", lambda dialog: dialog.accept())
        context.set_default_timeout(TIMEOUT * 1000)
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
from urllib.parse import urljoin, urlsplit

from selenium import webdriver

BASE_URL = os.environ.get("BASE_URL", @@BASE@@).rstrip("/")
TIMEOUT = float(os.environ.get("TIMEOUT", "10"))
RAND = os.environ.get("RAND") or os.urandom(3).hex()  # unique text for the data this run creates
'''
SE_MAIN = '''

def main():
    options = webdriver.ChromeOptions()
    if os.environ.get("HEADED") == "0":
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


class _StandaloneSource(ast.NodeTransformer):
    """Bind shared runtime imports to the definitions embedded in an exported script."""

    def visit_ImportFrom(self, node):
        return None if node.level else node

    def visit_Name(self, node):
        if node.id == "TIMEOUT":
            return ast.copy_location(ast.Name(id="STEP_TIMEOUT", ctx=node.ctx), node)
        return node


def _standalone_source(value) -> str:
    tree = ast.parse(textwrap.dedent(inspect.getsource(value)))
    return ast.unparse(_StandaloneSource().visit(tree))


def _runtime_support(engine: str) -> str:
    """Embed the same locators, actions and checks without requiring an autoSkill installation."""
    from . import playwright_driver, selenium_driver

    module = playwright_driver if engine == "playwright" else selenium_driver
    driver = module.PlaywrightDriver if engine == "playwright" else module.SeleniumDriver
    field = "page" if engine == "playwright" else "wd"
    support = '''
class _IR:
    expand = staticmethod(lambda value: value)
    describe = staticmethod(lambda step: step.get("name", step.get("assertion", "断言")))
    describe_locator = staticmethod(str)


class _Secrets:
    resolve = staticmethod(lambda value: value)


class _Captcha:
    solve = staticmethod(lambda _image: input("请输入验证码："))


ir, secrets, captcha = _IR(), _Secrets(), _Captcha()
POLL, PRIMARY_GRACE, LOCATOR, STEP_TIMEOUT = 0.1, 1.5, "locator_not_found", "timeout"
'''
    for function in (ir.locators, ir.absolute_url, ir.check_origin):
        support += "\n\n" + _standalone_source(function) + f"\nir.{function.__name__} = {function.__name__}"
    for function in (ui.StepError, ui.check_cancel, ui._recorded_field, ui.choose_locator, ui._same, ui.evaluate, ui._prepare_assertion, ui._read_assertion,
                     ui._poll_assertion, ui.check, ui.navigate, ui.perform):
        support += "\n\n" + _standalone_source(function)
    for name, value in vars(module).items():
        if name.isupper() and isinstance(value, (str, int, float, dict, list, tuple)):
            support += f"\n\n{name} = {value!r}"
    support += (f"\n\nclass AssertionDriver:\n    name = {engine!r}\n\n    def __init__(self, browser):\n"
                f"        self.{field} = browser\n        self.timeout_ms = int(TIMEOUT * 1000)\n")
    public = {"count", "read", "url", "title", "image", "act", "press"}
    for name in driver.__dict__:
        if name in public or (name.startswith("_") and not name.startswith("__")):
            method = getattr(driver, name)
            if callable(method):
                support += "\n\n" + textwrap.indent(_standalone_source(method), "    ")
    support += '''

def execute(driver, step):
    fallback = perform(driver, step, BASE_URL, [], TIMEOUT)
    if fallback:
        print("已使用备选定位")
    return fallback


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
    flows = [*[stage["automation"] for stage in automation.get("setup_cases", [])], automation]
    body = [f"def {name}({arg}):", *["    # " + line for line in f"{case['id']} {case['title']}".splitlines()],
            f"    tabs = {factory}({arg})", f"    {opener}({url_expr(flows[0]['start_url'])})"]
    for step in (step for flow in flows for step in [*flow.get("actions", []), *flow.get("assertions", [])]):
        body.extend("    # " + line for line in ir.describe(step).splitlines())
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
    notes = [f"{chain[-1]['id']} {chain[-1]['title']}", "",
             "由 autoSkill 根据录制生成的独立回放脚本；运行它不需要 autoSkill。",
             "浏览器默认显示，设置 HEADED=0 可隐藏。可用环境变量：BASE_URL、CHROME_BINARY、TIMEOUT" + ("、CHROMEDRIVER" if engine == "selenium" else "") + "。"]
    notes += ["动作与断言使用共享的定位候选、XPath、iframe 与开放 Shadow DOM 上下文规则。",
              "断言使用与内置回放相同的轮询、来源状态和持续后置条件语义。",
              "差异：独立脚本遇到首个失败立即停止；内置回放会继续收集业务断言。",
              "验证码需要人工输入；登录态复用、运行历史和截图由内置执行器管理，不随脚本导出。"]
    if wanted:
        notes.append("运行前需要设置凭据环境变量：" + "、".join(wanted) + "。")
    calls = "\n".join(f"{pad}{'page = ' if engine == 'playwright' else ''}{name}({arg})" for name, _ in functions)
    tab_support = inspect.getsource(Tabs) + "\n\n" + inspect.getsource(playwright_tabs if engine == "playwright" else selenium_tabs)
    if engine == "playwright":
        tab_support += "\n\n" + inspect.getsource(PlaywrightAction)
    return (repr("\n".join(notes)) + "\n" + header.replace("@@BASE@@", json.dumps(base_url)) + "\n\n"
            + tab_support + "\n\n" + _runtime_support(engine) + "\n\n" + "\n\n\n".join(source for _, source in functions)
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
