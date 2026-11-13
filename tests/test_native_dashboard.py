"""The native dashboard imports real assets, runs them, and saves native/host schedules."""
import json
import sys
import unittest
from pathlib import Path

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dashboard_schedules as fixtures

from autoskill import native_assets, workflow


class NativeDashboardTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DashboardScheduleTests.setUpClass.__func__)
    disable_tasks = fixtures.DashboardScheduleTests.disable_tasks
    stop_process = fixtures.DashboardScheduleTests.stop_process
    wait_for_server = fixtures.DashboardScheduleTests.wait_for_server
    new_page = fixtures.DashboardScheduleTests.new_page
    tearDown = fixtures.DashboardScheduleTests.tearDown

    def setUp(self):
        fixtures.DashboardScheduleTests.setUp(self)
        self.source = self.folder / "test_native_smoke.py"
        self.source.write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_expected(self):\n        self.assertEqual(2 + 2, 4)\n", encoding="utf-8")

    def test_import_run_report_and_bound_evidence_download(self):
        self.page.goto(self.origin + "/#/native-assets")
        expect(self.page.locator("#view h1")).to_have_text("原生资产")
        self.page.get_by_role("button", name="导入原生资产", exact=True).first.click()
        dialog = self.page.get_by_role("dialog", name="导入原生资产", exact=True)
        dialog.get_by_label("名称", exact=True).fill("Native smoke")
        dialog.get_by_label("来源目录或文件", exact=True).fill(str(self.source))
        dialog.get_by_label("执行器", exact=True).select_option("selenium-unittest")
        dialog.get_by_role("button", name="导入", exact=True).click()
        expect(self.page.locator("#view h2")).to_have_text("Native smoke")
        expect(self.page.get_by_text("test_native_smoke.py", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="运行资产", exact=True).click()
        run_dialog = self.page.get_by_role("dialog", name="运行原生资产", exact=True)
        run_dialog.get_by_label("运行参数（JSON）", exact=True).fill("[]")
        run_dialog.get_by_role("button", name="开始运行", exact=True).click()
        expect(run_dialog.get_by_role("alert")).to_contain_text("JSON 对象")
        run_dialog.get_by_label("运行参数（JSON）", exact=True).fill("{}")
        run_dialog.get_by_role("button", name="开始运行", exact=True).click()
        expect(self.page.locator("#view h1")).to_have_text("原生执行报告")
        summary = self.page.locator("#view section").first
        expect(summary.locator(".status").first).to_have_text("已结束", timeout=30000)
        expect(summary.locator(".state-details")).to_contain_text("综合：通过", timeout=30000)
        expect(summary.locator(".state-details")).to_contain_text("业务：通过")
        self.page.locator(".result-head").first.click()
        expect(self.page.locator(".result-body .checks")).to_be_visible()
        with self.page.expect_download() as event:
            self.page.get_by_role("link", name="下载原生汇总 JSON", exact=True).click()
        download = event.value
        data = json.loads(Path(download.path()).read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "Pass")
        self.assertEqual(len(data["cases"]), 1)
        self.page.get_by_role("button", name="列出报告与证据附件", exact=True).click()
        expect(self.page.get_by_role("link", name="unittest.json", exact=True)).to_be_visible()
        first = self.page.url
        self.page.get_by_role("button", name="按原版本再次运行", exact=True).click()
        expect(self.page).not_to_have_url(first)
        expect(self.page.locator("#view h1")).to_have_text("原生执行报告")

    def test_native_and_maintenance_targets_are_saved_and_delivery_is_not_a_fake_run(self):
        asset = native_assets.import_asset(self.project_id, str(self.source), "selenium-unittest", name="Scheduled native")
        task = workflow.create(self.project_id, {"title": "Scheduled requirements", "sources": [{"id": "s1", "text": "Check the saved fixture"}]}, idempotency_key="schedule-workflow")
        self.page.reload()
        self.page.get_by_role("button", name="新建定时任务", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="新建定时任务", exact=True)
        dialog.get_by_label("任务名称", exact=True).fill("Native periodic")
        dialog.get_by_label("任务类型", exact=True).select_option("native")
        expect(dialog.get_by_label("原生资产", exact=True).locator("option")).to_have_count(2)
        dialog.get_by_label("原生资产", exact=True).select_option(asset["id"])
        dialog.get_by_role("button", name="创建任务", exact=True).click()
        native_row = self.page.locator("tr").filter(has_text="Native periodic")
        expect(native_row).to_contain_text("原生资产")
        native_row.get_by_role("button", name="立即运行", exact=True).click()
        expect(self.page.locator("#view h1")).to_have_text("原生执行报告")
        self.page.goto(self.origin + "/#/schedules")
        self.page.get_by_role("button", name="新建定时任务", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="新建定时任务", exact=True)
        dialog.get_by_label("任务名称", exact=True).fill("Maintenance periodic")
        dialog.get_by_label("任务类型", exact=True).select_option("maintenance")
        expect(dialog.get_by_label("维护工作流", exact=True).locator("option")).to_have_count(2)
        dialog.get_by_label("维护工作流", exact=True).select_option(task["workflow_id"])
        dialog.get_by_label("执行宿主", exact=True).select_option("generic")
        dialog.get_by_role("button", name="创建任务", exact=True).click()
        maintenance_row = self.page.locator("tr").filter(has_text="Maintenance periodic")
        expect(maintenance_row).to_contain_text("AI 维护")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/run")) as delivery:
            maintenance_row.get_by_role("button", name="立即运行", exact=True).click()
        self.assertEqual(delivery.value.json()["status"], "awaiting_host")
        expect(self.page.locator(".toast").last).to_contain_text("等待宿主接续")
        expect(self.page.locator("#view h1")).to_have_text("定时任务")


if __name__ == "__main__":
    unittest.main()
