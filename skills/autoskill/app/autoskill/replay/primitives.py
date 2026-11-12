"""Portable replay helpers, also embedded in standalone exports.

Keep helpers self contained: exports copy their source and must not import
autoSkill. CDP traffic is restricted to Chrome's existing local debug socket.
"""
from __future__ import annotations


def project_asset(root, name):
    """Resolve an existing project-relative upload without escaping its root."""
    from pathlib import Path, PureWindowsPath

    if root is None:
        raise ValueError("上传文件缺少项目资源目录")
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise ValueError("上传文件必须使用项目相对路径")
    relative = Path(name)
    if (relative.is_absolute() or PureWindowsPath(name).drive or any(part in ("", ".", "..") or ":" in part for part in name.split("/"))
            or "${" in name or "{{" in name):
        raise ValueError("上传文件必须使用项目相对路径")
    base = Path(root).resolve()
    destination = (base / relative).resolve()
    if not destination.is_relative_to(base) or not destination.is_file():
        raise ValueError("上传资源不存在或超出项目目录")
    return destination


def output_file(root, name):
    """Resolve a private run output; recorded data may never name a host path."""
    from pathlib import Path, PureWindowsPath

    if root is None:
        raise ValueError("下载文件缺少运行输出目录")
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise ValueError("下载文件名必须使用输出目录相对路径")
    relative = Path(name)
    if (relative.is_absolute() or PureWindowsPath(name).drive or any(part in ("", ".", "..") or ":" in part for part in name.split("/"))
            or "${" in name or "{{" in name):
        raise ValueError("下载文件名必须使用输出目录相对路径")
    base = Path(root).resolve()
    destination = (base / relative).resolve()
    if not destination.is_relative_to(base):
        raise ValueError("下载文件名超出运行输出目录")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    original, index = destination, 1
    while destination.exists() or destination.is_symlink():
        destination = original.with_name(f"{original.stem} ({index}){original.suffix}")
        if not destination.resolve().is_relative_to(base):
            raise ValueError("下载文件名超出运行输出目录")
        index += 1
    return destination


def scroll_delta(direction, pixels):
    import math

    if direction not in ("up", "down", "left", "right"):
        raise ValueError("滚动方向必须是 up/down/left/right")
    if isinstance(pixels, bool) or not isinstance(pixels, (int, float)) or not math.isfinite(pixels) or pixels < 0:
        raise ValueError("滚动像素必须是非负有限数值")
    return {"up": (0, -pixels), "down": (0, pixels), "left": (-pixels, 0), "right": (pixels, 0)}[direction]


def eval_expression(script):
    """Evaluate an expression or zero-argument function once, awaiting promises."""
    import json

    if not isinstance(script, str) or not script.strip():
        raise ValueError("eval 缺少 JavaScript 表达式或函数")
    source = "(" + script.rstrip().removesuffix(";") + "\n)"
    return ("(async () => { const value = (0, eval)(" + json.dumps(source) + "); "
            "return typeof value === 'function' ? await value() : await value; })()")


class DialogPolicy:
    """One explicit next-dialog choice, falling back to the existing accept policy."""

    def __init__(self):
        from threading import Lock

        self._lock = Lock()
        self._pending = None

    def arm(self, mode, text=None):
        if mode not in ("accept", "dismiss") or (text is not None and not isinstance(text, str)):
            raise ValueError("dialog 需要 accept/dismiss 和可选文本")
        with self._lock:
            if self._pending is not None:
                raise ValueError("上一个 dialog 选择尚未被页面对话框使用")
            self._pending = (mode, text)

    def take(self):
        with self._lock:
            choice = self._pending or ("accept", None)
            self._pending = None
            return choice

    def pending(self):
        with self._lock:
            return self._pending is not None


def normalize_browser_command(command, args):
    """Validate the deliberately small cross-engine mutation vocabulary."""
    import json
    import math

    if not isinstance(command, str) or not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError("browser_command 需要 command 字符串和 args 字符串数组")

    def number(text):
        try:
            result = float(text)
        except (TypeError, ValueError):
            raise ValueError("浏览器设置需要有限数值") from None
        if not math.isfinite(result):
            raise ValueError("浏览器设置需要有限数值")
        return result

    if command == "storage" and len(args) >= 2 and args[0] in ("local", "session"):
        if args[1] == "set" and len(args) == 4:
            return {"kind": "storage", "storage": args[0], "operation": "set", "key": args[2], "value": args[3]}
        if args[1] == "clear" and len(args) == 2:
            return {"kind": "storage", "storage": args[0], "operation": "clear"}
    if command == "cookies" and args:
        if args == ["clear"]:
            return {"kind": "cookies", "operation": "clear"}
        if args[0] == "set" and len(args) >= 3:
            result = {"kind": "cookies", "operation": "set", "name": args[1], "value": args[2]}
            flags = {"--http-only": "httpOnly", "--httpOnly": "httpOnly", "--secure": "secure", "--same-site": "sameSite", "--sameSite": "sameSite",
                     "--url": "url", "--domain": "domain", "--path": "path", "--expires": "expires"}
            index = 3
            while index < len(args):
                key = flags.get(args[index])
                if not key or key in result:
                    raise ValueError("cookies set 包含不支持或重复的属性")
                if key in ("httpOnly", "secure"):
                    result[key] = True
                    index += 1
                else:
                    if index + 1 >= len(args):
                        raise ValueError("cookies set 属性缺少值")
                    result[key] = number(args[index + 1]) if key == "expires" else args[index + 1]
                    index += 2
            if result.get("sameSite", "Lax") not in ("Strict", "Lax", "None"):
                raise ValueError("cookie same-site 必须是 Strict/Lax/None")
            if "domain" in result and "url" in result:
                raise ValueError("cookie 不可同时指定 domain 与 url")
            if not result["name"] or ("path" in result and not result["path"].startswith("/")):
                raise ValueError("cookie 名称或路径无效")
            return result
    if command == "set" and args:
        setting, values = args[0], args[1:]
        if setting == "viewport" and len(values) in (2, 3):
            width, height = number(values[0]), number(values[1])
            scale = number(values[2]) if len(values) == 3 else 1.0
            if width <= 0 or height <= 0 or not width.is_integer() or not height.is_integer() or scale <= 0:
                raise ValueError("viewport 宽高需要正整数；缩放需要正数")
            return {"kind": "viewport", "width": int(width), "height": int(height), "scale": scale}
        if setting == "offline" and len(values) == 1 and values[0] in ("on", "off"):
            return {"kind": "offline", "offline": values[0] == "on"}
        if setting == "headers" and len(values) == 1:
            try:
                headers = json.loads(values[0])
            except (ValueError, TypeError):
                raise ValueError("headers 需要 JSON 对象") from None
            if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
                raise ValueError("headers 需要字符串键值的 JSON 对象")
            return {"kind": "headers", "headers": headers}
        if setting in ("geo", "geolocation") and len(values) == 2:
            latitude, longitude = map(number, values)
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError("地理位置经纬度超出范围")
            return {"kind": "geo", "latitude": latitude, "longitude": longitude}
        if setting == "media" and 1 <= len(values) <= 2 and values[0] in ("dark", "light", "no-preference"):
            if len(values) == 2 and values[1] not in ("reduced-motion", "no-preference"):
                raise ValueError("media 动态偏好不受支持")
            return {"kind": "media", "color_scheme": values[0],
                    "reduced_motion": "reduce" if len(values) == 2 and values[1] == "reduced-motion" else "no-preference"}
    raise ValueError("此浏览器命令没有可移植回放实现；请保留 native_step 的不支持原因")


def cookie_from_command(data, current_url):
    from urllib.parse import urlsplit

    cookie = {key: data[key] for key in ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires") if key in data}
    url = data.get("url", current_url)
    if "domain" not in cookie:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("cookie 需要有效 http(s) 地址或显式 domain")
        # Explicit domain/path keeps identical cookie scope in both engines.
        cookie["domain"] = parts.hostname
    cookie.setdefault("path", "/")
    return cookie


class PlaywrightCommands:
    """Portable mutations with context-wide state and future-tab emulation."""

    def __init__(self, context):
        self.context = context
        self._settings = {}
        self._sessions = {}
        context.on("page", self._on_page)

    def _on_page(self, page):
        if self._settings:
            session = self.context.new_cdp_session(page)
            self._sessions[page] = session
            for method, params in self._settings.items():
                session.send(method, params)

    def _set_pages(self, method, params):
        self._settings[method] = params
        for page in self.context.pages:
            if page.is_closed():
                continue
            session = self._sessions.get(page)
            if session is None:
                session = self.context.new_cdp_session(page)
                self._sessions[page] = session
            session.send(method, params)

    def execute(self, command, args, page, scope=None):
        data = normalize_browser_command(command, args)
        kind = data["kind"]
        if kind == "storage":
            (scope or page).evaluate("d => { const s = d.storage === 'local' ? localStorage : sessionStorage; "
                                     "if (d.operation === 'clear') s.clear(); else s.setItem(d.key, d.value); }", data)
        elif kind == "cookies":
            if data["operation"] == "clear":
                self.context.clear_cookies()
            else:
                self.context.add_cookies([cookie_from_command(data, page.url)])
        elif kind == "headers":
            self.context.set_extra_http_headers(data["headers"])
        elif kind == "offline":
            self.context.set_offline(data["offline"])
        elif kind == "geo":
            self.context.grant_permissions(["geolocation"])
            self.context.set_geolocation({"latitude": data["latitude"], "longitude": data["longitude"], "accuracy": 1})
        elif kind == "viewport":
            self._set_pages("Emulation.setDeviceMetricsOverride", {"width": data["width"], "height": data["height"],
                                                                  "deviceScaleFactor": data["scale"], "mobile": False})
        elif kind == "media":
            self._set_pages("Emulation.setEmulatedMedia", {"features": [
                {"name": "prefers-color-scheme", "value": data["color_scheme"]},
                {"name": "prefers-reduced-motion", "value": data["reduced_motion"]}]})


class SeleniumCommands:
    """The same mutation vocabulary, using the existing Chrome CDP channel."""

    def __init__(self, driver, events):
        self.driver, self.events = driver, events

    def execute(self, command, args):
        data = normalize_browser_command(command, args)
        kind = data["kind"]
        if kind == "storage":
            self.driver.execute_script("const d=arguments[0], s=d.storage==='local'?localStorage:sessionStorage; "
                                       "if(d.operation==='clear')s.clear();else s.setItem(d.key,d.value);", data)
        elif kind == "cookies":
            if data["operation"] == "clear":
                self.driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
            else:
                result = self.driver.execute_cdp_cmd("Network.setCookie", cookie_from_command(data, self.driver.current_url))
                if not result.get("success", False):
                    raise ValueError("Chrome 未能设置 cookie")
        elif kind == "headers":
            self.events.set_page_command("Network.enable", {})
            self.events.set_page_command("Network.setExtraHTTPHeaders", {"headers": data["headers"]})
        elif kind == "offline":
            self.events.set_page_command("Network.enable", {})
            self.events.set_page_command("Network.emulateNetworkConditions", {
                "offline": data["offline"], "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
        elif kind == "geo":
            self.events.grant_permissions(["geolocation"])
            self.events.set_page_command("Emulation.setGeolocationOverride", {
                "latitude": data["latitude"], "longitude": data["longitude"], "accuracy": 1})
        elif kind == "viewport":
            self.events.set_page_command("Emulation.setDeviceMetricsOverride", {
                "width": data["width"], "height": data["height"], "deviceScaleFactor": data["scale"], "mobile": False})
        elif kind == "media":
            self.events.set_page_command("Emulation.setEmulatedMedia", {"features": [
                {"name": "prefers-color-scheme", "value": data["color_scheme"]},
                {"name": "prefers-reduced-motion", "value": data["reduced_motion"]}]})
        self.events.check()


class SeleniumDialogs:
    """Chrome CDP event channel for real dialogs and completed downloads.

    WebDriver calls cannot answer a dialog while a triggering call is blocked.
    This independent channel prepares listeners for every current/future page
    before it runs. websocket-client is an existing Selenium dependency.
    """

    def __init__(self, driver, policy):
        import json
        import threading
        import urllib.request
        import websocket

        self._json = json
        self._threading = threading
        self.policy = policy
        self._send_lock = threading.Lock()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._next_id = 0
        self._pending = {}
        self._sessions = {}
        self._page_settings = {}
        self._permissions = set()
        self._closed = False
        self._failure = None
        self._download = None
        address = driver.capabilities.get("goog:chromeOptions", {}).get("debuggerAddress")
        if not address:
            raise ValueError("Selenium 对话框与下载需要 Chrome 的 CDP 调试地址")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://" + address + "/json/version", timeout=5) as response:
            endpoint = json.load(response)["webSocketDebuggerUrl"]
        self._ws = websocket.create_connection(endpoint, timeout=0.5, suppress_origin=True, enable_multithread=True)
        self._thread = threading.Thread(target=self._read, daemon=True, name="autoskill-chrome-events")
        self._thread.start()
        try:
            self.command("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
            targets = self.command("Target.getTargets").get("targetInfos", [])
            for target in targets:
                if target.get("type") == "page" and target["targetId"] not in self._sessions:
                    self.command("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
            # Acknowledge Page.enable before any user action can trigger a dialog.
            for session in self._session_ids():
                self.command("Page.enable", session=session)
        except Exception:
            self.close()
            raise

    def _session_ids(self):
        with self._lock:
            return list(self._sessions.values())

    def _send(self, method, params=None, session=None, wait=True):
        with self._send_lock:
            if self._closed:
                raise RuntimeError("Chrome 事件连接已关闭")
            self._next_id += 1
            identifier = self._next_id
            slot = {"event": self._threading.Event(), "response": None, "method": method, "wait": wait}
            with self._lock:
                self._pending[identifier] = slot
            payload = {"id": identifier, "method": method, "params": params or {}}
            if session:
                payload["sessionId"] = session
            self._ws.send(self._json.dumps(payload))
        return identifier, slot

    def command(self, method, params=None, session=None, timeout=10):
        identifier, slot = self._send(method, params, session)
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(identifier, None)
            raise TimeoutError("Chrome 事件命令超时：" + method)
        result = slot["response"]
        if result is None or "error" in result:
            raise RuntimeError("Chrome 事件命令失败：" + method)
        return result.get("result", {})

    def _fire(self, method, params=None, session=None):
        self._send(method, params, session, wait=False)

    def _read(self):
        import websocket

        try:
            while not self._closed:
                try:
                    raw = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                message = self._json.loads(raw)
                if "id" in message:
                    with self._lock:
                        slot = self._pending.pop(message["id"], None)
                    if slot:
                        slot["response"] = message
                        slot["event"].set()
                        if not slot["wait"] and "error" in message:
                            self._failure = "Chrome 事件命令失败：" + slot["method"]
                    continue
                self._event(message)
        except Exception as error:
            if not self._closed:
                self._failure = "Chrome 事件连接中断：" + type(error).__name__
        finally:
            if not self._closed and not self._failure:
                self._failure = "Chrome 事件连接意外关闭"
            with self._lock:
                for slot in self._pending.values():
                    slot["event"].set()
                self._pending.clear()
                self._changed.notify_all()

    def _event(self, message):
        method, params = message.get("method"), message.get("params", {})
        session = message.get("sessionId")
        if method == "Target.attachedToTarget":
            child = params["sessionId"]
            target = params["targetInfo"]
            if target.get("type") != "page":
                self._fire("Runtime.runIfWaitingForDebugger", session=child)
                return
            with self._lock:
                previous = self._sessions.get(target["targetId"])
                if not previous:
                    self._sessions[target["targetId"]] = child
                settings = list(self._page_settings.items())
            if previous and previous != child:
                self._fire("Target.detachFromTarget", {"sessionId": child})
                return
            self._fire("Page.enable", session=child)
            for setting, values in settings:
                self._fire(setting, values, session=child)
            self._fire("Runtime.runIfWaitingForDebugger", session=child)
        elif method == "Target.detachedFromTarget":
            with self._lock:
                self._sessions = {target: value for target, value in self._sessions.items() if value != params.get("sessionId")}
        elif method == "Page.javascriptDialogOpening":
            mode, text = self.policy.take()
            choice = {"accept": mode == "accept"}
            if text is not None and mode == "accept":
                choice["promptText"] = text
            self._fire("Page.handleJavaScriptDialog", choice, session=session)
        elif method in ("Browser.downloadWillBegin", "Browser.downloadProgress"):
            with self._changed:
                capture = self._download
                if capture is not None:
                    if method.endswith("WillBegin"):
                        if capture.get("guid") is not None:
                            capture["error"] = "一个 download 步骤触发了多个文件"
                        else:
                            capture["guid"] = params["guid"]
                    elif capture.get("guid") == params.get("guid"):
                        capture["state"] = params.get("state")
                    self._changed.notify_all()

    def check(self):
        if self._failure:
            raise RuntimeError(self._failure)

    def set_page_command(self, method, params):
        """Apply settings to all current pages and before future pages resume."""
        with self._lock:
            self._page_settings[method] = params
            sessions = list(self._sessions.values())
        for session in sessions:
            self.command(method, params, session=session)
        self.check()

    def grant_permissions(self, permissions):
        # Browser.grantPermissions replaces the origin's allowlist. Retain
        # earlier explicit grants when adding clipboard/geolocation/audio.
        with self._lock:
            self._permissions.update(permissions)
            combined = sorted(self._permissions)
        self.command("Browser.grantPermissions", {"permissions": combined})

    def download(self, trigger, destination, timeout):
        from pathlib import Path
        import tempfile
        import time

        destination = Path(destination)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=destination.parent) as staging:
            with self._changed:
                if self._download is not None:
                    raise ValueError("同时只能等待一个下载步骤")
                self._download = {}
            try:
                self.command("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": staging, "eventsEnabled": True})
                trigger()  # Never retry a click: the server may already have applied a mutation.
                deadline = time.monotonic() + timeout
                with self._changed:
                    while True:
                        self.check()
                        capture = self._download
                        if capture.get("error"):
                            raise ValueError(capture["error"])
                        if capture.get("state") == "canceled":
                            raise ValueError("浏览器取消了下载")
                        if capture.get("state") == "completed":
                            source = Path(staging) / capture["guid"]
                            if not source.is_file():
                                raise ValueError("浏览器报告下载完成，但文件不存在")
                            source.replace(destination)
                            return destination
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("等待下载完成超时")
                        self._changed.wait(min(remaining, 0.1))
            finally:
                with self._changed:
                    self._download = None
                self.command("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": False})

    def close(self):
        if self._closed:
            return
        try:
            self.command("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False, "flatten": True}, timeout=2)
        except Exception:
            pass
        self._closed = True
        self._ws.close()
        if self._thread is not self._threading.current_thread():
            self._thread.join(timeout=1)
