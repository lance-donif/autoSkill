"""Startup metadata cannot hide real content or override saved replay preferences."""
import unittest

import test_dashboard_review_fixes as fixtures
from playwright.sync_api import expect


class DashboardStartupTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DashboardReviewTests.setUpClass.__func__)
    handle_request = fixtures.DashboardReviewTests.handle_request
    history_response = fixtures.DashboardReviewTests.history_response

    def setUp(self):
        fixtures.DashboardReviewTests.setUp(self)
        self.started = []

    def api_response(self, request, path, query):
        if path == '/api/settings':
            return {'data_id': 'startup-fixture', 'dashboard': {'engine': 'selenium', 'headed': False},
                    'legacy_dashboard_import_eligible': False}
        if path == '/api/runs' and request.method == 'POST':
            self.started.append(request.post_data_json)
            return {'runs': [{'id': 'run-1', 'project_id': 'review'}], 'skipped': []}
        return fixtures.DashboardReviewTests.api_response(self, request, path, query)

    def hold_metadata(self, path):
        self.page.add_init_script("""(() => {
            const original = window.fetch.bind(window);
            const gate = new Promise(resolve => { window.releaseMetadata = resolve; });
            window.fetch = (url, options) => String(url) === PATH
                ? gate.then(() => original(url, options)) : original(url, options);
        })();""".replace('PATH', repr(path)))

    def open_cases(self):
        self.page.goto('http://127.0.0.1:8787/#/cases?range=all', wait_until='domcontentloaded')
        expect(self.page.locator('#view .table tbody')).to_contain_text('Example case', timeout=5000)

    def test_slow_runtime_metadata_does_not_hide_cases_or_navigation(self):
        self.hold_metadata('/api/status')
        self.open_cases()
        self.page.get_by_role('link', name='执行记录', exact=True).click()
        expect(self.page.locator('#view .table tbody')).to_contain_text('Original project', timeout=5000)

    def test_slow_preferences_do_not_hide_cases_and_launch_waits_for_saved_values(self):
        self.hold_metadata('/api/settings')
        self.open_cases()
        self.page.get_by_role('button', name='运行当前显示的 1 条', exact=True).click()
        expect(self.page.get_by_role('button', name='正在启动', exact=True)).to_be_disabled()
        self.assertEqual(self.started, [])
        self.page.evaluate('window.releaseMetadata()')
        expect(self.page).to_have_url('http://127.0.0.1:8787/#/runs/run-1?range=all')
        self.assertEqual(self.started, [{'items': [{'project_id': 'review', 'case_ids': ['TC-UI-001']}],
                                       'engine': 'selenium', 'headed': False}])

    def test_slow_initial_revision_does_not_hide_results_or_lose_startup_changes(self):
        self.hold_metadata('/api/state-revision')
        self.empty_history = True
        self.page.goto('http://127.0.0.1:8787/#/runs?range=all', wait_until='domcontentloaded')
        expect(self.page.get_by_text('这个范围内还没有执行', exact=True)).to_be_visible(timeout=5000)
        self.empty_history = False
        self.revision = 'written-during-startup'
        self.page.evaluate('window.releaseMetadata()')
        expect(self.page.locator('#view .table tbody')).to_contain_text('Original project', timeout=1000)


if __name__ == '__main__':
    unittest.main()
