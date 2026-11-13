"""Status filters update their own regions without dimming or rebuilding the dashboard."""
import sys
import unittest
from pathlib import Path

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
        count = 1 if query.get("current") else 4
        return {"total": count, "items": [{**record, "id": f"run-{status}-{i}", "status": status,
                                            "case_title": f"{status} case {i}", "reason": status} for i in range(count)]}

    def api_response(self, request, path, query):
        self.requests.append((path, query))
        if path == "/api/summary":
            return {"results": {"executions": 8, "passed": 0, "failed": 4, "blocked": 4, "still_failing": 1},
                    "cases": {"total": 1, "ui": 1, "api": 0, "added": 0},
                    "runs": {"total": 4, "scheduled": 0, "active": 0},
                    "trend": {"unit": "day", "points": [{"bucket": "2026-11-12", "passed": 0, "failed": 4, "blocked": 4, "pass_rate": 0}]},
                    "tasks": {"total": 0, "enabled": 0, "paused": 0, "fired_in_range": 0, "tasks": []}}
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
        self.page.get_by_role("button", name="当前仍失败", exact=True).click()
        expect(self.page.locator("[data-dashboard-failures] .list-row")).to_have_count(1)
        self.assert_regions_unchanged()
        self.assertEqual(sum(path == "/api/summary" for path, _ in self.requests), summaries)
        self.assertEqual(sum(path == "/api/runs" for path, _ in self.requests), runs)
        self.page.get_by_role("button", name="范围内全部", exact=True).click()
        expect(self.page.locator("[data-dashboard-failures] .list-row")).to_have_count(6)
        self.assert_regions_unchanged()
        self.remember_regions(["#side", "#scope", "#view .page-head", "#view .kpis", "[data-dashboard-failures]"])
        self.page.get_by_role("button", name="表格", exact=True).click()
        expect(self.page.locator(".chart-table")).to_be_visible()
        self.page.get_by_role("button", name="图表", exact=True).click()
        expect(self.page.locator(".chart-svg")).to_be_visible()
        self.assert_regions_unchanged()

    def test_failed_and_blocked_history_filters_keep_controls_and_back_navigation(self):
        self.page.goto("http://127.0.0.1:8787/#/runs?view=results&range=all")
        expect(self.page.get_by_role("group", name="结果筛选")).to_be_visible()
        self.remember_regions(["#side", "#scope", "#view .page-head", "#view .toolbar", "#view section.card"])
        filters = self.page.get_by_role("group", name="结果筛选")
        for status, label in (("Fail", "失败"), ("Blocked", "阻塞")):
            filters.get_by_role("button", name=label, exact=True).click()
            expect(self.page.locator("#view .table tbody tr").first).to_contain_text(f"{status} case")
            expect(filters.get_by_role("button", name=label, exact=True)).to_have_attribute("aria-pressed", "true")
            self.assertIn(f"status={status}", self.page.url)
            self.assert_regions_unchanged()
        self.page.go_back()
        expect(self.page.locator("#view .table tbody tr").first).to_contain_text("Fail case")
        expect(filters.get_by_role("button", name="失败", exact=True)).to_have_attribute("aria-pressed", "true")
        self.assert_regions_unchanged()

    def test_slow_status_response_does_not_dim_and_latest_filter_wins(self):
        self.page.goto("http://127.0.0.1:8787/#/runs?view=results&range=all")
        expect(self.page.get_by_role("group", name="结果筛选")).to_be_visible()
        self.page.evaluate("""() => {
            const fetchNow = window.fetch.bind(window);
            window.fetch = async (...args) => {
                if (String(args[0]).includes('status=Fail')) await new Promise(resolve => setTimeout(resolve, 300));
                return fetchNow(...args);
            };
        }""")
        self.remember_regions(["#side", "#scope", "#view .toolbar"])
        filters = self.page.get_by_role("group", name="结果筛选")
        filters.get_by_role("button", name="失败", exact=True).click()
        expect(self.page.locator("#view")).to_have_attribute("aria-busy", "true")
        self.assert_regions_unchanged()
        filters.get_by_role("button", name="阻塞", exact=True).click()
        expect(self.page.locator("#view .table tbody tr").first).to_contain_text("Blocked case")
        self.page.wait_for_function("document.getElementById('view').getAttribute('aria-busy') !== 'true'")
        self.assert_regions_unchanged()


if __name__ == "__main__":
    unittest.main()
