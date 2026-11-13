"""Real Chromium drives data and preference pages against isolated live Python APIs."""
import contextlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))
from autoskill import paths

SERVER = """
import json, socket, sys
import uvicorn
from autoskill.server.app import create_app
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(('127.0.0.1', int(sys.argv[1])))
listener.listen(128)
port = listener.getsockname()[1]
app = create_app(port=port, start_worker=False)
print(json.dumps({'port': port}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[listener])
"""


class DataServer:
    def __init__(self, root, port=0):
        self.root = root
        self.resources = contextlib.ExitStack()
        self.errors = self.resources.enter_context(tempfile.TemporaryFile(mode="w+t", encoding="utf-8"))  # noqa: SIM115  # ExitStack closes the owned subprocess log when this fixture stops.
        env = {**os.environ, "PYTHONPATH": str(APP), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
               "AUTOSKILL_WORKSPACE": str(root), "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}
        env.pop("AUTOSKILL_CASE_SKILL", None)
        self.process = subprocess.Popen([sys.executable, "-u", "-c", SERVER, str(port)], cwd=ROOT, env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self.errors,
                                        text=True, encoding="utf-8")
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(self.process.stdout.readline()), daemon=True).start()
        try:
            line = ready.get(timeout=20)
            self.port = json.loads(line)["port"]
            self.origin = f"http://127.0.0.1:{self.port}"
            for _ in range(100):
                try:
                    self.api("/status")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.02)
            else:
                raise RuntimeError("Data fixture did not start listening")
        except Exception:
            self.close()
            raise

    def api(self, path, method="GET", body=None):
        request = urllib.request.Request(self.origin + "/api" + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                detail = error.read().decode("utf-8")
            raise RuntimeError(f"Fixture API {method} {path} returned {error.code}: {detail}") from error

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stdout.close()
        self.resources.close()


class DataDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = paths.chrome()
        if not executable:
            raise unittest.SkipTest("Data dashboard tests need an installed or bundled browser")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="autoskill-data-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.servers = []
        self.addCleanup(self.close_servers)
        self.context = self.browser.new_context(color_scheme="light", accept_downloads=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def close_servers(self):
        for server in self.servers:
            server.close()

    def server(self, name="data", port=0):
        server = DataServer(self.folder / name, port)
        self.servers.append(server)
        return server

    def open(self, server):
        url = server.origin + "/#/data"
        if self.page.url == url:
            self.page.reload()
        else:
            self.page.goto(url)
        expect(self.page.get_by_role("heading", name="数据管理", exact=True)).to_be_visible()
        expect(self.page.get_by_test_id("current-data-root")).to_have_text(str(server.root))

    def test_preferences_are_saved_in_data_and_work_without_browser_storage(self):
        server = self.server()
        self.open(server)
        self.page.locator("#side").get_by_role("button", name="深色", exact=True).click()
        expect(self.page.locator("html")).to_have_attribute("data-theme", "dark")
        self.page.get_by_role("button", name="回放设置", exact=True).click()
        self.page.get_by_role("button", name="Selenium", exact=True).click()
        expect(self.page.get_by_role("button", name="Selenium", exact=True)).to_have_attribute("aria-pressed", "true")
        self.page.get_by_role("switch", name="显示浏览器窗口", exact=True).click()
        expect(self.page.get_by_role("switch", name="显示浏览器窗口", exact=True)).to_have_attribute("aria-checked", "false")
        expected = {"theme": "dark", "engine": "selenium", "headed": False}
        self.assertEqual(server.api("/settings")["dashboard"], expected)
        saved = json.loads((server.root / ".local/settings.json").read_text())
        self.assertEqual(saved["dashboard"], expected)
        self.assertEqual(self.page.evaluate("Object.keys(localStorage).filter(key => ['autoskill.theme','autoskill.engine','autoskill.headed'].includes(key))"), [])
        isolated = self.browser.new_context(color_scheme="light")
        self.addCleanup(isolated.close)
        isolated.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new Error('Storage unavailable'); } });")
        page = isolated.new_page()
        page.goto(server.origin + "/#/data")
        expect(page.get_by_role("heading", name="数据管理", exact=True)).to_be_visible()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.get_by_role("button", name="回放设置", exact=True).click()
        expect(page.get_by_role("button", name="Selenium", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("switch", name="显示浏览器窗口", exact=True)).to_have_attribute("aria-checked", "false")
        self.assertEqual(self.page_errors, [])

    def test_legacy_preferences_fill_only_missing_fields_and_never_follow_another_data_root(self):
        root = self.folder / "legacy"
        (root / ".local").mkdir(parents=True)
        (root / ".local/settings.json").write_text(json.dumps({"dashboard": {"theme": "light"}}))
        self.context.add_init_script("localStorage.setItem('autoskill.theme','dark'); localStorage.setItem('autoskill.engine','selenium'); localStorage.setItem('autoskill.headed','0');")
        server = self.server("legacy")
        self.assertTrue(server.api("/settings")["legacy_dashboard_import_eligible"])
        self.open(server)
        expected = {"theme": "light", "engine": "selenium", "headed": False}
        self.assertEqual(server.api("/settings")["dashboard"], expected)
        self.assertFalse(server.api("/settings")["legacy_dashboard_import_eligible"])
        first_id = server.api("/settings")["data_id"]
        self.assertEqual(self.page.evaluate("localStorage.getItem('autoskill.dashboard.legacy-data-id')"), first_id)
        port = server.port
        server.close()
        self.servers.remove(server)
        second = self.server("new-data", port)
        self.open(second)
        self.assertNotEqual(second.api("/settings")["data_id"], first_id)
        self.assertEqual(second.api("/settings")["dashboard"], {})
        expect(self.page.locator("html")).to_have_attribute("data-theme", "light")
        self.page.get_by_role("button", name="回放设置", exact=True).click()
        expect(self.page.get_by_role("button", name="Playwright", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(self.page.get_by_role("switch", name="显示浏览器窗口", exact=True)).to_have_attribute("aria-checked", "true")
        second.close()
        self.servers.remove(second)
        other = self.folder / "another-legacy"
        (other / ".local").mkdir(parents=True)
        (other / ".local/settings.json").write_text("{}")
        third = self.server("another-legacy", port)
        self.assertTrue(third.api("/settings")["legacy_dashboard_import_eligible"])
        self.open(third)
        self.assertEqual(third.api("/settings")["dashboard"], {})
        self.assertFalse(third.api("/settings")["legacy_dashboard_import_eligible"])
        self.assertEqual(self.page.evaluate("localStorage.getItem('autoskill.dashboard.legacy-data-id')"), first_id)
        self.assertEqual(self.page_errors, [])

    def test_a_new_data_root_does_not_import_unclaimed_old_browser_preferences(self):
        self.context.add_init_script("localStorage.setItem('autoskill.theme','dark'); localStorage.setItem('autoskill.engine','selenium'); localStorage.setItem('autoskill.headed','0');")
        server = self.server()
        self.assertFalse(server.api("/settings")["legacy_dashboard_import_eligible"])
        self.open(server)
        expect(self.page.locator("html")).to_have_attribute("data-theme", "light")
        self.assertEqual(server.api("/settings")["dashboard"], {})
        self.assertIsNone(self.page.evaluate("localStorage.getItem('autoskill.dashboard.legacy-data-id')"))

    def test_a_page_from_another_data_root_cannot_save_preferences_into_the_new_root(self):
        server = self.server("first-data")
        self.open(server)
        first_id = server.api("/settings")["data_id"]
        port = server.port
        server.close()
        self.servers.remove(server)
        second = self.server("second-data", port)
        self.assertNotEqual(second.api("/settings")["data_id"], first_id)
        self.page.locator("#side").get_by_role("button", name="深色", exact=True).click()
        expect(self.page.locator("#toasts")).to_contain_text("数据")
        expect(self.page.locator("html")).to_have_attribute("data-theme", "light")
        self.assertEqual(second.api("/settings")["dashboard"], {})
        saved_file = second.root / ".local/settings.json"
        before = saved_file.read_bytes() if saved_file.exists() else None
        self.page.get_by_role("button", name="回放设置", exact=True).click()
        browser = self.page.get_by_role("switch", name="使用本机 Chrome", exact=True)
        previous = browser.get_attribute("aria-checked")
        with self.page.expect_response(lambda response: response.url == second.origin + "/api/settings"
                                       and response.request.method == "PUT") as change:
            browser.click()
        self.assertEqual(change.value.status, 409)
        expect(browser).to_have_attribute("aria-checked", previous)
        self.assertEqual(saved_file.read_bytes() if saved_file.exists() else None, before)
        self.assertEqual(self.page_errors, [])

    def test_save_and_import_errors_are_visible_and_keep_the_previous_state(self):
        server = self.server()
        self.open(server)
        self.page.route("**/api/settings", lambda route: route.fulfill(status=503, content_type="application/json", body='{"detail":"模拟偏好保存失败"}')
                        if route.request.method == "PUT" else route.continue_())
        self.page.locator("#side").get_by_role("button", name="深色", exact=True).click()
        expect(self.page.locator("#toasts")).to_contain_text("模拟偏好保存失败")
        expect(self.page.locator("html")).to_have_attribute("data-theme", "light")
        self.page.get_by_role("button", name="回放设置", exact=True).click()
        self.page.get_by_role("switch", name="显示浏览器窗口", exact=True).click()
        expect(self.page.get_by_role("switch", name="显示浏览器窗口", exact=True)).to_have_attribute("aria-checked", "true")
        self.assertEqual(server.api("/settings")["dashboard"], {})
        self.page.keyboard.press("Escape")
        self.page.get_by_label("旧数据位置", exact=True).fill(str(self.folder / "missing-source"))
        self.page.get_by_label("新的空数据目录", exact=True).fill(str(self.folder / "new-target"))
        self.page.get_by_role("button", name="导入到新目录", exact=True).click()
        expect(self.page.locator("#view [role=alert]:visible")).not_to_be_empty()
        expect(self.page.get_by_role("heading", name="已导入，尚未启用", exact=True)).to_have_count(0)
        expect(self.page.get_by_test_id("current-data-root")).to_have_text(str(server.root))
        self.assertFalse((self.folder / "new-target").exists())
        self.assertEqual(self.page_errors, [])

    def test_complete_export_and_import_preserve_data_without_activating_the_copy(self):
        server = self.server()
        project = server.api("/projects", "POST", {"name": "Portable history", "url": "http://portable.invalid"})
        local = server.root / ".local"
        (local / "runs/historic-run").mkdir(parents=True)
        (local / "runs/historic-run/evidence.txt").write_text("historical evidence bytes", encoding="utf-8")
        (local / "credentials.json").write_text('{"fixture-account":"synthetic-only"}', encoding="utf-8")
        with sqlite3.connect(local / "autoskill.db") as database:
            database.execute("INSERT INTO runs(id,project_id,engine,status,created_at) VALUES(?,?,?,?,?)",
                             ("historic-run", project["id"], "http", "Fail", "2026-01-01T00:00:00Z"))
        self.open(server)
        self.page.locator("#side").get_by_role("button", name="深色", exact=True).click()
        expect(self.page.locator("html")).to_have_attribute("data-theme", "dark")
        self.page.get_by_role("button", name="导出全部数据", exact=True).click()
        expect(self.page.get_by_role("heading", name="完整数据 ZIP 已生成", exact=True)).to_be_visible(timeout=20000)
        with self.page.expect_download() as download:
            self.page.get_by_role("link", name="下载完整数据 ZIP", exact=True).click()
        archive = self.folder / "complete-data.zip"
        download.value.save_as(archive)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertTrue(any(name.endswith(".local/credentials.json") for name in names))
            self.assertTrue(any(name.endswith(".local/autoskill.db") for name in names))
            self.assertTrue(any(name.endswith(".local/runs/historic-run/evidence.txt") for name in names))
        target = self.folder / "imported-data"
        self.page.get_by_label("旧数据位置", exact=True).fill(str(archive))
        self.page.get_by_label("新的空数据目录", exact=True).fill(str(target))
        self.page.get_by_role("button", name="导入到新目录", exact=True).click()
        expect(self.page.get_by_role("heading", name="已导入，尚未启用", exact=True)).to_be_visible(timeout=20000)
        expect(self.page.get_by_test_id("imported-data-root")).to_have_text(str(target))
        expect(self.page.get_by_test_id("current-data-root")).to_have_text(str(server.root))
        self.assertEqual(server.api("/data/status")["root"], str(server.root))
        self.assertEqual((target / ".local/credentials.json").read_bytes(), (local / "credentials.json").read_bytes())
        self.assertEqual(json.loads((target / ".local/settings.json").read_text())["dashboard"], {"theme": "dark"})
        self.assertEqual((target / ".local/runs/historic-run/evidence.txt").read_bytes(), (local / "runs/historic-run/evidence.txt").read_bytes())
        with sqlite3.connect((target / ".local/autoskill.db").as_uri() + "?mode=ro", uri=True) as database:
            self.assertEqual(database.execute("SELECT status FROM runs WHERE id='historic-run'").fetchone()[0], "Fail")
        before = (target / ".local/credentials.json").read_bytes()
        self.page.get_by_role("button", name="导入到新目录", exact=True).click()
        expect(self.page.locator("#view [role=alert]:visible")).not_to_be_empty()
        self.assertEqual((target / ".local/credentials.json").read_bytes(), before)
        self.assertEqual(self.page_errors, [])


if __name__ == "__main__":
    unittest.main()
