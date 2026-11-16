"""Unpublished candidates can be verified again from their real historical dashboard result."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/autoskill/app'))

from test_data_dashboard import DataServer

from autoskill import builder, candidate_publication, case_document, db, execution_inputs, paths, plan, projects, runner


class CandidateTarget(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.calls += 1
        page = self.path == '/ui'
        body = (('<h1 data-testid="state">' + ('已就绪' if self.server.passing else '未就绪') + '</h1>').encode()
                if page else json.dumps({'ok': self.server.passing}).encode())
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if page else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class CandidateRerunDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=paths.chrome(), headless=True, args=['--no-sandbox'])
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'data'
        environment = patch.dict(os.environ, {'AUTOSKILL_WORKSPACE': str(self.workspace), 'AUTOSKILL_SESSION': 'candidate-rerun',
                                              'AUTOSKILL_CASE_SKILL': '', 'AUTOSKILL_HEADED': '0',
                                              'DISPLAY': '', 'WAYLAND_DISPLAY': '',
                                              'AUTOSKILL_BRIDGE': 'off', 'AUTOSKILL_RUNTIME_CONTEXT': '',
                                              'AUTOSKILL_WORKFLOW_BINDING': '', 'AUTOSKILL_QUEUE_IDLE_SECONDS': '0.1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.target = ThreadingHTTPServer(('127.0.0.1', 0), CandidateTarget)
        self.target.passing, self.target.calls = False, 0
        threading.Thread(target=self.target.serve_forever, daemon=True).start()
        self.addCleanup(self.target.server_close)
        self.addCleanup(self.target.shutdown)
        self.pid = projects.create('候选版本验证', f'http://127.0.0.1:{self.target.server_port}', project_id='candidate-rerun')['id']
        design = {'id': 'DESIGN-ORDER', 'title': '验证订单恢复可处理状态', 'module': '订单', 'preconditions': '独立本地测试服务已启动。',
                  'steps': ['查询本次订单状态。'], 'expected': '订单处于可处理状态，返回 ok 为 true。', 'source': '独立业务约定：订单恢复后应可处理。'}
        file = self.root / 'design.json'
        file.write_text(json.dumps([design], ensure_ascii=False), encoding='utf-8')
        self.written = plan.write(self.pid, str(file))
        row = plan.show()['cases'][0]
        case = builder.api_case('API-UNPUBLISHED', '尚未入库的订单候选', 'GET', '/result', {'status': 200, 'json_equals': {'/ok': True}})
        case['extensions']['execution'] = {'api_only': True}
        case_document.apply(case, {'description': row['title'], 'preconditions': row['preconditions'], 'steps': row['steps'],
                                   'expected': row['expected'], 'source': row['execution_source']})
        self.candidate = candidate_publication.stage(self.pid, [case], {}, {'fixture': 'dashboard-candidate-rerun'})
        queued = candidate_publication.queue(self.candidate['id'], start=False)
        self.original = runner.execute(queued['run_id'])
        self.assertEqual(self.original['status'], 'Fail')
        self.assertEqual(projects.cases(self.pid), [])
        self.service = DataServer(self.workspace)
        self.addCleanup(self.service.close)
        self.addCleanup(self.wait_for_worker)
        context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.rerun_requests = []
        self.page.on('request', lambda request: self.rerun_requests.append(request.url)
                     if request.method == 'POST' and request.url.endswith('/rerun') else None)

    def wait_for_worker(self):
        deadline = time.monotonic() + 20
        while (self.workspace / '.local/queue-worker.json').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse((self.workspace / '.local/queue-worker.json').exists(), 'Candidate worker must finish before cleanup')

    def assert_timeline_returns_to_trigger(self, reader, run_id):
        row = reader.locator('[data-case="DESIGN-ORDER"]')
        expect(row).to_be_visible()
        summary = row.locator('summary')
        if not summary.locator('..').evaluate('node => node.open'):
            summary.focus()
            summary.press('Enter')
        timeline = row.get_by_role('region', name='实际结果与复测记录', exact=True).locator(f'a[href*="{run_id}"]')
        overview = row.get_by_role('link', name='查看最新复测', exact=True)
        self.assertEqual(timeline.get_attribute('href'), overview.get_attribute('href'))
        for _attempt in range(3):
            timeline.focus()
            before = reader.evaluate('scrollY')
            timeline.press('Enter')
            expect(reader.locator('#view')).to_contain_text('本次综合通过')
            reader.evaluate('history.back()')
            expect(row).to_be_visible()
            expect(timeline).to_be_focused()
            expect(overview).not_to_be_focused()
            self.assertTrue(summary.locator('..').evaluate('node => node.open'))
            reader.wait_for_function('expected => Math.abs(scrollY - expected) < 2', arg=before)

    def test_unpublished_failure_reruns_its_frozen_candidate_and_keeps_original_failure(self):
        old_id = self.original['id']
        original_results = db.rows('SELECT id, status, detail FROM results WHERE run_id = ?', (old_id,))
        frozen = execution_inputs.load(db.one('SELECT * FROM runs WHERE id = ?', (old_id,)))['cases']
        self.page.goto(self.service.origin + '/#/runs/' + old_id)
        expect(self.page.get_by_role('button', name='按本次候选版本再次验证', exact=True)).to_be_visible()
        expect(self.page.get_by_role('button', name='按当前用例重新运行', exact=True)).to_have_count(0)
        expect(self.page.locator('#view')).to_contain_text('本次综合失败')
        self.target.passing = True
        with self.page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/rerun')) as response:
            self.page.get_by_role('button', name='再次验证已记录未通过的 1 条候选', exact=True).click()
        created = response.value.json()
        self.assertEqual(response.value.status, 200, created)
        self.assertEqual(created['candidate_id'], self.candidate['id'])
        self.assertEqual(created['run_ids'], [created['id']])
        self.assertNotEqual(created['id'], old_id)
        expect(self.page).to_have_url(self.service.origin + '/#/runs/' + created['id'])
        expect(self.page.locator('#view')).to_contain_text('使用本次候选版本')
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = self.service.api('/runs/' + created['id'])
            if current['status'] not in ('Queued', 'Running') and current['publication']['complete']:
                break
            self.page.wait_for_timeout(50)
        else:
            self.fail('Candidate verification did not finish')
        self.assertEqual(current['status'], 'Pass', current)
        self.assertEqual(current['publication']['published_ids'], ['API-UNPUBLISHED'])
        expect(self.page.locator('[aria-label="本批候选入库结果"]')).to_contain_text('本批候选已入库 1 条')
        expect(self.page.locator('[aria-label="本批候选入库结果"]')).to_contain_text('API-UNPUBLISHED')
        expect(self.page.get_by_role('link', name='查看本项目可复用用例', exact=True)).to_have_attribute('href', '#/cases?projects=' + self.pid)
        expect(self.page.locator('#view')).not_to_contain_text('已加入执行队列')
        expect(self.page.locator('#view')).not_to_contain_text('入队不等于验证通过')
        self.assertEqual(len(self.rerun_requests), 1)

        repeated = execution_inputs.load(db.one('SELECT * FROM runs WHERE id = ?', (created['id'],)))
        self.assertEqual(repeated['cases'], frozen)
        self.assertTrue(repeated['api_only'])
        self.assertEqual(db.rows('SELECT id, status, detail FROM results WHERE run_id = ?', (old_id,)), original_results)
        self.assertEqual(self.service.api('/runs/' + old_id)['status'], 'Fail')
        self.page.goto(self.service.origin + '/#/runs/' + old_id)
        expect(self.page.locator('#view')).to_contain_text('本次综合失败')
        self.assertGreaterEqual(self.target.calls, 2)
        destination = self.service.origin + '/?embed=1#/case-plans?project=' + self.pid + '&session=candidate-rerun'
        self.page.goto(destination.replace('/?embed=1#/', '/#/'))
        self.assert_timeline_returns_to_trigger(self.page, created['id'])
        self.page.goto(self.service.origin + '/api/status')
        self.page.set_viewport_size({'width': 700, 'height': 700})
        self.page.set_content('<iframe name="host" style="width:390px;height:500px;border:0" src="' + destination + '"></iframe>')
        reader = self.page.frame(name='host')
        row = reader.locator('[data-case="DESIGN-ORDER"]')
        expect(row).to_contain_text('本任务曾失败')
        expect(row).to_contain_text('最新复测：通过')
        expect(row).to_contain_text('本次版本已入库')
        result_id = current['results'][0]['id']
        expect(row.get_by_role('link', name='查看原失败', exact=True)).to_have_attribute('href', '#/runs/' + old_id + '?case=result%3A' + str(original_results[0]['id']))
        expect(row.get_by_role('link', name='查看最新复测', exact=True)).to_have_attribute('href', '#/runs/' + created['id'] + '?case=result%3A' + str(result_id))
        row.locator('summary').click()
        history = row.get_by_role('region', name='实际结果与复测记录', exact=True)
        expect(history).to_contain_text('第 1 次执行失败')
        expect(history).to_contain_text('第 2 次执行通过')
        self.assertEqual(reader.evaluate('document.documentElement.scrollWidth'), 390)
        self.assert_timeline_returns_to_trigger(reader, created['id'])
        output = os.environ.get('AUTOSKILL_VALIDATION_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(output) / 'candidate-retest-original-plan.png'), full_page=True)
        shown = plan.show()
        self.assertEqual(shown['task_id'], self.written['task_id'])
        self.assertEqual(shown['delivery_excel'], self.written['delivery_excel'])
        self.assertEqual(shown['cases'][0]['status'], '失败')
        book = load_workbook(shown['delivery_excel'])
        self.addCleanup(book.close)
        self.assertEqual(book['功能测试用例'].max_column, 14)
        cells = '\n'.join(str(cell.value or '') for sheet in book for cells in sheet for cell in cells)
        self.assertIn(old_id, cells)
        self.assertIn(created['id'], cells)
        self.assertIn('通过', cells)
        self.assertIn('失败', cells)
        linked = runner.create(self.pid, ['API-UNPUBLISHED'])
        self.assertEqual(runner.execute(linked['id'])['status'], 'Pass')
        latest = self.service.api('/case-plans/' + self.pid + '?session=candidate-rerun')['rows'][0]['evidence']['latest_execution']
        self.assertEqual(latest['run_id'], linked['id'])
        current_case = projects.case(self.pid, 'API-UNPUBLISHED')
        current_case['title'] += ' 新定义'
        with self.page.expect_response(lambda response: '/api/case-plans/' + self.pid in response.url):
            projects.save_cases(self.pid, [current_case])
        expect(row).to_contain_text('当前定义尚未验证，需重新执行')
        expect(row).to_contain_text('关联用例已修改或不再有效')
        expect(row).to_contain_text('历史执行：通过')
        expect(row).not_to_contain_text('最新复测：')
        expect(row).not_to_contain_text('本次版本已入库')
        history_link = row.get_by_role('link', name='查看历史执行', exact=True)
        expect(history_link).to_have_attribute('href', '#/runs/' + linked['id'] + '?case=result%3A' + str(latest['result_id']))
        expect(row.get_by_role('link', name='查看原失败', exact=True)).to_be_visible()
        history_link.click()
        expect(reader.locator('#view')).to_contain_text('本次综合通过')
        self.assertEqual(len(self.rerun_requests), 1)

    def test_first_ui_candidate_retest_uses_server_headless_default_without_saving_it(self):
        settings = self.service.api('/settings')
        self.assertEqual(settings['dashboard'], {})
        self.assertFalse(settings['dashboard_defaults']['headed'])
        case = builder.ui_case('UI-DEFAULT', '服务恢复后显示已就绪', {
            'start_url': '/ui', 'actions': [], 'assertions': [
                {'assertion': 'text', 'locators': [{'by': 'testid', 'value': 'state'}], 'expected': '已就绪', 'timeout_seconds': 1}]},
            test_case_document={'description': '验证服务恢复后显示就绪状态。', 'preconditions': '本地服务已经启动。',
                                'steps': ['打开服务页面并查看就绪状态。'], 'expected': '页面显示“已就绪”。',
                                'source': '独立需求：服务恢复后显示已就绪。'})
        candidate = candidate_publication.stage(self.pid, [case], {}, {'fixture': 'first-ui-retest-default'})
        queued = candidate_publication.queue(candidate['id'])
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            first = self.service.api('/runs/' + queued['run_id'])
            if first['status'] not in ('Queued', 'Running') and first['publication']['complete']:
                break
            self.page.wait_for_timeout(50)
        else:
            self.fail('Original UI failure did not finish')
        self.assertEqual(first['status'], 'Fail', first)
        self.page.goto(self.service.origin + '/#/runs/' + queued['run_id'])
        expect(self.page.locator('#view')).to_contain_text('本次综合失败')
        self.target.passing = True
        with self.page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/rerun')) as response:
            self.page.get_by_role('button', name='再次验证已记录未通过的 1 条候选', exact=True).click()
        self.assertEqual(response.value.request.post_data_json, {'headed': False, 'failed_only': True})
        created = response.value.json()
        self.assertEqual(response.value.status, 200, created)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = self.service.api('/runs/' + created['id'])
            if current['status'] not in ('Queued', 'Running') and current['publication']['complete']:
                break
            self.page.wait_for_timeout(50)
        else:
            self.fail('First UI retest did not finish')
        self.assertEqual(current['status'], 'Pass', current)
        self.assertEqual(current['publication']['published_ids'], ['UI-DEFAULT'])
        expect(self.page.locator('#view')).to_contain_text('本次综合通过')
        self.assertFalse(db.one('SELECT headed FROM runs WHERE id = ?', (created['id'],))['headed'])
        self.assertEqual(len(self.rerun_requests), 1)
        self.assertEqual(self.service.api('/settings')['dashboard'], {})
        if output := os.environ.get('AUTOSKILL_VALIDATION_OUTPUT'):
            Path(output).mkdir(parents=True, exist_ok=True)
            (Path(output) / 'first-ui-retest-default.json').write_text(json.dumps({
                'post_count': len(self.rerun_requests), 'body': response.value.request.post_data_json,
                'old_status': first['status'], 'new_status': current['status'], 'publication': current['publication'],
                'settings': self.service.api('/settings')}, ensure_ascii=False, indent=2), encoding='utf-8')
        self.service.api('/settings', method='PUT', body={'dashboard': {'headed': True}, 'expected_data_id': settings['data_id']})
        self.page.reload()
        self.page.get_by_role('button', name='回放设置', exact=True).click()
        expect(self.page.get_by_role('switch', name='显示浏览器窗口', exact=True)).to_have_attribute('aria-checked', 'true')
        self.assertTrue(self.service.api('/settings')['dashboard']['headed'])
