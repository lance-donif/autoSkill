"""Project settings persist real runtime values, credentials and asynchronous login runs."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dashboard_schedules as fixtures

from autoskill import builder, projects, secrets


class LoginHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "token": "synthetic-login-token"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class ProjectSettingsTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DashboardScheduleTests.setUpClass.__func__)
    disable_tasks = fixtures.DashboardScheduleTests.disable_tasks
    stop_process = fixtures.DashboardScheduleTests.stop_process
    wait_for_server = fixtures.DashboardScheduleTests.wait_for_server
    new_page = fixtures.DashboardScheduleTests.new_page
    tearDown = fixtures.DashboardScheduleTests.tearDown

    def setUp(self):
        fixtures.DashboardScheduleTests.setUp(self)

    def settings(self, name="Schedule fixture"):
        self.page.goto(self.origin + "/#/cases")
        self.page.get_by_role("button", name=f"「{name}」设置", exact=True).click()
        return self.page.get_by_role("dialog", name="项目设置", exact=True)

    def test_runtime_values_credentials_create_edit_delete_and_refresh(self):
        dialog = self.settings()
        dialog.get_by_label("操作超时（秒）", exact=True).fill("240")
        dialog.get_by_label("页面加载超时（秒）", exact=True).fill("900")
        dialog.get_by_label("忽略 HTTPS 证书错误", exact=True).check()
        dialog.locator(".drawer-foot").get_by_role("button", name="保存", exact=True).click()
        info = self.client.get('/api/projects/' + self.project_id).json()
        self.assertEqual((info["timeout_seconds"], info["page_load_seconds"], info["ignore_https_errors"]), (240, 900, True))
        dialog = self.settings()
        expect(dialog.get_by_label("操作超时（秒）", exact=True)).to_have_value("240")
        expect(dialog.get_by_label("页面加载超时（秒）", exact=True)).to_have_value("900")
        dialog.get_by_text("添加与管理本机凭据", exact=True).click()
        dialog.get_by_label("凭据名称", exact=True).fill("SETTINGS_TOKEN")
        dialog.get_by_label("凭据内容", exact=True).fill("synthetic-value")
        dialog.get_by_role("button", name="添加凭据", exact=True).click()
        expect(dialog.get_by_role("textbox", name="SETTINGS_TOKEN", exact=True)).to_have_value("synthetic-value")
        dialog.get_by_role("textbox", name="SETTINGS_TOKEN", exact=True).fill("synthetic-changed")
        row = dialog.get_by_role("textbox", name="SETTINGS_TOKEN", exact=True).locator('..')
        row.get_by_role("button", name="保存", exact=True).click()
        expect(self.page.locator(".toast").last).to_contain_text("SETTINGS_TOKEN 已保存")
        self.assertEqual(secrets.get_secret("SETTINGS_TOKEN"), "synthetic-changed")
        dialog.get_by_role("button", name="查看全部本机凭据", exact=True).click()
        dialog.get_by_role("button", name="删除凭据 SETTINGS_TOKEN", exact=True).click()
        expect(dialog.get_by_role("textbox", name="SETTINGS_TOKEN", exact=True)).to_have_count(0)
        self.assertNotIn("SETTINGS_TOKEN", secrets.names())
        bad = self.client.patch('/api/projects/' + self.project_id, json={"timeout_seconds": 0})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.get('/api/projects/' + self.project_id).json()["timeout_seconds"], 240)

    def test_explicit_login_is_queued_uses_window_preference_and_preserves_force(self):
        site = ThreadingHTTPServer(("127.0.0.1", 0), LoginHandler)
        threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        pid = projects.create("Login fixture", f"http://127.0.0.1:{site.server_port}")["id"]
        case = builder.api_case("TC-API-LOGIN", "Saved login fixture", "GET", "/login", {"status": 200, "json_equals": {"/ok": True}}, extract={"LOGIN_TOKEN": "/token"})
        projects.save_cases(pid, [case])
        projects.set_auth(pid, {"api_case": case["id"]})
        data = self.client.get("/api/settings").json()
        self.assertEqual(self.client.put("/api/settings", json={"dashboard": {"headed": False}, "expected_data_id": data["data_id"]}).status_code, 200)
        self.page.reload()
        dialog = self.settings("Login fixture")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/login")) as response:
            dialog.get_by_role("button", name="强制重新登录", exact=True).click()
        receipt = response.value.json()
        self.assertEqual(response.value.status, 200)
        self.assertEqual(receipt["status"], "Queued")
        self.assertIs(receipt["headed"], False)
        expect(self.page.locator("#view h1")).to_have_text("Login fixture")
        expect(self.page.locator(".run-summary .status").first).to_have_text("通过", timeout=30000)
        run = self.client.get('/api/runs/' + receipt["id"]).json()
        self.assertEqual((run["force_login"], run["headed"], run["trigger"]), (1, 0, "login"))
        self.assertTrue(self.client.get('/api/projects/' + pid).json()["session"]["saved"])
        dialog = self.settings("Login fixture")
        dialog.get_by_role("button", name="清除登录态", exact=True).click()
        expect(dialog).to_have_count(0)
        self.assertFalse(self.client.get('/api/projects/' + pid).json()["session"]["saved"])


if __name__ == "__main__":
    unittest.main()
