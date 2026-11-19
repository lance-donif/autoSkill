"""The dashboard inspects host-created native assets and retains reports, reruns and schedules."""
import os
import sys
import unittest
from pathlib import Path

from dashboard_revision_fixture import change_project_and_wait_for_ui
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

    def test_inspect_host_asset_report_and_bound_evidence_download(self):
        asset = native_assets.import_asset(self.project_id, str(self.source), "selenium-unittest", name="Native smoke")
        started = self.client.post("/api/native-runs", json={"project_id": self.project_id, "asset_id": asset["id"]})
        self.assertEqual(started.status_code, 200, started.text)
        prepared = started.json()
        self.page.goto(self.origin + "/#/native-assets/" + asset["id"] + "?project=" + self.project_id)
        expect(self.page.locator("#view h1")).to_have_text("原生资产")
        expect(self.page.get_by_text("test_native_smoke.py", exact=True)).to_be_visible()
        expect(self.page.get_by_text("Native smoke", exact=True)).to_be_visible()
        expect(self.page.get_by_role("button", name="导入原生资产", exact=True)).to_have_count(0)
        expect(self.page.get_by_role("button", name="运行资产", exact=True)).to_have_count(0)
        expect(self.page.locator("#view")).to_contain_text(asset["asset_version"])
        self.page.get_by_role("link", name="查看项目执行记录", exact=True).click()
        expect(self.page.locator("#view h1")).to_have_text("执行记录")
        self.page.locator('#view a[href^="#/runs/' + prepared["id"] + '"]').click()
        expect(self.page.locator("nav a[aria-current='page']")).to_have_attribute("data-page", "runs")
        expect(self.page.get_by_role("button", name="筛选项目", exact=True)).to_have_count(0)
        expect(self.page.locator("#view h1")).to_have_text("执行报告")
        summary = self.page.locator("#view section").first
        expect(summary.locator(".status").first).to_have_text("通过", timeout=30000)
        self.page.locator(".result-head").first.click()
        expect(self.page.locator(".result-body .checks")).to_be_visible()
        expect(self.page.locator(".result-body .state-details")).to_have_count(0)
        with self.page.expect_download() as event:
            summary.get_by_role("button", name="下载 HTML 报告", exact=True).click()
        exported = self.page.context.new_page()
        self.addCleanup(exported.close)
        exported.set_content(event.value.path().read_text(encoding="utf-8"))
        expect(exported.locator(".result")).to_have_count(1)
        expect(exported.locator(".result")).to_contain_text("test_expected")
        expect(exported.locator(".result-body .state-details")).to_have_count(0)
        self.page.get_by_role("button", name="列出报告与证据附件", exact=True).click()
        attachment = self.page.get_by_role("link", name="unittest.json", exact=True)
        expect(attachment).to_be_visible()
        attachment.focus()
        change_project_and_wait_for_ui(self, self.project_id)
        expect(attachment).to_be_visible()
        expect(attachment).to_be_focused()
        expect(self.page.get_by_role("button", name="按原版本再次运行", exact=True)).to_have_count(0)

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
        expect(self.page.locator("#view h1")).to_have_text("执行报告")
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

    def test_native_long_results_fit_real_iframe_and_keep_reading_state_on_refresh(self):
        long_name = "test_000_" + "unbroken_order_identifier_" * 8
        methods = [f"    def {long_name}(self):\n        self.assertEqual('actual', 'expected')\n"]
        methods.extend(f"    def test_{number:03}(self):\n        self.assertEqual(2 + 2, 4)\n" for number in range(1, 70))
        self.source.write_text("import unittest\n\nclass LongResults(unittest.TestCase):\n" + "\n".join(methods), encoding="utf-8")
        asset = native_assets.import_asset(self.project_id, str(self.source), "selenium-unittest", name="订单结果与完整证据")
        response = self.client.post("/api/native-runs", json={"project_id": self.project_id, "asset_id": asset["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["id"]
        self.page.goto(self.origin + "/api/status")
        self.page.set_viewport_size({"width": 700, "height": 700})
        destination = self.origin + "/?embed=1#/native-runs/" + run_id
        self.page.set_content('<iframe name="host" style="width:390px;height:500px;border:0" src="' + destination + '"></iframe>')
        reader = self.page.frame(name="host")
        expect(reader.locator("#view h1")).to_have_text("执行报告")
        expect(reader.locator("#view section").first.locator(".status").first).to_have_text("不通过", timeout=30000)
        failing = reader.locator(".result").filter(has_text=long_name)
        failing.locator(".result-head").click()
        complete = failing.locator("summary").filter(has_text="完整用例结果")
        expect(complete).to_be_visible()
        complete.click()
        complete.press("Tab")
        self.page.keyboard.press("Shift+Tab")
        expect(complete).to_be_focused()
        position = reader.evaluate("scrollY")
        with self.page.expect_response(lambda reply: reply.url.split('?')[0] == self.origin + '/api/native-runs/' + run_id):
            updated = self.client.patch('/api/projects/' + self.project_id, json={'category': '来自宿主的真实更新'})
            self.assertEqual(updated.status_code, 200, updated.text)
        expect(reader.locator('#view')).not_to_have_attribute('aria-busy', 'true')
        expect(complete).to_be_focused()
        self.assertTrue(complete.locator('..').evaluate('node => node.open'))
        self.assertEqual(reader.evaluate("scrollY"), position)
        with self.page.expect_download() as event:
            reader.get_by_role("button", name="下载 HTML 报告", exact=True).click()
        exported = self.page.context.new_page()
        self.addCleanup(exported.close)
        exported.set_content(event.value.path().read_text(encoding="utf-8"))
        expect(exported.locator(".result")).to_have_count(70)
        expect(exported.locator(".result").last).to_contain_text("test_069")
        self.assertEqual(reader.evaluate("scrollX"), 0)
        geometry = reader.evaluate("({width:innerWidth, document:document.documentElement.scrollWidth})")
        self.assertLessEqual(geometry['document'], geometry['width'], geometry)
        if output := os.environ.get("AUTOSKILL_VALIDATION_OUTPUT"):
            self.page.screenshot(path=str(Path(output) / "native-long-iframe390.png"), full_page=True)


if __name__ == "__main__":
    unittest.main()
