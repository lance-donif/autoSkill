"""Local record -> compile -> two-engine replay -> standalone capability coverage.

The fixture intentionally exposes observable results for events and a real CRUD API.
No external website, model, package installer, or live account is needed.
"""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/browser-capabilities"
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
os.environ.setdefault("AUTOSKILL_HEADED", "0")
from autoskill import paths, projects, secrets  # noqa: E402
from autoskill.recorder import agent, session  # noqa: E402
from autoskill.replay import codegen, ir, ui  # noqa: E402
from autoskill.replay.playwright_driver import PlaywrightDriver  # noqa: E402
from autoskill.replay.selenium_driver import SeleniumDriver  # noqa: E402

DOWNLOAD = b"autoSkill capability download\n"


class CapabilityServer(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), CapabilitySite)
        self.lock = threading.RLock()
        self.items: dict[str, dict] = {}
        self.next_id = 1
        self.requests: list[tuple[str, str]] = []


class CapabilitySite(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass

    def send(self, status, data, mime="application/json", **headers):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def api(self, method):
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            return False
        with self.server.lock:
            self.server.requests.append((method, path))
            if path == "/api/items" and method == "GET":
                self.send(200, {"items": list(self.server.items.values()), "total": len(self.server.items)})
                return True
            if path == "/api/items" and method == "POST":
                body = self.body()
                if not isinstance(body.get("title"), str) or not body["title"].strip():
                    self.send(422, {"error": "title required"})
                    return True
                item_id = f"ITEM-{self.server.next_id:04d}"
                self.server.next_id += 1
                item = {"id": item_id, "title": body["title"], "status": "created"}
                self.server.items[item_id] = item
                self.send(201, item)
                return True
            item_id = path.removeprefix("/api/items/")
            item = self.server.items.get(item_id)
            if path.startswith("/api/items/") and item:
                if method == "GET":
                    self.send(200, item)
                elif method == "PATCH":
                    body = self.body()
                    item.update({key: body[key] for key in ("title", "status") if key in body})
                    self.send(200, item)
                elif method == "DELETE":
                    del self.server.items[item_id]
                    self.send(200, {"deleted": True, "id": item_id})
                else:
                    self.send(405, {"error": "method not allowed"})
                return True
            self.send(404, {"error": "item missing"})
            return True

    def do_GET(self):
        if self.api("GET"):
            return
        path = urlsplit(self.path).path
        if path == "/download.txt":
            self.send(200, DOWNLOAD, "text/plain", Content_Disposition='attachment; filename="capability.txt"')
            return
        if path == "/network-probe":
            self.send(200, {"source": "server", "header": self.headers.get("X-Capability", "")})
            return
        relative = path.lstrip("/") or "index.html"
        file = (FIXTURE / relative).resolve()
        if not file.is_relative_to(FIXTURE.resolve()) or not file.is_file():
            self.send(404, {"error": "file missing"})
            return
        self.send(200, file.read_bytes(), mimetypes.guess_type(str(file))[0] or "application/octet-stream")

    def do_POST(self):
        if not self.api("POST"):
            self.send(404, {"error": "not found"})

    def do_PATCH(self):
        self.api("PATCH")

    def do_DELETE(self):
        self.api("DELETE")


@unittest.skipUnless(paths.agent_browser() and paths.chrome() and paths.chromedriver(), "complete local browser runtime required")
class BrowserCapabilitiesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = CapabilityServer()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.saved_env = {key: os.environ.get(key) for key in ("AUTOSKILL_WORKSPACE", "AUTOSKILL_HEADED")}
        os.environ["AUTOSKILL_WORKSPACE"] = str(Path(self.tmp.name) / "workspace")
        os.environ["AUTOSKILL_HEADED"] = "0"
        self.pid = projects.create("Browser capability lab", self.base, project_id="capability-lab")["id"]
        with self.server.lock:
            self.server.items.clear()
            self.server.requests.clear()

    def tearDown(self):
        try:
            session.cancel()
        finally:
            for key, value in self.saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.tmp.cleanup()

    def begin(self):
        result = session.start(self.pid, login=False)
        self.assertIn("Browser capability lab", result["snapshot"])

    def browser(self, *argv):
        return session.browser(list(argv))

    def check(self, kind, target, expected=None, **options):
        return session.check(kind, target, expected, **options)

    def finish(self, title):
        result = session.stop(title)
        case = next(case for case in projects.cases(self.pid) if case["id"] == result["ui_case"]["id"])
        ir.validate(case["extensions"]["automation"])
        return case, result

    def replay(self, case, exports=True, timeout=4):
        root = Path(self.tmp.name)
        for engine, driver_type in (("playwright", PlaywrightDriver), ("selenium", SeleniumDriver)):
            with self.subTest(engine=engine, phase="replay"):
                config = {**projects.config(self.pid), "page_load_seconds": 8, "timeout_seconds": timeout,
                          "_asset_root": str(projects.path(self.pid)), "_output_root": str(root / "private" / engine)}
                driver = driver_type(config, headless=True)
                try:
                    driver.reset()
                    result = ui.run_case(driver, case, config, root / "evidence", engine + ".png")
                    self.assertEqual(result["status"], "Pass", json.dumps(result, ensure_ascii=False, indent=2))
                finally:
                    driver.close()
            if exports:
                with self.subTest(engine=engine, phase="standalone"):
                    source = codegen.script([case], self.base, engine, config=config)
                    self.assertNotIn("from autoskill", source)
                    file = root / f"capability_{engine}.py"
                    file.write_text(source, encoding="utf-8")
                    self.run_standalone(file, projects.path(self.pid), timeout=timeout)

    def run_standalone(self, file, assets=None, timeout=8):
        env = {**paths.child_env(), "CHROME_BINARY": paths.chrome(), "CHROMEDRIVER": paths.chromedriver(), "HEADED": "0",
               "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1", "TIMEOUT": str(timeout),
               "DOWNLOAD_ROOT": str(Path(self.tmp.name) / "standalone-downloads" / file.stem)}
        if assets is not None:
            env["PROJECT_ASSETS"] = str(assets)
        for name in secrets.names():
            env[name] = secrets.get_secret(name)
        done = subprocess.run([sys.executable, str(file)], cwd=str(file.parent), env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=100)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("PASS", done.stdout)

    def center(self, selector):
        script = "JSON.stringify((() => {const b=document.querySelector(" + json.dumps(selector) + ").getBoundingClientRect();return [b.x+b.width/2,b.y+b.height/2]})())"
        value = agent.call(session.session_name(), ["eval", "--stdin"], stdin_text=script)["result"]
        return [str(round(number, 2)) for number in json.loads(value)]

    def test_input_keyboard_pointer_waits_navigation_record_replay_export(self):
        self.begin()
        self.browser("fill", "#note", "start")
        self.browser("type", "#note", "-typed")
        self.check("value", "#note", "start-typed")
        self.browser("focus", "#note")
        self.check("text", "#focus-result", "note focused")
        self.browser("keyboard", "type", "-keyboard")
        self.browser("keyboard", "inserttext", "-insert")
        self.browser("keyboard", "down", "Shift")
        self.browser("keyboard", "press", "ArrowLeft")
        self.check("text", "#key-result", "ArrowLeft:true")
        self.browser("keyboard", "up", "Shift")
        self.browser("press", "End")
        self.check("value", "#note", "start-typed-keyboard-insert")
        self.browser("select", "#choice", "two")
        self.check("value", "#choice", "two")
        self.browser("check", "#checked")
        self.check("checked", "#checked", "true")
        self.browser("uncheck", "#checked")
        self.check("checked", "#checked", "false")
        self.browser("hover", "#hover-target")
        self.check("text", "#hover-result", "hovered")
        x, y = self.center("#mouse-target")
        self.browser("mouse", "move", x, y)
        self.browser("keyboard", "down", "Shift")
        self.browser("mouse", "down", "left")
        self.check("text", "#mouse-result", "down:0")
        self.check("text", "#mouse-modifier", "true")
        self.browser("mouse", "up", "left")
        self.check("text", "#mouse-result", "up:0")
        self.check("text", "#mouse-modifier", "true")
        self.browser("keyboard", "up", "Shift")
        self.browser("scroll", "down", "100", "--selector", "#scroll-box")
        self.check("text", "#scroll-result", "0,100")
        x, y = self.center("#scroll-box")
        self.browser("mouse", "move", x, y)
        self.browser("mouse", "wheel", "100", "0")
        self.browser("wait", "150")
        self.check("text", "#scroll-result", "0,200")
        self.browser("scrollintoview", "#scroll-end")
        self.browser("click", "#reveal")
        self.browser("wait", "#delayed", "--state", "visible")
        self.check("text", "#delayed", "Ready later")
        self.browser("wait", "#dynamic", "--state", "attached")
        self.browser("click", "#remove-dynamic")
        self.browser("wait", "#dynamic", "--state", "detached")
        self.browser("wait", "#dynamic", "--state", "hidden")
        self.browser("eval", "() => document.querySelector('#script-result').textContent = 'script executed'")
        self.check("text", "#script-result", "script executed")
        self.browser("click", "#second-page")
        self.check("title", None, "Second capability page")
        self.browser("back")
        self.check("title", None, "autoSkill browser capability lab")
        self.browser("forward")
        self.check("title", None, "Second capability page")
        self.browser("reload")
        self.check("visible", "#second-note")
        case, _ = self.finish("Inputs, keyboard, pointer, waits and navigation")
        actions = {step.get("action") for step in case["extensions"]["automation"]["actions"]}
        self.assertTrue({"focus", "keyboard", "mouse", "scroll", "scroll_into_view", "wait", "eval", "back", "forward"} <= actions)
        self.replay(case)

    def test_nested_frames_reset_to_main_on_tab_switch_and_export(self):
        self.begin()
        self.browser("frame", "#outer-frame")
        self.browser("fill", "#outer-note", "outer data")
        self.check("value", "#outer-note", "outer data")
        self.browser("frame", "#inner-frame")
        self.browser("fill", "#inner-note", "nested data")
        self.assertEqual(self.browser("get", "value", "#inner-note")["result"]["value"], "nested data")
        ref = next(key for key, item in session.current()["refs"].items() if item.get("selector") == "#inner-note")
        self.assertEqual(self.browser("get", "value", "@" + ref)["result"]["value"], "nested data")
        self.assertTrue(self.browser("is", "visible", "#inner-note")["result"]["visible"])
        self.browser("wait", "#inner-note", "--state", "visible")
        self.browser("scroll", "down", "50", "--selector", "#inner-scroll")
        self.check("text", "#inner-scroll-result", "50")
        steps_before_unsupported = len(session.current()["steps"])
        with self.assertRaises((ValueError, session.RecordError)):
            self.browser("find", "role", "button", "click", "--name", "Confirm choice")
        self.assertEqual(len(session.current()["steps"]), steps_before_unsupported)
        untouched = agent.call(session.session_name(), ["eval", "--stdin"],
                               stdin_text="document.querySelector('#dialog-result').textContent")["result"]
        self.assertEqual(untouched, "no dialog", "An unsupported iframe command must never click a main-frame button")
        self.browser("click", "#inner-save")
        self.check("text", "#inner-result", "nested data")
        self.browser("tab", "new", "/second.html")
        self.browser("fill", "#second-note", "new tab data")
        self.check("value", "#second-note", "new tab data")
        self.browser("tab", "t1")
        self.browser("fill", "#note", "main after tab")
        self.check("value", "#note", "main after tab")
        self.browser("frame", "#outer-frame")
        self.browser("frame", "#inner-frame")
        self.check("text", "#inner-result", "nested data")
        self.browser("frame", "main")
        self.browser("tab", "close", "t2")
        self.check("visible", "#note")
        self.replay(self.finish("Nested frames and tabs")[0])

    def test_dialog_accept_dismiss_prompt_and_password_never_persist_plain(self):
        self.begin()
        self.browser("dialog", "accept")
        self.browser("click", "#confirm")
        self.check("text", "#dialog-result", "accepted")
        self.browser("dialog", "dismiss")
        self.browser("click", "#confirm")
        self.check("text", "#dialog-result", "dismissed")
        secrets.set_secret("CAPABILITY_PROMPT", "prompt-value-5564")
        self.browser("dialog", "accept", "secret:CAPABILITY_PROMPT")
        self.browser("click", "#prompt")
        self.check("text", "#dialog-result", "${ENV:CAPABILITY_PROMPT}")
        self.browser("dialog", "dismiss")
        self.browser("click", "#prompt")
        self.check("text", "#dialog-result", "prompt dismissed")
        self.browser("click", "#alert")
        self.check("text", "#dialog-result", "alert accepted")
        self.browser("fill", "#password", "typed-private-7712")
        self.browser("keyboard", "type", "-private-suffix-9933")
        case, _ = self.finish("Explicit dialog policies and private inputs")
        persisted = json.dumps(case)
        for private in ("prompt-value-5564", "typed-private-7712", "-private-suffix-9933"):
            self.assertNotIn(private, persisted)
        self.replay(case)

    def test_html_drag_and_drop_is_replayed_in_both_engines_and_exports(self):
        self.begin()
        self.browser("drag", "#drag-source", "#drop-target")
        self.check("text", "#drop-result", "dropped")
        self.replay(self.finish("HTML drag and drop")[0])

    def test_long_stream_wait_overrides_short_default_and_requires_stable_completion(self):
        projects.update(self.pid, timeout_seconds=1)
        self.begin()
        self.browser("click", "#start-stream")
        self.check("text", "#stream-first", "first token", timeout_seconds=1)
        self.browser("wait", "#stream-done", "--timeout", "10000", "--stable-for", "250")
        self.check("text", "#stream-output", "answer complete", timeout_seconds=2, stable_for_seconds=0.1)
        case, _ = self.finish("Long AI stream waits for stable final completion")
        automation = case["extensions"]["automation"]
        step = next(step for step in automation["actions"] if step.get("action") == "wait")
        self.assertEqual(step["timeout_seconds"], 10)
        self.assertEqual(step["stable_for_seconds"], 0.25)
        self.assertEqual(automation["assertions"][-1]["timeout_seconds"], 2)
        self.replay(case, timeout=1)

    def test_storage_cookies_settings_clipboard_replay_and_export(self):
        self.begin()
        self.browser("storage", "local", "set", "lab_local", "local value")
        self.browser("storage", "session", "set", "lab_session", "session value")
        self.browser("cookies", "set", "lab_cookie", "cookie-value", "--same-site", "Strict")
        self.browser("eval", "() => document.querySelector('#script-result').textContent = localStorage.getItem('lab_local') + '|' + sessionStorage.getItem('lab_session') + '|' + document.cookie")
        self.check("text", "#script-result", "local value|session value|lab_cookie=cookie-value")
        self.browser("set", "viewport", "1280", "800")
        self.browser("eval", "() => document.querySelector('#script-result').textContent = innerWidth + 'x' + innerHeight")
        self.check("text", "#script-result", "1280x800")
        self.browser("set", "media", "dark", "reduced-motion")
        self.browser("eval", "() => document.querySelector('#script-result').textContent = matchMedia('(prefers-color-scheme: dark)').matches + '|' + matchMedia('(prefers-reduced-motion: reduce)').matches")
        self.check("text", "#script-result", "true|true")
        self.browser("set", "headers", '{"X-Capability":"header-value"}')
        self.browser("eval", "async () => { const data = await fetch('/network-probe').then(r => r.json()); document.querySelector('#script-result').textContent = data.header; }")
        self.check("text", "#script-result", "header-value")
        self.browser("set", "offline", "on")
        self.assertIs(agent.call(session.session_name(), ["eval", "--stdin"], stdin_text="navigator.onLine")["result"], False,
                      "Offline state must survive the adapter's CDP disconnect")
        self.browser("eval", "() => document.querySelector('#script-result').textContent = String(navigator.onLine)")
        self.check("text", "#script-result", "false")
        self.browser("set", "offline", "off")
        self.browser("eval", "() => document.querySelector('#script-result').textContent = String(navigator.onLine)")
        self.check("text", "#script-result", "true")
        secrets.set_secret("CAPABILITY_CLIPBOARD", "clipboard-private-1129")
        self.browser("clipboard", "write", "secret:CAPABILITY_CLIPBOARD")
        self.browser("focus", "#note")
        self.browser("keyboard", "press", "Control+v")
        self.check("value", "#note", "${ENV:CAPABILITY_CLIPBOARD}")
        self.browser("storage", "local", "clear")
        self.browser("storage", "session", "clear")
        self.browser("cookies", "clear")
        self.browser("eval", "() => document.querySelector('#script-result').textContent = localStorage.length + '|' + sessionStorage.length + '|' + document.cookie")
        self.check("text", "#script-result", "0|0|")
        case, _ = self.finish("Portable storage, cookies, settings and clipboard")
        self.assertTrue(ir.is_replayable(case))
        self.assertNotIn("clipboard-private-1129", json.dumps(case))
        self.replay(case)

    def test_geolocation_record_replay_and_exports(self):
        self.begin()
        self.browser("set", "geo", "37.7749", "-122.4194")
        observed = agent.call(session.session_name(), ["eval", "--stdin"], stdin_text=
                              "new Promise(resolve => navigator.geolocation.getCurrentPosition(p => resolve([p.coords.latitude,p.coords.longitude]), "
                              "e => resolve({code:e.code,message:e.message}), {timeout:3000}))")["result"]
        self.assertEqual(observed, [37.7749, -122.4194], "Geolocation permission and override must survive CDP disconnect")
        self.browser("eval", "async () => { const p = await new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject)); document.querySelector('#script-result').textContent = p.coords.latitude + ',' + p.coords.longitude; }")
        self.check("text", "#script-result", "37.7749,-122.4194")
        self.replay(self.finish("Geolocation override is usable by the page")[0])

    def test_upload_stages_assets_download_stays_private_and_export_moves(self):
        original = Path(self.tmp.name) / "external-input.txt"
        original.write_text("portable upload payload", encoding="utf-8")
        self.begin()
        self.browser("upload", "#upload", str(original))
        self.check("contains", "#upload-result", "portable upload payload")
        first_download = self.browser("download", "#download", "nested/result.txt")["artifacts"][0]
        second_download = self.browser("download", "#download", "nested/result.txt")["artifacts"][0]
        self.assertNotEqual(first_download["path"], second_download["path"], "Repeated downloads must retain both outputs")
        for item in (first_download, second_download):
            self.assertEqual((paths.local() / item["path"]).read_bytes(), DOWNLOAD)
        case, _ = self.finish("Portable upload and private download")
        automation = case["extensions"]["automation"]
        upload = next(step for step in automation["actions"] if step.get("action") == "upload")
        downloads = [step for step in automation["actions"] if step.get("action") == "download"]
        self.assertEqual(len(downloads), 2)
        self.assertEqual(downloads[0]["filename"], "nested/result.txt")
        self.assertNotIn(str(original), json.dumps(case))
        for name in upload["files"]:
            self.assertFalse(Path(name).is_absolute())
            self.assertEqual((projects.path(self.pid) / name).read_text(encoding="utf-8"), "portable upload payload")
        original.unlink()
        self.replay(case, exports=False)
        for engine in ("playwright", "selenium"):
            files = list((Path(self.tmp.name) / "private" / engine / "nested").glob("result*.txt"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(file.read_bytes() == DOWNLOAD for file in files))
        folder = Path(self.tmp.name) / "generated"
        result = codegen.export(self.pid, str(folder))
        self.assertEqual(len(result["scripts"]), 2, result)
        relocated = Path(self.tmp.name) / "moved-exports"
        folder.rename(relocated)
        for name in upload["files"]:
            (projects.path(self.pid) / name).unlink()
        for name in result["scripts"]:
            self.run_standalone(relocated / name)
            files = list((Path(self.tmp.name) / "standalone-downloads" / Path(name).stem / "nested").glob("result*.txt"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(file.read_bytes() == DOWNLOAD for file in files))

    def test_create_read_update_delete_closes_loop_in_record_replay_exports(self):
        self.begin()
        self.browser("fill", "#item-title", "created-{{RAND}}")
        self.browser("click", "#create-item")
        self.check("text", "#crud-result", "created:created-{{RAND}}")
        self.check("text", "#crud-count", "1")
        self.browser("click", "#read-item")
        self.check("text", "#crud-result", "read:created-{{RAND}}")
        self.browser("fill", "#item-title", "changed-{{RAND}}")
        self.browser("click", "#update-item")
        self.check("text", "#crud-result", "updated:changed-{{RAND}}")
        self.check("contains", "#items", "changed-{{RAND}}")
        self.browser("dialog", "dismiss")
        self.browser("click", "#delete-item")
        self.check("text", "#crud-result", "delete cancelled")
        self.check("text", "#crud-count", "1")
        self.browser("dialog", "accept")
        self.browser("click", "#delete-item")
        self.check("text", "#crud-result", "deleted")
        self.check("text", "#crud-count", "0")
        case, summary = self.finish("CRUD create, read, update, cancel delete and delete")
        self.assertTrue(summary["api_cases"], summary)
        methods = {case.get("method") for case in projects.cases(self.pid) if case["kind"] == "api"}
        self.assertTrue({"POST", "GET", "PATCH", "DELETE"} <= methods)
        self.replay(case)
        with self.server.lock:
            self.assertEqual(self.server.items, {})
            operations = self.server.requests[:]
        self.assertEqual(sum(method == "POST" for method, _ in operations), 5)
        self.assertEqual(sum(method == "PATCH" for method, _ in operations), 5)
        self.assertEqual(sum(method == "DELETE" for method, _ in operations), 5)

    def test_native_captures_are_private_observations_with_real_output(self):
        self.begin()
        captured = []
        for argv in (("screenshot", "lab.png"), ("pdf", "lab.pdf")):
            result = self.browser(*argv)
            self.assertEqual(len(session.current()["steps"]), 0, result)
            captured.extend(result["artifacts"])
        self.browser("trace", "start")
        self.browser("click", "#hover-target")
        captured.extend(self.browser("trace", "stop", "lab-trace.json")["artifacts"])
        self.browser("profiler", "start")
        self.browser("fill", "#note", "profiled input")
        captured.extend(self.browser("profiler", "stop", "lab-profile.json")["artifacts"])
        self.browser("storage", "local", "set", "lab_persist", "saved-state-value")
        self.browser("cookies", "set", "lab_saved", "saved-state-cookie")
        captured.extend(self.browser("state", "save", "lab-state.json")["artifacts"])
        self.browser("storage", "local", "set", "lab_persist", "changed-state-value")
        self.browser("cookies", "clear")
        if shutil.which("ffmpeg"):
            started = self.browser("record", "start", "lab.webm", "--fps", "5")
            self.assertIn("artifact_pending", started["result"])
            self.browser("fill", "#note", "video input")
            self.browser("wait", "250")
            captured.extend(self.browser("record", "stop")["artifacts"])
        self.check("visible", "#note")
        case, _ = self.finish("Private diagnostic captures around business actions")
        actions = {step.get("action") for step in case["extensions"]["automation"]["actions"]}
        self.assertFalse(actions & {"screenshot", "pdf", "trace", "profiler", "record", "state", "native_step"})
        kinds = {item["kind"] for item in captured}
        self.assertTrue({"screenshot", "pdf", "trace", "profiler", "state"} <= kinds, captured)
        if shutil.which("ffmpeg"):
            self.assertIn("video", kinds)
        for item in captured:
            with self.subTest(kind=item["kind"]):
                self.assertFalse(Path(item["path"]).is_absolute())
                file = paths.local() / item["path"]
                self.assertTrue(file.is_file(), item)
                self.assertGreater(file.stat().st_size, 0)
                self.assertNotIn(str(file), json.dumps(case))
                if item["kind"] == "screenshot":
                    self.assertTrue(file.read_bytes().startswith(b"\x89PNG"))
                if item["kind"] == "pdf":
                    self.assertTrue(file.read_bytes().startswith(b"%PDF"))
                if item["kind"] == "state":
                    state = json.loads(file.read_text(encoding="utf-8"))
                    self.assertTrue({"cookies", "origins"} <= set(state))
        saved_state = next(paths.local() / item["path"] for item in captured if item["kind"] == "state")
        self.browser("state", "load", str(saved_state))
        self.browser("open", self.base)
        observed = agent.call(session.session_name(), ["eval", "--stdin"], stdin_text=
                              "JSON.stringify({storage:localStorage.getItem('lab_persist'),cookie:document.cookie})")["result"]
        restored = json.loads(observed)
        self.assertEqual(restored["storage"], "saved-state-value")
        self.assertIn("lab_saved=saved-state-cookie", restored["cookie"])
        self.assertNotIn("saved-state-cookie", json.dumps(case))

    def test_native_lifecycle_guards_preserve_recording_and_unportable_mutation_blocks(self):
        from autoskill import runner

        self.begin()
        before = session.current()
        for argv in (("close",), ("connect", "9222"), ("--proxy", "http://127.0.0.1:1", "open", self.base),
                     ("network", "har", "start"), ("network", "har", "stop", "steal.har"), ("doctor", "--fix")):
            with self.subTest(argv=argv), self.assertRaises((ValueError, session.RecordError)):
                self.browser(*argv)
            self.assertEqual(session.current(), before)
        self.check("visible", "#note")
        result = self.browser("set", "credentials", "capability-user", "private-native-password-7109")
        self.assertFalse(result["replayable"], result)
        self.check("visible", "#note")
        case, _ = self.finish("Unsupported basic-auth mutation stays explicit")
        self.assertFalse(ir.is_replayable(case))
        self.assertNotIn("private-native-password-7109", json.dumps(case))
        self.assertIn("native_step", {step.get("action") for step in case["extensions"]["automation"]["actions"]})
        with self.server.lock:
            requests_before_replay = self.server.requests[:]
        for engine in ("playwright", "selenium"):
            queued = runner.create(self.pid, [case["id"]], engine)
            blocked = runner.execute(queued["id"])
            self.assertEqual(blocked["status"], "Blocked", blocked)
            self.assertEqual(blocked["blocked"], 1, blocked)
        with self.server.lock:
            self.assertEqual(self.server.requests, requests_before_replay, "A blocked case must not navigate or send business requests")
        generated = codegen.export(self.pid, str(Path(self.tmp.name) / "blocked-export"))
        self.assertEqual(generated["scripts"], [])
        self.assertTrue(generated["notes"], generated)

    def test_native_network_mock_executes_and_stays_explicitly_nonportable(self):
        self.begin()
        routed = self.browser("network", "route", self.base + "/network-probe", "--body", '{"source":"mocked"}')
        self.assertFalse(routed["replayable"], routed)
        script = "async () => { const data = await fetch('/network-probe').then(r => r.json()); document.querySelector('#script-result').textContent = data.source; }"
        self.browser("eval", script)
        self.check("text", "#script-result", "mocked")
        self.browser("network", "unroute", self.base + "/network-probe")
        self.browser("eval", script)
        self.check("text", "#script-result", "server")
        case, _ = self.finish("Native network mocks keep replay limitation explicit")
        self.assertFalse(ir.is_replayable(case))
        blockers = [step for step in case["extensions"]["automation"]["actions"] if step.get("action") == "native_step"]
        self.assertEqual(len(blockers), 2)
        self.assertTrue(all(step["command"] == "network" for step in blockers))
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                codegen.script([case], self.base, engine)


class CapabilityModelTests(unittest.TestCase):
    def test_nonportable_native_mutation_is_not_replayable_or_exportable(self):
        case = {"id": "NATIVE", "kind": "ui", "title": "Native state mutation", "extensions": {"automation": {
            "start_url": "/", "actions": [{"action": "native_step", "command": "network", "args": ["route", "*", "--abort"],
                                            "reason": "Network interception has no portable replay"}], "assertions": []}}}
        self.assertFalse(ir.is_replayable(case))
        class NeverNavigate:
            def goto(self, url):
                raise AssertionError("A nonportable case must block before navigation")

        with tempfile.TemporaryDirectory() as folder:
            outcome = ui.run_case(NeverNavigate(), case, {"base_url": "http://127.0.0.1:1"}, Path(folder), "unused.png")
        self.assertEqual(outcome["status"], "Blocked", outcome)
        self.assertEqual(outcome["category"], "unsupported_capability", outcome)
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                codegen.script([case], "http://127.0.0.1:1", engine)

    def test_upload_and_download_paths_reject_absolute_traversal_and_substitution(self):
        for name in ("../outside", "/tmp/outside", "C:/outside", r"..\outside", "a/../../outside", "a//b", "${ENV:UPLOAD}", "{{RAND}}"):
            for action, field, value in (("upload", "files", [name]), ("download", "filename", name)):
                step = {"action": action, "selector": "#files", field: value}
                with self.subTest(action=action, path=name), self.assertRaises(ValueError):
                    ir.validate({"start_url": "/", "actions": [step]})


if __name__ == "__main__":
    unittest.main()
