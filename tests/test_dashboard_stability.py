"""Status filters update their own regions without dimming or rebuilding the dashboard."""
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dashboard_review_fixes as fixtures


class DashboardStabilityTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DashboardReviewTests.setUpClass.__func__)
    handle_request = fixtures.DashboardReviewTests.handle_request

    def setUp(self):
        fixtures.DashboardReviewTests.setUp(self)
        self.requests = []
        self.page.set_viewport_size({"width": 1440, "height": 700})
        self.page.add_init_script("""window.scrollCalls = 0;
            const scroll = window.scrollTo.bind(window);
            window.scrollTo = (...args) => { window.scrollCalls++; scroll(...args); };
        """)

    def history_response(self, path, query):
        if query.get("status") == ["active"]:
            return {"total": 0, "items": []}
        status = query.get("status", ["Fail"])[0]
        record = self.result if path == "/api/results" else self.run
        count = 1 if query.get("current") == ["true"] else 6
        state = {"lifecycle": "completed", "phase": "settled", "outcome": "not_run" if status == "NotRun" else status.lower(),
                 "checks": {"business": "fail" if status == "Fail" else "unverified", "performance": "not_applicable", "quality": "not_applicable"},
                 "evidence": "valid", "report": "not_generated", "delivery": "not_applicable"}
        return {"total": count, "items": [{**record, "id": f"run-{status}-{i}", "status": status, "history_status": status,
                                            "state": state, "case_title": f"{status} case {i}", "reason": status} for i in range(count)]}

    def dashboard_response(self, query):
        failures = self.history_response("/api/results", {**query, "status": ["Fail"]})
        return {"summary": self.summary_response(), "recent": self.history_response("/api/runs", query), "failures": failures}

    def summary_response(self):
        return {"results": {"executions": 8, "passed": 0, "failed": 6, "not_run": 2, "still_failing": 1},
                "cases": {"total": 1, "ui": 1, "api": 0, "added": 0},
                "runs": {"total": 4, "scheduled": 0, "active": 0},
                "trend": {"unit": "day", "points": [{"bucket": "2026-11-12", "passed": 0, "failed": 6, "not_run": 2, "pass_rate": 0}]},
                "tasks": {"total": 0, "enabled": 0, "paused": 0, "fired_in_range": 0, "tasks": []}}

    def api_response(self, request, path, query):
        self.requests.append((path, query))
        if path == "/api/summary":
            return self.summary_response()
        if path == "/api/dashboard":
            return self.dashboard_response(query)
        return fixtures.DashboardReviewTests.api_response(self, request, path, query)

    def remember_regions(self, selectors):
        self.page.evaluate("""selectors => {
            window.keptRegions = selectors.map(selector => [selector, document.querySelector(selector)]);
            window.dimmed = false;
            window.stale = false;
            window.scrollCalls = 0;
            const view = document.getElementById('view');
            new MutationObserver(records => {
                if (records.some(record => record.attributeName === 'class')) {
                    window.stale ||= view.classList.contains('stale');
                    window.dimmed ||= Number(getComputedStyle(view).opacity) < 1;
                }
            }).observe(view, { attributes: true });
        }""", selectors)

    def assert_regions_unchanged(self):
        data = self.page.evaluate("""() => ({
            kept: window.keptRegions.every(([selector, node]) => node === document.querySelector(selector) && node.isConnected),
            dimmed: window.dimmed, stale: window.stale, scrollCalls: window.scrollCalls,
            opacity: getComputedStyle(document.getElementById('view')).opacity,
        })""")
        self.assertEqual(data, {"kept": True, "dimmed": False, "stale": False, "scrollCalls": 0, "opacity": "1"})

    def test_dashboard_failure_scope_changes_only_its_list_and_trend_switch_is_local(self):
        self.page.goto("http://127.0.0.1:8787/#/dashboard?range=all")
        expect(self.page.get_by_role("group", name="失败列表范围")).to_be_visible()
        self.remember_regions(["#side", "#scope", "#view .page-head", "#view .kpis", ".chart-wrap"])
        summaries = sum(path == "/api/summary" for path, _ in self.requests)
        runs = sum(path == "/api/runs" for path, _ in self.requests)
        bundles = sum(path == "/api/dashboard" for path, _ in self.requests)
        self.page.get_by_role("button", name="当前仍失败", exact=True).click()
        expect(self.page.locator("[data-dashboard-failures] .list-row")).to_have_count(1)
        self.assert_regions_unchanged()
        self.assertEqual(sum(path == "/api/summary" for path, _ in self.requests), summaries)
        self.assertEqual(sum(path == "/api/runs" for path, _ in self.requests), runs)
        self.assertEqual(sum(path == "/api/dashboard" for path, _ in self.requests), bundles + 1)
        self.page.get_by_role("button", name="全部", exact=True).click()
        expect(self.page.locator("[data-dashboard-failures] .list-row")).to_have_count(6)
        self.assert_regions_unchanged()
        self.assertEqual(sum(path == "/api/dashboard" for path, _ in self.requests), bundles + 2)
        self.remember_regions(["#side", "#scope", "#view .page-head", "#view .kpis", "[data-dashboard-failures]"])
        self.page.get_by_role("button", name="表格", exact=True).click()
        expect(self.page.locator(".chart-table")).to_be_visible()
        self.page.get_by_role("button", name="图表", exact=True).click()
        expect(self.page.locator(".chart-svg")).to_be_visible()
        self.assert_regions_unchanged()
        self.assertEqual(sum(path == "/api/dashboard" for path, _ in self.requests), bundles + 2)

    def test_three_history_filters_keep_controls_and_back_navigation(self):
        self.page.goto("http://127.0.0.1:8787/#/runs?view=results&range=all")
        choices = self.page.get_by_role("group", name="用例结果", exact=True)
        expect(choices.get_by_role("button")).to_have_count(3)
        self.remember_regions(["#side", "#scope", "#view .page-head", "#view .toolbar", "#view section.card"])
        for status, name, title in (("NotRun", "未执行", "NotRun case"), ("Pass", "通过", "Pass case"), ("Fail", "失败", "Fail case")):
            button = choices.get_by_role("button", name=name, exact=True)
            button.click()
            expect(self.page.locator("#view .table tbody tr").first).to_contain_text(title)
            expect(button).to_have_attribute("aria-pressed", "true")
            expect(button).to_be_focused()
            self.assertIn(f"status={status}", self.page.url)
            self.assert_regions_unchanged()
        self.page.go_back()
        expect(self.page.locator("#view .table tbody tr").first).to_contain_text("Pass case")
        passed = choices.get_by_role("button", name="通过", exact=True)
        expect(passed).to_have_attribute("aria-pressed", "true")
        self.assert_regions_unchanged()
        passed.press("Space")
        expect(choices.locator('[aria-pressed="true"]')).to_have_count(0)
        self.assertNotIn("status=", self.page.url)

    def test_slow_status_response_does_not_dim_and_latest_filter_wins(self):
        self.page.goto("http://127.0.0.1:8787/#/runs?view=results&range=all")
        choices = self.page.get_by_role("group", name="用例结果", exact=True)
        expect(choices).to_be_visible()
        self.page.evaluate("""() => {
            const fetchNow = window.fetch.bind(window);
            window.fetch = async (...args) => {
                if (String(args[0]).includes('status=Pass')) await new Promise(resolve => setTimeout(resolve, 300));
                return fetchNow(...args);
            };
        }""")
        self.remember_regions(["#side", "#scope", "#view .toolbar"])
        choices.get_by_role("button", name="通过", exact=True).click()
        expect(self.page.locator("#view")).to_have_attribute("aria-busy", "true")
        self.assert_regions_unchanged()
        choices.get_by_role("button", name="失败", exact=True).click()
        expect(self.page.locator("#view .table tbody tr").first).to_contain_text("Fail case")
        expect(choices.get_by_role("button", name="失败", exact=True)).to_have_attribute("aria-pressed", "true")
        self.page.wait_for_function("document.getElementById('view').getAttribute('aria-busy') !== 'true'")
        self.assert_regions_unchanged()

    def open_static_preview(self, fragment, width=1440):
        origin = "https://preview.test/autoSkill/"
        errors, unexpected = [], []
        self.page.on("pageerror", lambda error: errors.append(str(error)))

        def serve_file(route):
            url = route.request.url
            if not url.startswith(origin) or "/api/" in url or route.request.method != "GET":
                unexpected.append(url)
                route.abort()
                return
            path = fixtures.ROOT / urlsplit(url).path.removeprefix("/autoSkill/")
            if path.is_dir():
                path = path / "index.html"
            if path.is_file():
                route.fulfill(path=path)
            else:
                unexpected.append(url)
                route.fulfill(status=404)

        self.page.unroute("**/*", self.handle_request)
        self.context.route("**/*", serve_file)
        self.addCleanup(self.assertEqual, unexpected, [])
        self.addCleanup(self.assertEqual, errors, [])
        self.page.set_viewport_size({"width": width, "height": 844})
        self.page.goto(origin + "preview/" + fragment)
        return json.loads((fixtures.ROOT / "preview/fixtures.json").read_text(encoding="utf-8"))

    def test_static_preview_filters_examples_and_returns_to_the_same_result_list(self):
        sample = self.open_static_preview("#/runs?range=all", width=390)
        toolbar = self.page.locator('[data-history-region="toolbar"]')
        rows = self.page.locator("#view tr[data-history-verdict]")
        passed = toolbar.get_by_role("button", name="通过", exact=True)
        failed = toolbar.get_by_role("button", name="失败", exact=True)
        expect(toolbar.get_by_role("group", name="运行状态").get_by_role("button")).to_have_count(3)
        expect(rows).to_have_count(sample["/runs"]["total"])
        passed.click()
        expect(rows).to_have_count(sample["/runs?status=Pass"]["total"])
        toolbar.get_by_role("button", name="按用例结果").click()
        expect(passed).to_have_attribute("aria-pressed", "true")
        count = sample["/results?status=Pass"]["total"]
        expect(rows).to_have_count(count)
        expect(self.page.locator("[data-history-count]")).to_have_text(str(count))
        link = rows.first.locator("a[data-history-id]")
        result_id = link.get_attribute("data-history-id").removeprefix("result:")
        link.click()
        target = self.page.locator(f'.result[data-result="{result_id}"]')
        head = target.locator(".result-head")
        expect(head).to_be_focused()
        expect(target.locator(".result-body")).to_be_visible()
        head.press("Enter")
        expect(target.locator(".result-body")).to_be_hidden()
        head.press("Enter")
        expect(target.locator(".result-body")).to_be_visible()
        self.page.get_by_role("link", name="返回执行记录").click()
        expect(passed).to_have_attribute("aria-pressed", "true")
        expect(rows).to_have_count(count)
        failed.click()
        expect(rows).to_have_count(sample["/results?status=Fail"]["total"])
        expect(rows.first.locator("td:first-child .status")).to_have_text("失败")
        failed.click()
        expect(rows).to_have_count(sample["/results"]["total"])
        self.page.go_back()
        expect(failed).to_have_attribute("aria-pressed", "true")
        toolbar.get_by_role("button", name="未执行", exact=True).click()
        expect(rows).to_have_count(sample["/results?status=NotRun"]["total"])
        expect(rows.first.locator("td:first-child .status")).to_have_text("未执行")
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

    def test_static_preview_case_edit_folders_settings_and_overview(self):
        sample = self.open_static_preview("#/dashboard?range=all")
        nav = self.page.get_by_role("navigation", name="主导航", exact=True)
        expect(nav.locator("a .label")).to_have_text(["总览", "测试用例", "执行记录", "定时任务", "数据管理"])
        cards = self.page.locator(".kpis .kpi")
        expect(cards).to_have_count(4)
        self.assertEqual(cards.locator(".kpi-label").evaluate_all("nodes => nodes.map(node => node.firstChild.textContent)"),
                         ["最近一批通过率", "用例总数", "通过", "失败"])
        expect(cards.first.get_by_role("img", name="未执行 0，通过 2，失败 1")).to_be_visible()
        expect(cards.first.locator(".kpi-value")).to_have_text("66.7%")
        expect(cards.last.locator(".kpi-note")).to_have_count(0)
        expect(self.page.locator(".page-head").get_by_role("link", name="选择用例重新运行")).to_have_count(0)
        tasks = self.page.locator("#view section").filter(has=self.page.get_by_role("heading", name="定时任务", exact=True))
        expect(tasks.locator(".card-head .sub")).to_have_text("启用 2 · 暂停 1")
        expect(tasks.locator(".status.idle").filter(has_text="未执行").first).to_be_visible()
        expect(self.page.locator("#view .card-head .sub")).to_have_count(1)
        recent = self.page.locator("#view section").filter(has=self.page.get_by_role("heading", name="最近执行", exact=True))
        expect(recent).to_be_visible()
        for phase in ("已结束", "阻塞"):
            expect(recent.get_by_text(phase, exact=True)).to_have_count(0)
        nav.get_by_role("link", name="测试用例", exact=True).click()
        for case in sample["/cases"]["items"]:
            tree = sample["/projects/" + case["project_id"] + "/folders"]
            by_id = {item["id"]: item for item in tree["folders"]}
            nested = by_id.get(tree["assignments"].get(case["case_id"]))
            if case["kind"] == "api" and nested and nested["parent_id"]:
                break
        else:
            self.fail("The sample catalog needs a nested API case")
        project = next(item for item in sample["/projects"] if item["id"] == case["project_id"])
        folders = self.page.get_by_role("navigation", name=project["name"] + "文件夹")
        expect(folders.get_by_text("根目录", exact=True)).to_have_count(0)
        folders.get_by_role("button", name="收起全部用例").click()
        expect(folders.locator(".folder-item:visible")).to_have_count(1)
        folders.get_by_role("button", name="展开全部用例").press("Enter")
        parent = by_id[nested["parent_id"]]
        folder_button = folders.locator(f'button[data-folder-id="{nested["id"]}"]')
        expect(folder_button).to_be_hidden()
        folders.get_by_role("button", name="展开" + parent["name"], exact=True).press("Enter")
        folder_button.click()
        expect(folder_button).to_have_attribute("aria-pressed", "true")
        title = self.page.get_by_role("button", name=case["title"], exact=True)
        title.press("Enter")
        dialog = self.page.get_by_role("dialog", name=case["title"], exact=True)
        dialog.get_by_role("button", name="编辑用例", exact=True).click()
        editor = self.page.get_by_role("dialog", name="编辑用例", exact=True)
        editor.get_by_text("基本信息与测试数据", exact=True).click()
        edited = case["title"] + "（编辑预览）"
        editor.get_by_label("用例标题", exact=True).fill(edited)
        editor.get_by_role("button", name="保存修改", exact=True).click()
        dialog = self.page.get_by_role("dialog", name=edited, exact=True)
        dialog.get_by_role("button", name="关闭", exact=True).last.click()
        self.page.get_by_role("button", name=f'「{project["name"]}」设置', exact=True).click()
        settings = self.page.get_by_role("dialog", name="项目设置", exact=True)
        expect(settings.get_by_label("项目名称")).to_have_value(project["name"])
        expect(settings.get_by_label("分类", exact=True)).to_have_count(0)
        renamed = project["name"] + "（预览）"
        settings.get_by_label("项目名称").fill(renamed)
        settings.get_by_role("button", name="保存", exact=True).click()
        expect(settings).to_be_hidden()
        for name in ("执行记录", "定时任务", "数据管理", "测试用例"):
            nav.get_by_role("link", name=name, exact=True).click()
            expect(self.page.locator("#view h1")).to_have_text(name)
        expect(self.page.get_by_role("button", name=edited, exact=True)).to_be_visible()
        expect(self.page.get_by_role("heading", name=renamed, exact=True)).to_be_visible()
        self.page.reload()
        expect(self.page.get_by_role("heading", name=project["name"], exact=True)).to_be_visible()
        for item in sample["/projects"]:
            head = self.page.locator(".project-head").filter(has=self.page.get_by_role("heading", name=item["name"], exact=True))
            expect(head.locator(".sub")).to_have_text(f'{item["base_url"]} · 页面 {item["cases"]["ui"]} · 接口 {item["cases"]["api"]}')
        expect(title).to_be_visible()
        expect(self.page.get_by_role("button", name=edited, exact=True)).to_have_count(0)

    def test_case_filters_stick_below_the_visible_header(self):
        self.open_static_preview("#/cases?range=all")
        filters = self.page.locator(".case-filters")
        expect(filters.get_by_role("searchbox", name="搜索用例")).to_be_visible()
        for width, header in ((1440, "#scope"), (390, "#side")):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 720})
                self.page.evaluate("window.scrollTo(0, 550)")
                expect(filters).to_be_in_viewport()
                self.page.wait_for_function("""selector => Math.abs(document.querySelector('.case-filters').getBoundingClientRect().top
                    - document.querySelector(selector).getBoundingClientRect().bottom) < 2""", arg=header)
                filters.get_by_role("button", name="接口", exact=True).click()
                expect(filters.get_by_role("button", name="接口")).to_have_attribute("aria-pressed", "true")
                filters.get_by_role("button", name="全部", exact=True).click()
                self.page.evaluate("window.scrollTo(0, 0)")

    def test_static_observed_cases_link_to_timestamped_public_evidence(self):
        sample = self.open_static_preview("#/cases?range=all&projects=runoob")
        records = sample["/observations/runoob"]["cases"]
        source_name = "实测来源"
        for kind in ("live", "historical"):
            record = next(row for row in records if row["source_kind"] == kind)
            title = self.page.get_by_role("button", name=record["title"], exact=True)
            title.click()
            panel = self.page.get_by_role("dialog", name=record["title"], exact=True)
            source = panel.get_by_role("region", name=source_name)
            expect(source.locator("time")).to_have_attribute("datetime", record["checked_at"])
            expect(source.locator("time")).to_have_text(record["checked_at"].replace("T", " "))
            expect(source.get_by_role("link", name="打开目标页")).to_have_attribute("href", record["url"])
            source.locator("time").click()
            expect(panel).to_be_hidden()
            expect(self.page.locator(".run-summary").get_by_role("region", name=source_name)).to_have_count(0)
            report = self.page.locator(".result-body").get_by_role("region", name=source_name).filter(has_text=record["case_id"])
            expect(report).to_contain_text(record["actual"])
            expect(report.locator(f'time[datetime="{record["checked_at"]}"]')).to_be_visible()
            receipt = report.get_by_role("link", name="打开原始报告" if kind == "historical" else "打开回执摘要 JSON").first
            with self.page.expect_popup() as opened:
                receipt.click()
            opened.value.wait_for_load_state()
            path = record["source_url"] if kind == "historical" else "scripts/fixtures/runoob-observations.json"
            self.assertEqual(urlsplit(opened.value.url).path, "/autoSkill/" + path)
            self.assertIn(record["run_id"], opened.value.locator("body").inner_text())
            opened.value.close()
            self.page.go_back()
            expect(title).to_be_visible()

        title.click()
        self.page.get_by_role("dialog").get_by_role("button", name="运行这条用例", exact=True).click()
        self.page.wait_for_url("**/#/runs/demo-*")
        expect(self.page.locator(".run-summary h1")).to_contain_text("浏览器模拟")
        expect(self.page.locator("#view").get_by_role("region", name=source_name)).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
