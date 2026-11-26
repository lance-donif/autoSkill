"""Real Chromium: environment editing, scope-bar switching and sync-file listing."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, projects


class EnvironmentDashboardTests(DashboardWorkspace):
    def setUp(self):
        self.sync_folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.sync_folder.cleanup)
        self.sync_environment = patch.dict(os.environ, {"AUTOSKILL_SYNC_DIR": self.sync_folder.name})
        self.sync_environment.start()
        self.addCleanup(self.sync_environment.stop)
        super().setUp()

    def wait_until(self, condition, message):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        self.fail(message)

    def project(self):
        return self.client.get(f"/api/projects/{self.pid}").json()

    def test_scope_bar_switch_and_settings_drawer_manage_environments(self):
        projects.update(self.pid, environments=[
            {"id": "dev", "name": "开发", "base_url": f"http://127.0.0.1:{self.target.server_port}", "variables": {"greeting": "dev"}, "is_default": True},
            {"id": "prod", "name": "生产", "base_url": "http://127.0.0.1:9"}])
        self.page.goto(self.origin + f"/#/dashboard?projects={self.pid}")
        switch = self.page.get_by_role("button", name="切换运行环境")
        expect(switch).to_contain_text("开发")
        switch.click()
        self.page.get_by_role("menuitemcheckbox").filter(has_text="生产").click()
        expect(switch).to_contain_text("生产")
        self.wait_until(lambda: self.project()["environment"]["id"] == "prod", "scope-bar switch did not change the active environment")

        self.page.goto(self.origin + f"/#/cases?settings={self.pid}")
        drawer = self.page.get_by_role("dialog", name="项目设置")
        expect(drawer).to_be_visible()
        drawer.get_by_role("button", name="添加环境").click()
        names = drawer.locator('input[aria-label="环境名称"]')
        expect(names).to_have_count(3)
        names.nth(2).fill("预发")
        drawer.locator('input[aria-label="环境地址"]').nth(2).fill("http://127.0.0.1:8")
        drawer.get_by_role("button", name="保存", exact=True).click()
        saved = self.wait_until(lambda: next((item for item in self.project()["environments"] if item["name"] == "预发"), None),
                                "added environment was not saved")
        self.assertEqual(saved["base_url"], "http://127.0.0.1:8")
        expect(drawer).to_have_count(0)

        self.page.reload()
        drawer = self.page.get_by_role("dialog", name="项目设置")
        expect(drawer.locator('input[aria-label="环境名称"]')).to_have_count(3)
        drawer.get_by_role("button", name="删除环境").nth(2).click()
        expect(drawer.locator('input[aria-label="环境名称"]')).to_have_count(2)
        drawer.get_by_role("button", name="保存", exact=True).click()
        self.wait_until(lambda: all(item["name"] != "预发" for item in self.project()["environments"]),
                        "removed environment is still saved")

    def test_data_page_scans_and_lists_synced_files(self):
        folder = Path(self.sync_folder.name) / "release"
        folder.mkdir()
        case = builder.api_case("API-SYNCED", "同步用例", "GET", "/begin", {"status": 200})
        (folder / "cases.json").write_text(json.dumps({"project_id": self.pid, "cases": [case], "status": "pass"}, ensure_ascii=False),
                                           encoding="utf-8")
        self.page.goto(self.origin + "/#/data")
        expect(self.page.get_by_role("heading", name="同步与错误目录")).to_be_visible()
        self.page.get_by_role("button", name="立即扫描").click()
        expect(self.page.get_by_text("release/cases.json")).to_be_visible()
        expect(self.page.get_by_text("已导入", exact=True)).to_be_visible()


if __name__ == "__main__":
    unittest.main()
