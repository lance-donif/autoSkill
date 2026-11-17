"""Verify visible replay scope and honest request receipts with a local browser and server."""
import re

from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, projects, runner


class RunFlowDashboardTests(DashboardWorkspace):
    def track_starts(self):
        requests = []
        self.page.on("request", lambda request: requests.append(request.post_data_json)
                     if request.method == "POST" and request.url.endswith("/api/runs") else None)
        return requests

    def test_visible_scope_runs_one_real_case_and_empty_scope_cannot_post(self):
        self.open("cases?range=all")
        requests = self.track_starts()
        search = self.page.get_by_role("searchbox", name="搜索用例", exact=True)
        search.fill("no-such-case")
        empty = self.page.get_by_role("button", name="运行当前显示的 0 条", exact=True)
        expect(empty).to_be_disabled()
        empty.evaluate("button => button.click()")
        self.assertEqual(requests, [])
        search.fill("可取消慢请求")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/runs")) as started:
            self.page.get_by_role("button", name="运行当前显示的 1 条", exact=True).click()
        receipt = started.value.json()
        self.assertEqual(started.value.status, 200, receipt)
        self.assertEqual(requests[0]["items"], [{"project_id": self.pid, "case_ids": ["TC-SLOW"]}])
        run = receipt["runs"][0]
        expect(self.page).to_have_url(re.compile("/runs/" + run["id"]))
        self.assertEqual(self.client.get("/api/runs/" + run["id"]).json()["total"], 1)
        expect(self.page.locator("#launch-receipts")).to_be_hidden()

    def test_live_failure_opens_and_single_case_rerun_keeps_the_original_task(self):
        case = builder.api_case("TC-LIVE-FAIL", "持续更新的断言失败", "GET", "/slow",
                                {"status": 200, "json_equals": {"/ok": False}}, params={"label": "{{label}}"})
        projects.save_cases(self.pid, [case])
        first = runner.create(self.pid, [case["id"]], api_only=True, session_id="live-report", inputs={"label": "fixed"})
        run_id = first["id"]
        result = self.page.locator('[data-case="TC-LIVE-FAIL"]')
        self.open("runs/" + run_id)
        expect(self.page.get_by_text("正在准备", exact=True)).to_be_visible()
        self.assertEqual(runner.execute(run_id)["status"], "Fail")
        head = result.locator(".result-head")
        expect(head).to_have_attribute("aria-expanded", "true")
        expect(result).to_contain_text("字段 /ok")
        head.click()
        with self.page.expect_response(lambda response: response.url.endswith("/api/runs/" + run_id)):
            self.page.evaluate("""async () => {
                const {refresh} = await import(new URL('core.js', document.querySelector('script[type="module"]').src).href);
                refresh();
            }""")
        expect(head).to_have_attribute("aria-expanded", "false")
        self.page.reload()
        expect(head).to_have_attribute("aria-expanded", "true")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/{run_id}/rerun")) as restarted:
            result.get_by_role("button", name="只重跑这一条", exact=True).click()
        reply = restarted.value
        self.assertEqual(reply.status, 200, reply.text())
        self.assertEqual(reply.request.post_data_json["case_id"], case["id"])
        retry = reply.json()["id"]
        self.assertNotEqual(retry, run_id)
        expect(self.page).to_have_url(re.compile("/runs/" + retry))
        expect(self.page.get_by_role("link", name="查看首条未通过结果", exact=True)).to_have_attribute("href", re.compile("/runs/" + retry))
        expect(self.page.locator('#view').get_by_text("本次综合失败", exact=False)).to_be_visible()
        replayed = self.client.get("/api/runs/" + retry).json()
        self.assertEqual((replayed["session_id"], replayed["case_ids"]), ("live-report", [case["id"]]))
        self.assertEqual(replayed["results"][0]["category"], "assertion_failed")

    def test_hidden_selections_can_be_removed_and_folder_failure_never_expands_scope(self):
        self.open("cases?range=all")
        self.page.get_by_role("checkbox", name="选择 TC-SLOW", exact=True).check()
        self.page.get_by_role("searchbox", name="搜索用例", exact=True).fill("接口会话校验")
        self.page.get_by_role("checkbox", name="选择 TC-CHECK", exact=True).check()
        expect(self.page.locator(".select-bar")).to_contain_text("其中 1 条不在当前显示范围")
        self.page.get_by_role("button", name="清除隐藏的 1 条", exact=True).click()
        expect(self.page.get_by_role("button", name="运行所选 1 条", exact=True)).to_be_enabled()
        requests = self.track_starts()
        self.page.route("**/api/projects/*/folders", lambda route: route.fulfill(status=500, json={"detail": "fixture folder unavailable"}))
        self.page.reload()
        expect(self.page.get_by_role("button", name="运行当前显示的 0 条", exact=True)).to_be_disabled()
        expect(self.page.get_by_role("button", name="运行项目全部 5 条", exact=True)).to_be_disabled()
        expect(self.page.locator("#view")).to_contain_text("文件夹加载失败")
        self.assertEqual(requests, [])

    def test_partial_receipt_retries_only_rejected_project_and_preserves_unknown_after_reload(self):
        other = projects.create("另一个项目", f"http://localhost:{self.target.server_port}", project_id="flow-other")
        projects.save_cases(other["id"], [builder.api_case("TC-OTHER", "另一个接口", "GET", "/slow", {"status": 200, "json_equals": {"/ok": True}})])
        self.open("cases?range=all")
        requests = []

        def answer(route):
            if route.request.method != "POST":
                route.continue_()
                return
            requests.append(route.request.post_data_json)
            if len(requests) == 1:
                route.fulfill(json={"runs": [{"id": "fixture-accepted", "project_id": self.pid}],
                                    "skipped": [{"project_id": other["id"], "reason": "fixture explicit rejection"}]})
            else:
                route.fulfill(status=500, json={"detail": "fixture lost response"})

        self.page.route("**/api/runs", answer)
        self.page.get_by_role("checkbox", name="选择 TC-SLOW", exact=True).check()
        self.page.get_by_role("checkbox", name="选择 TC-OTHER", exact=True).check()
        self.page.locator("#scope").get_by_role("button", name="筛选项目", exact=True).click()
        self.page.get_by_role("menuitemcheckbox", name="另一个项目", exact=True).click()
        expect(self.page.locator(".select-bar")).to_contain_text("已选 2 条用例")
        expect(self.page.locator(".select-bar")).to_contain_text("其中 1 条不在当前显示范围")
        self.page.get_by_role("button", name="运行所选 2 条", exact=True).click()
        expect(self.page.locator("#launch-receipts")).to_contain_text("fixture-accepted")
        expect(self.page.locator("#launch-receipts")).to_contain_text("另一个项目：未启动")
        self.page.get_by_role("button", name="只重试未启动的 1 个项目", exact=True).click()
        expect(self.page.locator("#launch-receipts")).to_contain_text("启动结果未知")
        self.assertEqual(requests[1]["items"], [{"project_id": other["id"], "case_ids": ["TC-OTHER"]}])
        self.page.reload()
        expect(self.page.locator("#launch-receipts")).to_contain_text("启动结果未知")
        expect(self.page.get_by_role("button", name="只重试未启动的 1 个项目", exact=True)).to_have_count(0)
        self.assertEqual(len(requests), 2)

    def test_matching_inflight_targets_share_one_real_request(self):
        self.open("cases?range=all")
        requests = self.track_starts()
        self.page.evaluate("""() => {
            const original = window.fetch;
            window.fetch = async (url, options) => {
                const response = await original(url, options);
                if (url === '/api/runs' && options?.method === 'POST') await new Promise(resolve => setTimeout(resolve, 250));
                return response;
            };
        }""")
        self.page.evaluate("""async project => {
            const {startRun} = await import(new URL('ui.js', document.querySelector('script[type="module"]').src).href);
            await Promise.all([
                startRun([{project_id:project,case_ids:['TC-SLOW','TC-CHECK']}]),
                startRun([{project_id:project,case_ids:['TC-CHECK','TC-SLOW']}])
            ]);
        }""", self.pid)
        self.assertEqual(len(requests), 1, requests)
        self.assertEqual(requests[0]["items"][0]["case_ids"], ["TC-CHECK", "TC-SLOW"])

    def test_pending_receipt_becomes_unknown_on_reload_and_settings_link_opens_once(self):
        self.open("cases?range=all&settings=" + self.pid)
        dialog = self.page.get_by_role("dialog", name="项目设置", exact=True)
        expect(dialog).to_be_visible()
        dialog.locator(".drawer-head").get_by_role("button", name="关闭", exact=True).click()
        self.page.evaluate("""async () => {
            const {refresh} = await import(new URL('core.js', document.querySelector('script[type="module"]').src).href);
            refresh();
        }""")
        expect(dialog).to_have_count(0)
        settings = self.client.get("/api/settings").json()
        self.page.evaluate("""([dataId, receipt]) => sessionStorage.setItem('autoskill.launch-receipts.' + dataId, JSON.stringify([receipt]))""",
                           [settings["data_id"], {"id": "pending-fixture", "pending": True, "rows": [{"project_id": self.pid}]}])
        requests = self.track_starts()
        self.page.reload()
        expect(self.page.locator("#launch-receipts")).to_contain_text("启动结果未知")
        expect(self.page.locator("#launch-receipts")).not_to_contain_text("正在启动测试")
        self.assertEqual(requests, [])
