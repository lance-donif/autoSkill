"""Status filters update their own regions without dimming or rebuilding the dashboard."""
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
        if status == "NotPassed":
            status = "Fail"
        record = self.result if path == "/api/results" else self.run
        count = 1 if query.get("current") else 4
        state = {"lifecycle": "completed", "phase": "blocked" if status == "Blocked" else "settled", "outcome": status.lower(),
                 "checks": {"business": "fail" if status == "Fail" else "unverified", "performance": "not_applicable", "quality": "not_applicable"},
                 "evidence": "valid", "report": "not_generated", "delivery": "not_applicable"}
        return {"total": count, "items": [{**record, "id": f"run-{status}-{i}", "status": status, "history_status": "Pass" if status == "Pass" else "NotPassed",
                                            "state": state, "case_title": f"{status} case {i}", "reason": status} for i in range(count)]}

    def dashboard_response(self, query):
        current = query.get("current") == ["true"]
        failed = self.history_response("/api/results", {**query, "status": ["Fail"]})
        if current:
            failures = failed
        else:
            blocked = self.history_response("/api/results", {**query, "status": ["Blocked"]})
            failures = {"total": failed["total"] + blocked["total"], "items": (failed["items"] + blocked["items"])[:6]}
        return {"summary": self.summary_response(), "recent": self.history_response("/api/runs", query), "failures": failures}

    def summary_response(self):
        return {"results": {"executions": 8, "passed": 0, "failed": 4, "blocked": 4, "still_failing": 1},
                "cases": {"total": 1, "ui": 1, "api": 0, "added": 0},
                "runs": {"total": 4, "scheduled": 0, "active": 0},
                "trend": {"unit": "day", "points": [{"bucket": "2026-11-12", "passed": 0, "failed": 4, "blocked": 4, "pass_rate": 0}]},
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
        self.page.get_by_role("button", name="范围内全部", exact=True).click()
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

    def test_binary_history_filters_keep_controls_and_back_navigation(self):
        self.page.goto("http://127.0.0.1:8787/#/runs?view=results&range=all")
        choices = self.page.get_by_role("group", name="用例结果", exact=True)
        expect(choices.get_by_role("button")).to_have_count(2)
        self.remember_regions(["#side", "#scope", "#view .page-head", "#view .toolbar", "#view section.card"])
        for status, name, title in (("Pass", "通过", "Pass case"), ("NotPassed", "不通过", "Fail case")):
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
        choices.get_by_role("button", name="不通过", exact=True).click()
        expect(self.page.locator("#view .table tbody tr").first).to_contain_text("Fail case")
        expect(choices.get_by_role("button", name="不通过", exact=True)).to_have_attribute("aria-pressed", "true")
        self.page.wait_for_function("document.getElementById('view').getAttribute('aria-busy') !== 'true'")
        self.assert_regions_unchanged()

    def test_static_preview_filters_examples_and_returns_to_the_same_result_list(self):
        origin = "http://preview.test/autoSkill/"
        requests, errors = [], []
        self.page.on("request", lambda request: requests.append((request.resource_type, request.url)))
        self.page.on("pageerror", lambda error: errors.append(str(error)))

        def serve_file(route):
            path = fixtures.ROOT / urlsplit(route.request.url).path.removeprefix("/autoSkill/")
            if path.is_dir():
                path = path / "index.html"
            if path.is_file():
                route.fulfill(path=path)
            else:
                route.fulfill(status=404)

        self.page.route(origin + "**", serve_file)
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.goto(origin + "preview/")
        toolbar = self.page.locator('[data-history-region="toolbar"]')
        rows = self.page.locator("#view tbody tr:visible")
        passed = toolbar.get_by_role("button", name="通过", exact=True)
        other = toolbar.get_by_role("button", name="不通过", exact=True)
        expect(toolbar.get_by_role("group", name="运行状态").get_by_role("button")).to_have_count(2)
        expect(toolbar.get_by_text("周期", exact=True)).to_have_count(0)
        expect(rows).to_have_count(3)
        passed.click()
        expect(rows).to_have_count(1)
        toolbar.get_by_role("button", name="按用例结果").click()
        expect(passed).to_have_attribute("aria-pressed", "true")
        expect(rows).to_have_count(2)
        expect(self.page.locator("[data-history-count]")).to_have_text("2")
        rows.locator('a[href*="run-fail.html"]').click()
        target = self.page.locator(".result:has(.result-head:target)")
        head = target.locator(".result-head")
        expect(head).to_be_focused()
        expect(head).to_have_attribute("aria-expanded", "true")
        head.click()
        expect(target.locator(".result-body")).to_be_hidden()
        head.press("Enter")
        expect(target.locator(".result-body")).to_be_visible()
        self.page.get_by_role("link", name="返回执行记录").click()
        expect(passed).to_have_attribute("aria-pressed", "true")
        expect(rows).to_have_count(2)
        other.click()
        expect(rows.locator("td:first-child .status")).to_have_text(["不通过", "不通过"])
        other.click()
        expect(rows).to_have_count(4)
        self.page.go_back()
        expect(other).to_have_attribute("aria-pressed", "true")
        expect(rows).to_have_count(2)
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(errors, [])
        self.assertFalse(any(kind in ("fetch", "xhr") or "/api/" in url for kind, url in requests))


if __name__ == "__main__":
    unittest.main()
