"""A native-only project with real execution history must retain dashboard visibility."""
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/autoskill/app'))

from test_data_dashboard import DataServer

from autoskill import native_assets, native_run, paths, projects


class NativeStateDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = paths.chrome()
        if not executable:
            raise unittest.SkipTest('Dashboard tests need an installed or bundled browser')
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
        cls.addClassCleanup(cls.browser.close)

    def native_fixture(self, body, expected, parameters=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        workspace = folder / 'workspace'
        environment = patch.dict(os.environ, {'AUTOSKILL_WORKSPACE': str(workspace), 'AUTOSKILL_CASE_SKILL': str(folder / 'sheet'),
                                               'AUTOSKILL_HEADED': '0', 'AUTOSKILL_BRIDGE': 'off', 'AUTOSKILL_SESSION': '',
                                               'AUTOSKILL_RUNTIME_CONTEXT': '', 'AUTOSKILL_WORKFLOW_BINDING': '',
                                               'AUTOSKILL_QUEUE_IDLE_SECONDS': '0.1'})
        environment.start()
        self.addCleanup(environment.stop)
        project = projects.create('Native-only project', 'http://native-only.example', project_id='native-only')
        source = folder / 'original'
        source.mkdir()
        self.native_source = source
        (source / 'test_original.py').write_text(body)
        asset = native_assets.import_asset(project['id'], str(source), 'selenium-unittest')
        run = native_run.enqueue(project['id'], asset['id'], parameters=parameters)
        native_run.execute(run['id'])
        self.assertEqual(native_run.get(run['id'])['status'], expected)
        self.assertEqual(projects.cases(project['id']), [])
        service = DataServer(workspace)
        self.addCleanup(service.close)
        self.addCleanup(self.wait_for_queue, workspace)
        self.assertEqual(service.api('/summary?cycle=current')['runs']['total'], 1)
        context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        self.addCleanup(context.close)
        page = context.new_page()
        return service, page, run

    def wait_for_queue(self, workspace):
        deadline = time.monotonic() + 15
        while (workspace / '.local/queue-worker.json').exists():
            if time.monotonic() >= deadline:
                self.fail('Isolated native queue did not finish before workspace cleanup')
            time.sleep(0.05)

    def test_native_history_is_visible_without_any_ir_cases(self):
        service, page, run = self.native_fixture('import unittest\nclass Original(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n', 'Pass')
        requests = []
        page.on('request', lambda request: requests.append(urlsplit(request.url).path))
        page.goto(service.origin + '/#/dashboard?range=all')
        expect(page.get_by_role('heading', name='最近执行', exact=True)).to_be_visible()
        expect(page.locator('#view')).to_contain_text('Native-only project')
        expect(page.get_by_text('还没有项目', exact=True)).to_have_count(0)
        expect(page.locator(f'.list-row[href*="{run["id"]}"] .status')).to_have_text('通过')
        self.assertIn('/api/dashboard', requests)
        self.assertNotIn('/api/summary', requests)
        self.assertNotIn('/api/results', requests)
        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/dashboard'
                                  and parse_qs(urlsplit(response.url).query).get('current') == ['true']):
            page.get_by_role('button', name='当前仍失败', exact=True).click()

    def test_native_rerun_api_keeps_frozen_asset_and_original_results(self):
        source = ('import os, unittest\nclass Original(unittest.TestCase):\n'
                  ' def test_ok(self): self.assertEqual(os.environ["FROZEN"], "original")\n'
                  ' def test_bad(self): self.assertEqual("actual", "expected")\n')
        parameters = {'env': {'FROZEN': 'original'}}
        service, page, old = self.native_fixture(source, 'Fail', parameters)
        frozen = native_run._frozen(native_run.get(old['id']))
        (self.native_source / 'test_original.py').write_text('import unittest\nclass Changed(unittest.TestCase):\n def test_changed(self): self.assertTrue(True)\n')
        changed = native_assets.import_asset('native-only', str(self.native_source), 'selenium-unittest', asset_id=frozen['asset_id'])
        self.assertNotEqual(changed['asset_version'], frozen['asset_version'])
        page.goto(service.origin + '/#/runs/' + old['id'] + '?range=all')
        expect(page.locator('.report-summary .status').first).to_have_text('不通过')
        expect(page.get_by_role('button', name='按原版本再次运行', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='只重跑这一条', exact=True)).to_have_count(0)
        created = service.api('/runs/' + old['id'] + '/rerun', method='POST', body={})
        self.assertNotEqual(created['id'], old['id'])
        self.assertEqual(created['family'], 'native')
        repeated = native_run._frozen(native_run.get(created['id']))
        self.assertEqual((repeated['asset_id'], repeated['head'], repeated['asset_version'], repeated['parameters']),
                         (frozen['asset_id'], frozen['head'], frozen['asset_version'], parameters))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            latest = service.api('/runs/' + created['id'])
            if latest['status'] not in ('Queued', 'Running'):
                break
            page.wait_for_timeout(50)
        else:
            self.fail('Native rerun did not complete')
        self.assertEqual(latest['total'], 2)
        self.assertEqual(len(latest['results']), 2)
        self.assertEqual(latest['status'], 'Fail')
        original = service.api('/runs/' + old['id'])
        self.assertEqual(original['status'], 'Fail')
        self.assertEqual(len(original['results']), 2)
        self.assertEqual(native_run._frozen(native_run.get(old['id'])), frozen)

    def test_report_downloads_read_back_and_recovery_does_not_execute(self):
        service, page, run = self.native_fixture(
            'import unittest\nclass Original(unittest.TestCase):\n def test_bad(self): self.assertEqual("actual", "expected")\n', 'Fail')
        before = service.api('/runs/' + run['id'])
        original_results = [row['id'] for row in before['results']]
        page.goto(service.origin + '/#/native-runs/' + run['id'])
        report = page.locator('[aria-label="本次报告"]')
        expect(report.get_by_role('button', name='下载 HTML 报告', exact=True)).to_be_visible()
        expect(report.get_by_role('link')).to_have_count(0)
        receipt = before['test_report']
        Path(receipt['excel']).write_bytes(b'broken workbook')
        damaged = page.request.get(service.origin + receipt['download_url'])
        self.assertFalse(damaged.ok)
        restored = service.api('/task-reports/' + receipt['report_id'] + '/retry', method='POST', body={})
        self.assertEqual(restored['status'], 'ready')
        for field in ('download_url', 'markdown_download_url', 'bugs_download_url'):
            response = page.request.get(service.origin + restored[field])
            self.assertTrue(response.ok)
            if field == 'download_url':
                book = load_workbook(io.BytesIO(response.body()))
                try:
                    self.assertEqual(book['功能测试用例'].max_column, 14)
                    self.assertIn('失败', str(list(book['功能测试用例'].values)))
                finally:
                    book.close()
            else:
                self.assertIn('失败', response.text())
        after = service.api('/runs/' + run['id'])
        self.assertEqual([row['id'] for row in after['results']], original_results)
        self.assertEqual(service.api('/runs?cycle=all')['total'], 1)


if __name__ == '__main__':
    unittest.main()
