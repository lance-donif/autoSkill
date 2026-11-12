"""Engine-independent UI replay: one step loop, two browser drivers.

A driver only knows primitive operations on an already chosen locator. Picking
the locator (with fallback), polling assertions, timing, classifying failures
and building the result all happen here, so Playwright and Selenium runs of the
same case are judged by identical rules.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import paths, secrets
from . import ir

# Failure categories shown in the dashboard.
LOCATOR, ASSERTION, TIMEOUT, BLOCKED_ORIGIN, ENVIRONMENT, SECRET = (
    "locator_not_found", "assertion_failed", "timeout", "unauthorized_origin", "environment", "missing_secret",
)
POLL = 0.1
PRIMARY_GRACE = 1.5  # seconds the best locator gets before fallbacks are considered
WATCH_PAUSE = 0.3  # seconds between steps when a person is watching the browser window


class StepError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def choose_locator(driver, step: dict, timeout: float, need: bool = True):
    """Return (locator, index) of the first candidate that matches; index > 0 means a fallback was used."""
    candidates = ir.locators(step)
    if not candidates:
        return None, 0
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        pool = candidates[:1] if need and elapsed < PRIMARY_GRACE else candidates
        for index, locator in enumerate(pool):
            if driver.count(locator) > locator.get("nth", 0):
                return locator, index
        if not need or elapsed >= timeout:
            if need:
                raise StepError(LOCATOR, "页面上找不到：" + " / ".join(ir.describe_locator(c) for c in candidates))
            return candidates[0], 0
        time.sleep(POLL)


def _same(expected, actual) -> bool:
    return " ".join(str(expected).split()) == " ".join(str(actual).split())


def evaluate(driver, step: dict, locator) -> tuple[bool, object]:
    """One reading of an assertion: (passed, actual value)."""
    op, expected = step["assertion"], ir.expand(step.get("expected"))
    if op == "url":
        return driver.url() == expected, driver.url()
    if op == "url_contains":
        return str(expected) in driver.url(), driver.url()
    if op == "title":
        return _same(expected, driver.title()), driver.title()
    count = driver.count(locator)
    if op == "count":
        return count == expected, count
    if op == "min_count":
        return count >= expected, count
    if op == "hidden":
        visible = count > locator.get("nth", 0) and driver.read(locator, "visible")
        return not visible, "可见" if visible else "不可见"
    if count <= locator.get("nth", 0):
        return False, "未找到元素"
    if op == "visible":
        visible = driver.read(locator, "visible")
        return bool(visible), "可见" if visible else "不可见"
    if op == "checked":
        actual = driver.read(locator, "checked")
        return actual == bool(step.get("expected", True)), actual
    if op == "attribute":
        actual = driver.read(locator, "attribute", step["attribute"])
        wanted = str(expected)
        # class lists match when the expected token is present, as testers expect
        if step["attribute"] == "class" and actual is not None:
            return wanted in str(actual).split(), actual
        return actual is not None and str(actual) == wanted, actual
    actual = driver.read(locator, "value" if op == "value" else "text")
    if op == "contains":
        return str(expected) in str(actual), actual
    return _same(expected, actual), actual


def check(driver, step: dict, timeout: float) -> dict:
    """Poll an assertion until it holds or the timeout passes."""
    name = step.get("name") or ir.describe(step)
    deadline = time.monotonic() + timeout
    fallback = 0
    while True:
        try:
            locator, selected = choose_locator(driver, step, 0, need=False)
            fallback = max(fallback, selected)
            passed, actual = evaluate(driver, step, locator)
        except Exception as error:  # noqa: BLE001  # Engine-specific read failures are retried while the element changes.
            passed, actual = False, "读取失败：" + type(error).__name__
        if passed or time.monotonic() >= deadline:
            break
        time.sleep(POLL)
    row = {"name": name, "passed": bool(passed), "expected": step.get("expected", True), "actual": actual}
    if fallback:
        row["fallback"] = True
    return row


def navigate(action, *args) -> None:
    """Load a page; a load that runs out of time is reported as such, not as a missing element."""
    try:
        action(*args)
    except Exception as error:
        if "Timeout" in type(error).__name__:
            raise StepError(TIMEOUT, "页面没有在限定时间内加载完成；页面本身很慢时可以调大：autoskill project update <项目> --page-load <秒>") from None
        raise


def perform(driver, step: dict, base_url: str, allowed: list[str], timeout: float) -> bool:
    """Run one action. Returns True when a fallback locator had to be used."""
    op = step["action"]
    if op.startswith("tab_"):
        tab_step = dict(step)
        if op == "tab_new":
            tab_step["url"] = ir.absolute_url(step["url"], base_url)
            ir.check_origin(tab_step["url"], allowed)
        driver.tab_action(tab_step, timeout)
        return False
    if op == "goto":
        url = ir.absolute_url(step["url"], base_url)
        ir.check_origin(url, allowed)
        navigate(driver.goto, url)
        return False
    if op == "reload":
        navigate(driver.reload)
        return False
    need = op != "wait_hidden"
    locator, fallback = choose_locator(driver, step, timeout, need=need)
    if op == "press":
        driver.press(locator, step["key"])
    elif op in ("fill", "type", "select"):
        driver.act(locator, op, secrets.resolve(ir.expand(step["value"])))
    elif op == "captcha":
        from .. import captcha

        picture, _ = choose_locator(driver, {"locators": step["image"]}, timeout)
        driver.act(locator, "fill", captcha.solve(driver.image(picture)))
    elif op in ("wait_visible", "wait_hidden"):
        wanted = op == "wait_visible"
        deadline = time.monotonic() + timeout
        while True:
            locator, selected = choose_locator(driver, step, 0, need=False)
            fallback = max(fallback, selected)
            visible = driver.count(locator) > locator.get("nth", 0) and bool(driver.read(locator, "visible"))
            if visible == wanted:
                break
            if time.monotonic() >= deadline:
                raise StepError(TIMEOUT, f"等待超时：{ir.describe(step)}")
            time.sleep(POLL)
    else:
        driver.act(locator, op, None)
    return bool(fallback)


def classify(error: Exception) -> tuple[str, str]:
    """Map any exception to (category, message a tester can act on)."""
    if isinstance(error, StepError):
        return error.category, str(error)
    if isinstance(error, PermissionError):
        return BLOCKED_ORIGIN, str(error)
    if isinstance(error, ValueError):
        return (SECRET if "缺少凭据" in str(error) else ENVIRONMENT), str(error)
    name = type(error).__name__
    if "Timeout" in name:
        if "标签页" in str(error):
            return TIMEOUT, str(error)
        return TIMEOUT, "操作超时：元素没有在限定时间内变为可操作"
    return ENVIRONMENT, "浏览器执行出错：" + name


def run_case(driver, case: dict, config: dict, evidence_dir: Path, screenshot_name: str) -> dict:
    """Replay one UI case on an already opened page and return its result fields."""
    automation = case.get("extensions", {}).get("automation")
    timeout = float(config.get("timeout_seconds", 20))
    allowed = list(config.get("allowed_origins", []))
    base_url = config.get("base_url", "")
    steps, assertions = [], []
    outcome = {"status": "Blocked", "actual": "", "reason": "", "category": "", "fallbacks": 0}
    started = time.monotonic()
    acted = False
    try:
        ir.validate(automation)
        url = ir.absolute_url(automation["start_url"], base_url)
        ir.check_origin(url, allowed)
        row = {"index": 0, "text": "打开 " + url, "status": "running", "ms": 0}
        steps.append(row)
        clock = time.monotonic()
        navigate(driver.goto, url)
        row.update(status="done", ms=round((time.monotonic() - clock) * 1000))
        acted = True
        for index, step in enumerate(automation.get("actions", []), 1):
            row = {"index": index, "text": ir.describe(step), "status": "running", "ms": 0}
            steps.append(row)
            clock = time.monotonic()
            try:
                if "assertion" in step:
                    result = check(driver, step, min(timeout, 5.0))
                    assertions.append(result)
                    outcome["fallbacks"] += int(bool(result.get("fallback")))
                    row["status"] = "done" if result["passed"] else "failed"
                    row["check"] = True
                    continue
                if perform(driver, step, base_url, allowed, timeout):
                    row["fallback"] = True
                    outcome["fallbacks"] += 1
                if step.get("tabs"):
                    change = {**step["tabs"], "opened": [
                        {**item, "url": ir.absolute_url(item["url"], base_url)} if item.get("url") else item
                        for item in step["tabs"].get("opened", [])]}
                    driver.sync_tabs(change, float(config.get("page_load_seconds", 90)))
                ir.check_origin(driver.url(), allowed)
                row["status"] = "done"
            finally:
                row["ms"] = round((time.monotonic() - clock) * 1000)
                if paths.headed():
                    time.sleep(WATCH_PAUSE)
        for step in automation.get("assertions", []):
            result = check(driver, step, min(timeout, 5.0))
            outcome["fallbacks"] += int(bool(result.get("fallback")))
            assertions.append(result)
        if not assertions:
            assertions.append({"name": "全部动作可以完成（未声明断言）", "passed": True, "expected": True, "actual": True})
        failed = [a for a in assertions if not a["passed"]]
        if failed:
            outcome.update(status="Fail", category=ASSERTION, actual="页面结果与预期不一致",
                           reason="；".join(f'{a["name"]}（实际：{a["actual"]}）' for a in failed)[:500])
        else:
            done = sum(1 for row in steps if not row.get("check"))
            outcome.update(status="Pass", actual=f"{done} 个步骤完成，{len(assertions)} 条断言通过")
    except Exception as error:  # noqa: BLE001  # Every engine failure must become the existing structured case result.
        category, message = classify(error)
        if steps and steps[-1]["status"] == "running":
            steps[-1].update(status="failed", error=message)
        if acted and category in (LOCATOR, TIMEOUT, ENVIRONMENT):
            assertions.append({"name": "全部动作可以完成", "passed": False, "expected": True, "actual": message})
            outcome.update(status="Fail", actual="动作未能完成", reason=message, category=category)
        else:
            assertions = []
            outcome.update(status="Blocked", reason=message, category=category)
    evidence = []
    if acted:
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            driver.screenshot(evidence_dir / screenshot_name)
            evidence.append(screenshot_name)
        except Exception:  # noqa: BLE001, S110  # Screenshot evidence is optional and must not change the case result.
            pass
    outcome.update(steps=steps, assertions=assertions, screenshots=evidence,
                   duration_ms=round((time.monotonic() - started) * 1000))
    return outcome
