"""Startup metadata cannot hide real content or override saved replay preferences."""
import json
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

    def hold_request(self, path, **query):
        self.page.add_init_script("""((target) => {
            const original = window.fetch.bind(window);
            const gate = new Promise(resolve => { window.releaseRequest = resolve; });
            window.fetch = (url, options) => {
                const address = new URL(url, location.href);
                const held = address.pathname === target.path && Object.entries(target.query)
                    .every(([key, value]) => address.searchParams.get(key) === value);
                return held ? gate.then(() => original(url, options)) : original(url, options);
            };
        })(TARGET);""".replace('TARGET', json.dumps({'path': path, 'query': query})))

    def open_cases(self):
        self.page.goto('http://127.0.0.1:8787/#/cases?range=all', wait_until='domcontentloaded')
        expect(self.page.locator('#view .table tbody')).to_contain_text('Example case', timeout=5000)

    def test_slow_runtime_metadata_does_not_hide_cases_or_navigation(self):
        self.hold_request('/api/status')
        self.open_cases()
        self.page.get_by_role('link', name='执行记录', exact=True).click()
        expect(self.page.locator('#view .table tbody')).to_contain_text('Original project', timeout=5000)

    def test_fresh_navigation_replaces_old_run_controls_while_destination_is_pending(self):
        self.hold_request('/api/runs', limit='50')
        self.open_cases()
        action = self.page.get_by_role('button', name='运行当前显示的 1 条', exact=True)
        history = self.page.get_by_role('link', name='执行记录', exact=True)
        expect(action).to_be_visible()
        old_action = action.element_handle()

        history.click()
        expect(history).to_have_attribute('aria-current', 'page')
        expect(self.page.locator('#view')).to_have_attribute('aria-busy', 'true')
        expect(action).to_have_count(0)
        self.assertFalse(old_action.evaluate('node => node.isConnected'))
        expect(self.page.locator('#view').get_by_text('正在加载页面…', exact=True)).to_be_visible()
        expect(self.page.get_by_role('status').filter(has_text='正在加载页面…')).to_have_count(1)
        self.assertEqual(self.started, [])

        self.page.go_back()
        expect(self.page.get_by_role('link', name='可复用用例', exact=True)).to_have_attribute('aria-current', 'page')
        expect(action).to_be_visible()
        expect(self.page.locator('#view')).not_to_have_attribute('aria-busy', 'true')

        history.click()
        expect(action).to_have_count(0)
        self.page.evaluate('window.releaseRequest()')
        expect(self.page.locator('#view h1')).to_have_text('执行记录')
        expect(self.page.get_by_role('status').filter(has_text='正在加载页面…')).to_have_count(0)

    def test_slow_preferences_do_not_hide_cases_and_launch_waits_for_saved_values(self):
        self.hold_request('/api/settings')
        self.open_cases()
        self.page.get_by_role('button', name='运行当前显示的 1 条', exact=True).click()
        expect(self.page.get_by_role('button', name='正在启动', exact=True)).to_be_disabled()
        self.assertEqual(self.started, [])
        self.page.evaluate('window.releaseRequest()')
        expect(self.page).to_have_url('http://127.0.0.1:8787/#/runs/run-1?range=all')
        self.assertEqual(self.started, [{'items': [{'project_id': 'review', 'case_ids': ['TC-UI-001']}],
                                       'engine': 'selenium', 'headed': False}])

    def test_slow_initial_revision_does_not_hide_results_or_lose_startup_changes(self):
        self.hold_request('/api/state-revision')
        self.empty_history = True
        self.page.goto('http://127.0.0.1:8787/#/runs?range=all', wait_until='domcontentloaded')
        expect(self.page.get_by_text('这个范围内还没有执行', exact=True)).to_be_visible(timeout=5000)
        self.empty_history = False
        self.revision = 'written-during-startup'
        self.page.evaluate('window.releaseRequest()')
        expect(self.page.locator('#view .table tbody')).to_contain_text('Original project', timeout=1000)


if __name__ == '__main__':
    unittest.main()
