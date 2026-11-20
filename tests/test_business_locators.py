"""Recorded business identities survive real list changes without acting on another row."""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from autoskill import paths, projects, runner, task_context
from autoskill.recorder import session
from autoskill.replay import codegen, ir, ui
from autoskill.replay.playwright_driver import DOM_JS, PlaywrightDriver
from autoskill.replay.selenium_driver import SeleniumDriver


class BusinessSite:
    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                settings = json.dumps({'rows': owner.rows, 'table': owner.table, 'delay': owner.delay}, ensure_ascii=False)
                page = '''<!doctype html><meta charset="utf-8"><div class="items"></div><output id="result">未操作</output>
<script>
const settings = SETTINGS;
const list = document.querySelector('.items');
if (settings.table) list.innerHTML = '<table><tbody></tbody></table>';
function append(item) {
  const row = document.createElement(settings.table ? 'tr' : 'div');
  row.className = 'entry';
  if (item.hidden) row.hidden = true;
  if (item.sku) row.setAttribute('data-sku', item.sku);
  const title = document.createElement(settings.table ? 'td' : 'p');
  title.textContent = item.name;
  if (item.nested) { const span = document.createElement('span'); span.textContent = item.name; title.replaceChildren(span); }
  if (item.hiddenBadge) { const badge = document.createElement('span'); badge.hidden = true; badge.textContent = '内部状态'; title.append(badge); }
  const price = document.createElement(settings.table ? 'td' : 'p');
  price.textContent = '$ ' + (item.price || '10.90');
  const button = document.createElement('button');
  button.textContent = '处理';
  button.onclick = async () => {
    const response = await fetch('/click', {method:'POST', body:JSON.stringify({id:item.id, name:item.name})});
    document.querySelector('#result').textContent = (await response.json()).name;
  };
  row.append(title, price);
  if (!item.noButton) {
    if (settings.table) { const cell = document.createElement('td'); cell.append(button); row.append(cell); }
    else row.append(button);
    if (item.extraButton) row.append(button.cloneNode(true));
  }
  (list.querySelector('tbody') || list).append(row);
}
for (const item of settings.rows) {
  if (item.delayed) setTimeout(() => append(item), settings.delay);
  else append(item);
}
</script>'''.replace('SETTINGS', settings)
                if self.path == '/frame':
                    page = '<!doctype html><meta charset="utf-8"><p>指定商品</p><button>处理</button><iframe id="products" src="/"></iframe>'
                body = page.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                item = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.hits.append(item)
                body = json.dumps(item, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.rows, self.hits, self.table, self.delay = [], [], False, 2100
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def config(self):
        return {'base_url': self.url, 'allowed_origins': [self.url], 'timeout_seconds': 4, 'page_load_seconds': 10}


class BusinessLocatorMixin:
    driver_class = PlaywrightDriver

    @classmethod
    def setUpClass(cls):
        cls.site = BusinessSite().__enter__()
        cls.addClassCleanup(cls.site.__exit__)
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.driver = cls.driver_class(cls.site.config(), headless=True)
        cls.addClassCleanup(cls.driver.close)

    def setUp(self):
        self.site.rows = [{'id': 'wanted', 'name': '指定商品'}, {'id': 'other', 'name': '其他商品'}]
        self.site.hits, self.site.table = [], False
        self.driver.reset()

    def describe_first(self, context=None):
        self.driver.goto(self.site.url + ('/frame' if context else ''))
        self.assertTrue(ui.check(self.driver, {'assertion': 'visible', 'selector': '.entry button', 'context': context or []}, 4)['passed'])
        script = 'const dom=(' + DOM_JS + ')(root); return dom.describe(root.querySelector(".entry button"));'
        if isinstance(self.driver, PlaywrightDriver):
            _, root = self.driver._root(context or [])
            try:
                return root.evaluate('root => {' + script + '}')
            finally:
                root.dispose()
        self.driver._root(context or [])
        return self.driver.wd.execute_script('const root=document; ' + script)

    def replay(self, locators, expected='指定商品', timeout=4, assertion='text', context=None):
        self.driver.reset()
        case = {'id': 'UI-BUSINESS', 'kind': 'ui', 'title': '操作指定业务对象', 'extensions': {'automation': {
            'start_url': '/frame' if context else '/', 'actions': [{'action': 'click', 'locators': locators, 'context': context or []}],
            'assertions': [{'assertion': assertion, 'selector': '#result', 'expected': expected, 'context': context or []}]}}}
        return ui.run_case(self.driver, case, {**self.site.config(), 'timeout_seconds': timeout}, Path(self.temp.name), 'result.png')

    def test_reordered_inserted_cards_keep_the_original_identity_and_not_its_price(self):
        described = self.describe_first()
        self.site.rows = [{'id': 'inserted', 'name': '新插入商品'}, self.site.rows[1], {**self.site.rows[0], 'price': '99.99'}]
        result = self.replay(described['locators'])
        self.assertEqual(result['status'], 'Pass', result)
        self.assertEqual(self.site.hits, [{'id': 'wanted', 'name': '指定商品'}])
        self.assertEqual(len(described['locators']), 1)
        self.assertNotIn('10.90', str(described['locators']))

    def test_filtered_target_appears_after_fallback_grace_without_clicking_another_row(self):
        described = self.describe_first()
        self.site.rows = [self.site.rows[1], {'id': 'inserted', 'name': '新商品'}, {**self.site.rows[0], 'delayed': True}]
        started = time.monotonic()
        result = self.replay(described['locators'])
        self.assertEqual(result['status'], 'Pass', result)
        self.assertGreater(time.monotonic() - started, 1.8)
        self.assertEqual(self.site.hits, [{'id': 'wanted', 'name': '指定商品'}])

    def test_missing_or_duplicate_business_identity_never_acts_on_another_object(self):
        described = self.describe_first()
        for rows in ([{'id': 'other', 'name': '其他商品'}, {'id': 'inserted', 'name': '新商品'}],
                     [{'id': 'copy-one', 'name': '指定商品'}, {'id': 'copy-two', 'name': '指定商品'}],
                     [{'id': 'copy-one', 'name': '指定商品'}, {'id': 'copy-two', 'name': '指定商品', 'noButton': True}],
                     [{'id': 'copy-one', 'name': '指定商品'}, {'id': 'copy-two', 'name': '指定商品', 'hidden': True}],
                     [{'id': 'wanted', 'name': '指定商品', 'extraButton': True}]):
            with self.subTest(rows=rows):
                self.site.rows, self.site.hits = rows, []
                result = self.replay(described['locators'], timeout=2)
                self.assertEqual(result['status'], 'Fail', result)
                self.assertEqual(result['category'], 'locator_not_found')
                self.assertEqual(result['business_status'], 'Unverified')
                self.assertEqual(self.site.hits, [])

    def test_row_name_quotes_and_chinese_whitespace_survive_reordering(self):
        name = '客户\u00a0\u3000“甲” \'A\' "B"'
        self.site.table = True
        self.site.rows[0]['name'] = name
        self.site.rows[0]['nested'] = True
        described = self.describe_first()
        self.site.rows.reverse()
        result = self.replay(described['locators'], expected=name)
        self.assertEqual(result['status'], 'Pass', result)
        self.assertEqual(self.site.hits, [{'id': 'wanted', 'name': name}])

    def test_real_container_identifier_survives_changed_display_name(self):
        self.site.rows[0]['sku'], self.site.rows[1]['sku'] = 'sku-wanted', 'sku-other'
        described = self.describe_first()
        self.site.rows = [self.site.rows[1], {**self.site.rows[0], 'name': '新显示名'}]
        result = self.replay(described['locators'], expected='新显示名')
        self.assertEqual(result['status'], 'Pass', result)
        self.assertEqual(self.site.hits, [{'id': 'wanted', 'name': '新显示名'}])

    def test_frame_business_identity_remains_within_its_existing_context(self):
        context = [{'kind': 'frame', 'locators': [{'by': 'css', 'value': '#products'}]}]
        described = self.describe_first(context)
        self.assertEqual(len(described['locators']), 1)
        self.site.rows.reverse()
        result = self.replay(described['locators'], context=context)
        self.assertEqual(result['status'], 'Pass', result)
        self.assertEqual(self.site.hits, [{'id': 'wanted', 'name': '指定商品'}])

    def test_names_shared_by_two_rows_do_not_produce_a_guessed_business_identity(self):
        for duplicate in ({}, {'noButton': True}, {'hidden': True}):
            with self.subTest(duplicate=duplicate):
                self.site.rows = [{'id': 'wanted', 'name': '指定商品'},
                                  {'id': 'copy', 'name': '指定商品', **duplicate}, {'id': 'other', 'name': '其他商品'}]
                described = self.describe_first()
                self.assertFalse(any('count(' in locator.get('value', '') for locator in described['locators']))

    def test_hidden_title_fragment_is_not_used_as_business_identity(self):
        self.site.rows[0]['hiddenBadge'] = True
        described = self.describe_first()
        self.assertFalse(any('count(' in locator.get('value', '') for locator in described['locators']))

    def test_negative_assertions_distinguish_ambiguous_identity_from_real_absence(self):
        described = self.describe_first()
        for ambiguous in (True, False):
            with self.subTest(ambiguous=ambiguous):
                self.site.rows = ([{'id': 'wanted', 'name': '指定商品'}, {'id': 'copy', 'name': '指定商品', 'noButton': True}]
                                  if ambiguous else [{'id': 'other', 'name': '其他商品'}])
                self.driver.reset()
                self.driver.goto(self.site.url)
                for kind in ('hidden', 'count'):
                    checked = ui.check(self.driver, {'assertion': kind, 'locators': described['locators'], 'expected': 0}, 0)
                    self.assertEqual(checked['passed'], not ambiguous, checked)
                    if ambiguous:
                        self.assertIn('读取失败', checked['actual'])
                explicit = ui.check(self.driver, {'assertion': 'count', 'locators': [{'by': 'xpath', 'value': '//div[@class="entry"]'}],
                                                 'expected': len(self.site.rows)}, 0)
                self.assertTrue(explicit['passed'], explicit)
                self.assertEqual(self.site.hits, [])

    def test_literal_template_markers_are_not_expanded_as_runtime_inputs(self):
        name = '字面 {{RAND}} ${ENV:BUSINESS_TITLE}'
        self.site.rows[0]['name'] = name
        described = self.describe_first()
        self.assertEqual(len(described['locators']), 1)
        self.assertNotIn('{{RAND}}', described['locators'][0]['value'])
        self.assertNotIn('${ENV:', described['locators'][0]['value'])
        self.site.rows.reverse()
        try:
            ir.set_rand('different-runtime-value')
            result = self.replay(described['locators'], expected='字面', assertion='contains')
        finally:
            ir.set_rand('')
        self.assertEqual(result['status'], 'Pass', result)
        self.assertEqual(self.site.hits, [{'id': 'wanted', 'name': name}])
        if isinstance(self.driver, PlaywrightDriver):
            case = {'id': 'UI-LITERAL', 'kind': 'ui', 'title': '字面标记商品', 'extensions': {'automation': {
                'start_url': '/', 'actions': [{'action': 'click', 'locators': described['locators']}],
                'assertions': [{'assertion': 'contains', 'selector': '#result', 'expected': '字面'}]}}}
            for engine in ('playwright', 'selenium'):
                file = Path(self.temp.name) / (engine + '-literal.py')
                file.write_text(codegen.script([case], self.site.url, engine), encoding='utf-8')
                environment = {**os.environ, 'PYTHONPATH': '', 'HEADED': '0', 'TIMEOUT': '4',
                               'CHROME_BINARY': paths.chrome(), 'CHROMEDRIVER': paths.chromedriver() or ''}
                environment.pop('BUSINESS_TITLE', None)
                before = len(self.site.hits)
                exported = subprocess.run([sys.executable, '-I', str(file)], cwd=self.temp.name, env=environment,
                                          capture_output=True, text=True, timeout=45, check=False)
                self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
                self.assertEqual(self.site.hits[before:], [{'id': 'wanted', 'name': name}])


class PlaywrightBusinessLocatorTests(BusinessLocatorMixin, unittest.TestCase):
    driver_class = PlaywrightDriver


@unittest.skipUnless(paths.chrome() and paths.chromedriver(), 'matching local Chromium and chromedriver required')
class SeleniumBusinessLocatorTests(BusinessLocatorMixin, unittest.TestCase):
    driver_class = SeleniumDriver


class RecordedReferenceTests(unittest.TestCase):
    def test_generated_reference_does_not_reinsert_its_old_position_as_an_explicit_locator(self):
        old = {'by': 'xpath', 'value': '//div[2]/button[1]'}
        scoped = {'by': 'xpath', 'value': '//div[p="业务甲"]/button'}
        rec = {'refs': {'e1': {'locators': [old], 'context': []}}}
        with patch.object(session, '_describe', side_effect=lambda *_args: {'locators': [copy.deepcopy(scoped)]}):
            self.assertEqual(session._target(rec, '@e1', True)['locators'], [scoped])
            self.assertEqual(session._assertion_locators(rec, 'text', '@e1'), [scoped])
            self.assertEqual(session._assertion_locators(rec, 'hidden', '@e1'), [old])
            self.assertEqual(session._assertion_locators(rec, 'count', '@e1'), [old])
            self.assertEqual(session._target(rec, 'xpath=//div[2]/button[1]', True)['locators'][0], old)


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), 'actual recording browser required')
class BusinessRecordingTests(unittest.TestCase):
    def test_public_recording_generates_scoped_candidate_and_replays_after_insertion(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'AUTOSKILL_WORKSPACE': folder, 'AUTOSKILL_HEADED': '0', 'AUTOSKILL_BRIDGE': 'off',
                'AUTOSKILL_CASE_SKILL': '', 'AUTOSKILL_RUNTIME_CONTEXT': '', 'AUTOSKILL_WORKFLOW_BINDING': ''}), BusinessSite() as site:
            site.rows = [{'id': 'wanted', 'name': '指定商品'}, {'id': 'other', 'name': '其他商品'}]
            project = projects.create('商品身份录制', site.url)['id']
            with task_context.use_session('business-identity-record'):
                try:
                    started = session.start(project, login=False)
                    reference = re.search(r'button "处理" \[ref=(e\d+)\]', started['snapshot']).group(1)
                    described = session.browser(['describe', '@' + reference])
                    session.browser(['click', '@' + reference])
                    self.assertTrue(session.check('text', '#result', '指定商品')['passed'])
                    with patch('autoskill.server.jobs.ensure_worker'):
                        saved = session.stop('按商品身份操作')
                    candidate = next(case for case in saved['candidates'] if case['kind'] == 'ui')
                    locators = candidate['extensions']['automation']['actions'][0]['locators']
                    self.assertEqual(locators, described['locator_definitions'])
                    self.assertEqual(len(locators), 1)
                    site.hits = []
                    site.rows = [site.rows[1], {'id': 'inserted', 'name': '新商品'}, site.rows[0]]
                    run = runner.execute(saved['run_id'])
                    self.assertEqual(run['status'], 'Pass', run)
                    self.assertEqual(site.hits, [{'id': 'wanted', 'name': '指定商品'}])
                    for engine in ('playwright', 'selenium'):
                        exported = codegen.script([candidate], site.url, engine)
                        file = Path(folder) / (engine + '.py')
                        file.write_text(exported, encoding='utf-8')
                        environment = {**os.environ, 'PYTHONPATH': '', 'HEADED': '0', 'TIMEOUT': '4',
                                       'CHROME_BINARY': paths.chrome(), 'CHROMEDRIVER': paths.chromedriver() or ''}
                        before = len(site.hits)
                        result = subprocess.run([sys.executable, '-I', str(file)], cwd=folder, env=environment,
                                                capture_output=True, text=True, timeout=45, check=False)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(site.hits[before:], [{'id': 'wanted', 'name': '指定商品'}])
                        retained = site.rows
                        site.rows = [{'id': 'other', 'name': '其他商品'}, {'id': 'inserted', 'name': '新商品'}]
                        environment['TIMEOUT'] = '2'
                        before = len(site.hits)
                        missing = subprocess.run([sys.executable, '-I', str(file)], cwd=folder, env=environment,
                                                 capture_output=True, text=True, timeout=45, check=False)
                        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)
                        self.assertIn('页面上找不到', missing.stdout + missing.stderr)
                        self.assertEqual(site.hits[before:], [])
                        site.rows = [{'id': 'wanted', 'name': '指定商品'}, {'id': 'copy', 'name': '指定商品', 'noButton': True}]
                        ambiguous = subprocess.run([sys.executable, '-I', str(file)], cwd=folder, env=environment,
                                                   capture_output=True, text=True, timeout=45, check=False)
                        self.assertNotEqual(ambiguous.returncode, 0, ambiguous.stdout + ambiguous.stderr)
                        self.assertIn('业务目标不唯一：', ambiguous.stdout + ambiguous.stderr)
                        self.assertEqual(site.hits[before:], [])
                        site.rows = retained
                finally:
                    session.cancel()
