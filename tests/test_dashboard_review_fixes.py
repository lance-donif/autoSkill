"""Dashboard filtering and saved settings update the real UI against isolated API fixtures."""
import json
import mimetypes
import re
import sys
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "skills/autoskill/app/ui"
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import paths


class DashboardReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = paths.chrome()
        if not executable:
            raise unittest.SkipTest("Dashboard tests need an installed or bundled browser")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.project = {
            "id": "review", "name": "Original project", "category": "", "base_url": "http://example.invalid",
            "cases": {"total": 1, "ui": 1, "api": 0}, "login": {"ui_case": None, "api_case": None},
            "missing_secrets": ["REVIEW_SECRET"], "credentials": {"REVIEW_SECRET": None}, "session": {"saved": False, "saved_at": None},
        }
        self.case = {"project_id": "review", "case_id": "TC-UI-001", "title": "Example case", "kind": "ui",
                     "module": "Example module", "depends_on": "[]", "last_status": None, "last_run_at": None}
        self.run = {"id": "run-1", "project_name": "Original project", "status": "Pass", "total": 1, "passed": 1,
                    "failed": 0, "blocked": 0, "engine": "playwright", "trigger": "manual", "created_at": "2026-11-12T00:00:00Z"}
        self.result = {"run_id": "run-1", "case_id": "TC-UI-001", "case_title": "Example case", "kind": "ui", "status": "Pass",
                       "project_name": "Original project", "started_at": "2026-11-12T00:00:00Z", "duration_ms": 1}
        self.empty_history = False
        self.revision = 'initial'
        self.page.route("**/*", self.handle_request)

    def history_response(self, path, query):
        if query.get("status") == ["active"]:
            return {"total": 0, "items": []}
        total = 0 if self.empty_history else 1 if query.get("projects") or query.get("start") else 60
        offset = int(query.get("offset", ["0"])[0])
        record = self.result if path == "/api/results" else self.run
        return {"total": total, "items": [record] if offset < total else []}

    def api_response(self, request, path, query):
        if path == "/api/projects":
            return [self.project]
        if path == "/api/settings":
            return {"data_id": "fixture-data", "dashboard": {}, "legacy_dashboard_import_eligible": False}
        if path == "/api/status":
            return {"browser": {}, "runtime": {}, "skill_path": "/example/skill"}
        if path == "/api/state-revision":
            return {"revision": self.revision}
        if path == "/api/projects/review":
            if request.method == "PATCH":
                self.project.update(request.post_data_json)
            return self.project
        if path == "/api/secrets/REVIEW_SECRET":
            self.project["credentials"]["REVIEW_SECRET"] = request.post_data_json["value"]
            self.project["missing_secrets"] = []
            return {"saved": "REVIEW_SECRET"}
        if path == "/api/cases":
            return {"items": [self.case], "total": 1}
        if path == "/api/projects/review/folders":
            return {"project_id": "review", "revision": "fixture-folders", "folders": [], "assignments": {self.case["case_id"]: None}}
        if path in ("/api/runs", "/api/results"):
            return self.history_response(path, query)
        if path == '/api/runs/run-1':
            return self.run
        raise AssertionError("Unexpected API request: " + path)

    def handle_request(self, route):
        url = urlsplit(route.request.url)
        if url.path.startswith("/api/"):
            data = self.api_response(route.request, url.path, parse_qs(url.query))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
            return
        file = UI / (url.path.lstrip("/") or "index.html")
        if file.is_file():
            media_type = "text/javascript" if file.suffix == ".js" else mimetypes.guess_type(str(file))[0] or "text/plain"
            route.fulfill(status=200, content_type=media_type, body=file.read_bytes())
        else:
            route.fulfill(status=404, body="Not found")

    def open_history(self, query):
        self.page.goto("http://127.0.0.1:8787/#/runs?" + query)
        expect(self.page.locator("#view h1")).to_have_text("执行记录")

    def test_project_and_time_filters_reset_pagination_for_runs_and_results(self):
        for mode in ("", "view=results&"):
            for picker, choice in (("筛选项目", "Original project"), ("筛选时间范围", "今天")):
                with self.subTest(mode=mode, filter=picker):
                    self.open_history(mode + "range=all&offset=50")
                    expect(self.page.get_by_role("button", name="上一页", exact=True)).to_be_visible()
                    self.page.locator("#scope").get_by_role("button", name=picker, exact=True).click()
                    self.page.get_by_role("menuitemcheckbox", name=choice, exact=True).click()
                    expect(self.page.locator("#view .table tbody tr")).to_have_count(1)
                    expect(self.page.locator("#view .page-head p")).to_contain_text("共 1")
                    self.assertNotIn("offset", parse_qs(urlsplit(self.page.url).fragment.partition("?")[2]))

    def test_saved_links_with_an_out_of_range_offset_recover_without_losing_filters(self):
        for mode in ("", "view=results&"):
            with self.subTest(mode=mode):
                self.open_history(mode + "projects=review&range=all&status=Pass&offset=50")
                expect(self.page).not_to_have_url(re.compile(r"[?&]offset="))
                expect(self.page.locator("#view .table tbody tr")).to_have_count(1)
                query = parse_qs(urlsplit(self.page.url).fragment.partition("?")[2])
                self.assertNotIn("offset", query)
                self.assertEqual((query["projects"], query["range"], query["status"]), (["review"], ["all"], ["Pass"]))
                self.assertEqual(query.get("view"), ["results"] if mode else None)

    def test_empty_history_also_recovers_from_a_saved_page_offset(self):
        self.empty_history = True
        self.open_history("projects=review&range=all&offset=50")
        expect(self.page.get_by_text("这个范围内还没有执行", exact=True)).to_be_visible()
        self.assertNotIn("offset", parse_qs(urlsplit(self.page.url).fragment.partition("?")[2]))

    def test_saved_project_name_and_category_appear_immediately(self):
        self.page.goto("http://127.0.0.1:8787/#/cases?projects=review&range=all")
        self.page.get_by_role("button", name="「Original project」设置").click()
        self.page.locator("#ps-name").fill("Renamed project")
        self.page.locator("#ps-cat").fill("Updated category")
        self.page.locator(".drawer-foot").get_by_role("button", name="保存", exact=True).click()
        expect(self.page.locator("#view h2")).to_contain_text("Renamed project")
        expect(self.page.locator("#view h2 .tag")).to_have_text("Updated category")
        self.assertEqual(self.project["name"], "Renamed project")
        self.assertIn("projects=review", self.page.url)

    def test_saving_a_credential_refreshes_the_missing_credential_banner(self):
        self.page.goto("http://127.0.0.1:8787/#/cases")
        expect(self.page.locator("#view .banner")).to_contain_text("REVIEW_SECRET")
        self.page.get_by_role("button", name="「Original project」设置").click()
        self.page.get_by_role("textbox", name="REVIEW_SECRET", exact=True).fill("synthetic-test-value")
        self.page.locator(".drawer-body").get_by_role("button", name="保存", exact=True).click()
        expect(self.page.locator("#view .banner")).to_have_count(0)
        expect(self.page.get_by_role("dialog", name="项目设置", exact=True)).to_be_visible()

    def test_idle_external_completion_updates_within_one_second(self):
        self.empty_history = True
        self.open_history('projects=review&range=all')
        expect(self.page.get_by_text('这个范围内还没有执行', exact=True)).to_be_visible()
        self.run['state'] = {'lifecycle': 'completed', 'phase': 'settled', 'outcome': 'fail',
                             'checks': {'business': 'pass', 'performance': 'fail', 'quality': 'not_applicable'},
                             'evidence': 'valid', 'report': 'failed', 'delivery': 'pending'}
        self.run['history_status'] = 'NotPassed'
        self.empty_history = False
        started = time.monotonic()
        self.revision = 'external-finished'
        verdict = self.page.locator('#view .table tbody .status').first
        expect(verdict).to_have_text('不通过', timeout=1000)
        self.assertLess(time.monotonic() - started, 1.0)
        expect(verdict).to_have_attribute('title', '失败')

    def test_refresh_preserves_focused_search_draft_and_selection(self):
        self.page.goto('http://127.0.0.1:8787/#/cases?projects=review&range=all')
        search = self.page.get_by_role('searchbox', name='搜索用例')
        search.fill('Example')
        original = search.element_handle()
        search.evaluate('(node) => node.setSelectionRange(2, 5)')
        self.case['state'] = {'phase': 'settled', 'outcome': 'unverified',
                              'checks': {'business': 'unverified', 'performance': 'not_applicable', 'quality': 'not_applicable'}}
        self.revision = 'case-result-finished'
        expect(self.page.locator('#view')).to_contain_text('业务：未验证', timeout=1000)
        expect(search).to_be_focused()
        expect(search).to_have_value('Example')
        self.assertEqual(search.evaluate('(node) => [node.selectionStart,node.selectionEnd]'), [2, 5])
        self.assertTrue(original.evaluate('(node) => node.isConnected'))
        search.fill('no matching fixture case')
        expect(self.page.get_by_text('没有符合条件的用例', exact=True)).to_be_visible()
        search.fill('Example')
        expect(self.page.locator('#view .table tbody tr')).to_have_count(1)

    def test_refresh_preserves_scroll_region_identity_and_horizontal_position(self):
        self.page.set_viewport_size({'width': 760, 'height': 1000})
        self.open_history('projects=review&range=all')
        region = self.page.get_by_role('region', name='执行记录表格', exact=True)
        original = region.element_handle()
        before = region.evaluate('(node) => { node.scrollLeft = node.scrollWidth; return node.scrollLeft; }')
        self.assertGreater(before, 0)
        self.run['state'] = {'phase': 'settled', 'outcome': 'fail',
                             'checks': {'business': 'pass', 'performance': 'fail', 'quality': 'not_applicable'}}
        self.run['history_status'] = 'NotPassed'
        self.revision = 'result-updated-while-scrolled'
        expect(region.locator('tbody .status').first).to_have_text('不通过', timeout=1000)
        self.assertTrue(original.evaluate('(node) => node.isConnected'))
        self.assertEqual(region.evaluate('(node) => node.scrollLeft'), before)

    def test_history_toolbar_and_removable_bookmark_filters_follow_the_url(self):
        self.open_history('projects=review&range=all&cycle=all&phase=settled&business=pass')
        toolbar = self.page.locator('[data-history-region="toolbar"]')
        expect(toolbar.get_by_text('周期', exact=True)).to_have_count(0)
        expect(toolbar.get_by_role('group', name='运行状态').get_by_role('button')).to_have_count(2)
        phase = self.page.get_by_role('button', name='清除执行阶段：已结束')
        expect(phase).to_be_visible()
        self.page.get_by_role('button', name='不通过', exact=True).click()
        expect(self.page).to_have_url(re.compile(r'status=NotPassed'))
        self.page.get_by_role('button', name='按用例结果', exact=True).click()
        expect(self.page).to_have_url(re.compile(r'view=results'))
        status = toolbar.get_by_role('group', name='用例结果')
        expect(status.get_by_role('button', name='不通过', exact=True)).to_have_attribute('aria-pressed', 'true')
        self.assertIn('cycle=all', self.page.url)
        phase.click()
        expect(phase).to_have_count(0)
        self.assertNotIn('phase=', self.page.url)
        self.assertIn('business=pass', self.page.url)
        self.page.go_back()
        expect(phase).to_be_visible()
        self.page.evaluate("location.hash = '#/runs?view=results&status=Unverified'")
        legacy = self.page.get_by_role('button', name='清除状态：未验证')
        expect(legacy).to_be_visible()
        expect(status.get_by_role('button')).to_have_count(2)
        status.get_by_role('button', name='通过', exact=True).click()
        expect(legacy).to_have_count(0)
        self.page.go_back()
        expect(legacy).to_be_visible()
        legacy.click()
        expect(status.get_by_role('button', name='通过', exact=True)).to_be_focused()

    def test_workspace_revision_keeps_inflight_diagnostic_and_its_result(self):
        original_api_response = self.api_response
        commands = []
        finished = False

        def diagnostic_response(request, path, query):
            if path == '/api/engines':
                return {'engines': [{'id': 'ai-eval', 'name': '合成诊断执行器'}]}
            if path == '/api/diagnostics':
                commands.append(request.post_data_json)
                return {'id': 'diagnostic-owned', 'status': 'Running'}
            if path == '/api/diagnostics/diagnostic-owned':
                return {'id': 'diagnostic-owned', 'status': 'Completed' if finished else 'Running',
                        'result': {'ok': False, 'modules': {'playwright': True}, 'missing': ['synthetic-runtime']}}
            return original_api_response(request, path, query)

        self.api_response = diagnostic_response
        self.page.goto('http://127.0.0.1:8787/#/diagnostics')
        self.page.get_by_role('button', name='检查运行环境', exact=True).click()
        expect(self.page.locator('#view')).to_contain_text('diagnostic-owned')
        with self.page.expect_response(lambda response: urlsplit(response.url).path == '/api/state-revision'
                                       and response.json()['revision'] == 'unrelated-during-diagnostic'):
            self.revision = 'unrelated-during-diagnostic'
        finished = True
        expect(self.page.get_by_role('table', name='Python 依赖状态', exact=True)).to_be_visible(timeout=1000)
        expect(self.page.locator('#view')).to_contain_text('synthetic-runtime')
        self.assertEqual(commands, [{'kind': 'doctor'}])

    def test_unverified_raw_pass_keeps_ir_subset_and_single_case_rerun_visible(self):
        state = {'phase': 'settled', 'lifecycle': 'completed', 'outcome': 'unverified',
                 'checks': {'business': 'unverified', 'performance': 'not_applicable', 'quality': 'not_applicable'}}
        self.run.update(family='replay', project_id='review', state=state, results=[{
            **self.result, 'id': 1, 'state': state, 'detail': {}, 'prerequisite': False,
            'category': '', 'reason': '缺少独立业务断言', 'actual': ''}])
        self.page.goto('http://127.0.0.1:8787/#/runs/run-1?range=all')
        expect(self.page.get_by_role('button', name='重跑已记录未通过的 1 条', exact=True)).to_be_visible()
        expect(self.page.get_by_role('button', name='只重跑这一条', exact=True)).to_be_visible()
        expect(self.page.get_by_role('button', name='按当前用例重新运行', exact=True)).to_be_visible()

    def test_primary_results_do_not_wait_for_slow_runtime_metadata(self):
        self.empty_history = True
        self.open_history('projects=review&range=all')
        expect(self.page.get_by_text('这个范围内还没有执行', exact=True)).to_be_visible()
        self.page.evaluate('''() => {
            const original = window.fetch;
            window.fetch = (url, options) => String(url) === '/api/status'
                ? new Promise(resolve => setTimeout(resolve, 1500)).then(() => original(url, options))
                : original(url, options);
        }''')
        self.run['state'] = {'phase': 'settled', 'outcome': 'unverified',
                             'checks': {'business': 'unverified', 'performance': 'not_applicable', 'quality': 'not_applicable'}}
        self.run['history_status'] = 'NotPassed'
        self.empty_history = False
        self.revision = 'completed-with-slow-runtime'
        verdict = self.page.locator('#view .table tbody .status').first
        expect(verdict).to_have_text('不通过', timeout=1000)
        expect(verdict).to_have_attribute('title', '未验证')


if __name__ == "__main__":
    unittest.main()
