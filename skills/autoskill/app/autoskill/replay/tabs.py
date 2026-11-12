"""Stable recording tab ids, shared by both engines and standalone exports."""
import time


class Tabs:
    @staticmethod
    def destination(url):
        # Popup destinations are hints, never credentials or one-time login parameters.
        import re
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url)
        sensitive = r"token|secret|password|passwd|authorization|api.?key|credential|session|signature|^(code|key|ticket|state)$"
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not re.search(sensitive, k, re.IGNORECASE)]
        return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, urlencode(query), ''))

    def __init__(self, handles, activate, create, close, url, ready, current):
        self.handles, self.activate, self.create = handles, activate, create
        self.close, self.url, self.ready = close, url, ready
        self.known = {"t1": current}
        self.current = current

    def select(self, tab, timeout):
        handle = self.known.get(tab)
        if handle is None or handle not in self.handles():
            raise ValueError("标签页不存在或已关闭：" + tab)
        self.activate(handle)
        self.current = handle
        self.ready(handle, timeout)
        return handle

    def action(self, step, timeout):
        op, tab = step["action"], step.get("tab")
        if op == "tab_new":
            if tab in self.known:
                raise ValueError("标签页编号不能重复使用：" + tab)
            self.known[tab] = self.create(step["url"])
            return self.select(tab, timeout)
        if op == "tab_close":
            handle = self.known.get(tab)
            if handle is None or handle not in self.handles():
                raise ValueError("标签页不存在或已关闭：" + tab)
            if step["active"] == tab or len(self.handles()) <= 1:
                raise ValueError("不能关闭最后一个标签页；请结束或取消录制")
            self.close(handle)
            return self.select(step["active"], timeout)
        if op == "tab_switch":
            return self.select(tab, timeout)
        return self.current

    def sync(self, change, timeout):
        if not change:
            return self.current
        deadline = time.monotonic() + timeout
        pending = list(change.get("opened", []))
        closed = change.get("closed", [])
        if any(tab not in self.known for tab in closed):
            raise ValueError("要关闭的标签页从未打开")
        while True:
            live = self.handles()
            unknown = [h for h in live if h not in self.known.values()]
            for item in pending[:]:
                # Use the recorded destination to disambiguate simultaneous popups.
                matches = [h for h in unknown if item.get("url") and self.destination(self.url(h)) == self.destination(item["url"])]
                if len(matches) == 1:
                    handle = matches[0]
                elif len(unknown) == 1 and len(pending) == 1:
                    handle = unknown[0]
                else:
                    continue
                if item["id"] in self.known:
                    raise ValueError("标签页编号不能重复使用：" + item["id"])
                self.known[item["id"]] = handle
                pending.remove(item)
                unknown.remove(handle)
            gone = all(self.known.get(tab) not in live for tab in closed)
            if not pending and gone:
                return self.select(change["active"], max(0.1, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                raise TimeoutError("等待新标签页打开或关闭超时；没有切到其他页面代替")
            # Playwright needs its event loop pumped while waiting for popup events.
            self.ready(None, 0.05)


def playwright_tabs(page):
    context = page.context
    for other in context.pages:
        if other != page:
            other.close()

    def create(url):
        child = context.new_page()
        child.goto(url, wait_until="domcontentloaded", timeout=90000)
        return child

    def ready(handle, timeout):
        if handle is None:
            live = context.pages
            if live:
                live[0].wait_for_timeout(timeout * 1000)
            else:
                time.sleep(timeout)
        else:
            handle.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)

    return Tabs(lambda: context.pages, lambda h: h.bring_to_front(), create,
                lambda h: h.close(), lambda h: h.url, ready, page)


def selenium_tabs(driver):
    handles = driver.window_handles
    if not handles:
        raise ValueError("所有标签页都已关闭")
    try:
        current = driver.current_window_handle
    except Exception:  # noqa: BLE001  # Closed-window failures vary by browser adapter; retain the existing fallback.
        current = handles[0]
    for handle in handles:
        if handle != current:
            driver.switch_to.window(handle)
            driver.close()
    driver.switch_to.window(current)

    def create(url):
        driver.switch_to.new_window("tab")
        driver.get(url)
        return driver.current_window_handle

    def close(handle):
        driver.switch_to.window(handle)
        driver.close()

    def url(handle):
        try:
            old = driver.current_window_handle
        except Exception:  # noqa: BLE001  # A closing window can fail through different driver exception types.
            old = None
        driver.switch_to.window(handle)
        value = driver.current_url
        if old in driver.window_handles:
            driver.switch_to.window(old)
        return value

    def ready(handle, timeout):
        if handle is None:
            time.sleep(timeout)
            return
        deadline = time.monotonic() + timeout
        while driver.execute_script("return document.readyState") == "loading":
            if time.monotonic() >= deadline:
                raise TimeoutError("标签页加载超时")
            time.sleep(0.05)

    def activate(handle):
        return driver.switch_to.window(handle)

    return Tabs(lambda: driver.window_handles, activate, create,
                close, url, ready, current)
