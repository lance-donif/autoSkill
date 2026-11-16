"""Real local UI/API CRUD and unchanged requirement oracles detect a deliberately wrong priority."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'skills/autoskill/app'
SITE = ROOT / 'skills/autoskill/examples/ticket-desk'
sys.path.insert(0, str(APP))
os.environ.setdefault('AUTOSKILL_HEADED', '0')

from autoskill import builder, paths, projects
from autoskill.recorder import session
from autoskill.replay import api, codegen, ir, ui

spec = importlib.util.spec_from_file_location('ticket_desk_acceptance_site', SITE / 'server.py')
ticket_desk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_desk)


def oracle(version=1):
    return json.loads((SITE / 'oracles' / f'v{version}.json').read_text())


def action(kind, target, **data):
    return {'action': kind, 'locators': [{'by': 'testid', 'value': target}], **data}


def expectation(kind, target, expected=None, **data):
    row = {'assertion': kind, 'locators': [{'by': 'testid', 'value': target}],
           'oracle': {'source': 'requirement', 'id': 'TD-CRUD', 'revision': 1}, **data}
    if expected is not None:
        row['expected'] = expected
    return row


def crud_ui_case():
    wanted = oracle()
    steps = [action('fill', 'login-username', value='qa_admin'), action('fill', 'login-password', value='Passw0rd!demo'),
             action('click', 'login-submit'), action('click', 'new-ticket'),
             action('fill', 'ticket-title', value='验收-{{RAND}}'), action('select', 'ticket-priority', value=wanted['create']['priority']),
             action('click', 'ticket-save'), expectation('hidden', 'ticket-dialog'), action('fill', 'ticket-search', value='验收-{{RAND}}'),
             expectation('count', 'ticket-row', 1),
             expectation('attribute', 'ticket-priority-value', wanted['create']['priority'], attribute='data-value'),
             expectation('attribute', 'ticket-status', wanted['create']['initial_state'], attribute='data-value'),
             action('click', 'ticket-resolve'),
             expectation('attribute', 'ticket-status', wanted['resolve']['state'], attribute='data-value'),
             expectation('attribute', 'ticket-priority-value', wanted['resolve']['priority'], attribute='data-value'),
             action('click', 'ticket-delete')]
    checks = [expectation('count', 'ticket-row', wanted['delete']['remaining_matches'], stable_for_seconds=0.2),
              expectation('min_count', 'ticket-row', 0)]
    return builder.ui_case('TC-UI-CRUD', '需求规定高优先级创建查询解决删除', {'start_url': '/', 'actions': steps, 'assertions': checks},
                           source='Ticket Desk oracles/v1.json')


def crud_api_cases():
    wanted = oracle()
    headers = {'Authorization': 'Bearer {{token}}'}
    calls = [
        ('LOGIN', 'POST', '/api/login', {'status': 200, 'json_equals': {'/user/role': 'admin'}},
         {'json': {'username': 'qa_admin', 'password': 'Passw0rd!demo'}, 'extract': {'token': '/token'}}),
        ('CREATE', 'POST', '/api/tickets', {'status': wanted['create']['status'], 'json_equals': {
            '/priority': wanted['create']['priority'], '/status': wanted['create']['initial_state'], '/title': '接口验收-{{RAND}}'}},
         {'json': {'title': '接口验收-{{RAND}}', 'priority': 'high'}, 'extract': {'ticket_id': '/id'}, 'headers': headers}),
        ('READ', 'GET', '/api/tickets/{{ticket_id}}', {'status': 200, 'json_equals': {'/title': '接口验收-{{RAND}}', '/priority': 'high'}}, {'headers': headers}),
        ('RESOLVE', 'PATCH', '/api/tickets/{{ticket_id}}', {'status': 200, 'json_equals': {'/status': 'resolved', '/priority': 'high'}},
         {'json': {'status': 'resolved'}, 'headers': headers}),
        ('DELETE', 'DELETE', '/api/tickets/{{ticket_id}}', {'status': wanted['delete']['status'], 'json_equals': {'/deleted': True, '/id': '{{ticket_id}}'}},
         {'headers': headers}),
        ('MISSING', 'GET', '/api/tickets/{{ticket_id}}', {'status': wanted['delete']['detail_status'], 'json_equals': {'/code': 'NOT_FOUND'}}, {'headers': headers}),
        ('EMPTY', 'GET', '/api/tickets', {'status': 200, 'json_equals': {'/items': [], '/total': 0}},
         {'params': {'q': '接口验收-{{RAND}}'}, 'headers': headers}),
    ]
    return [builder.api_case('TC-API-' + name, name, method, path, rules, source='Ticket Desk oracles/v1.json', **data)
            for name, method, path, rules, data in calls]


class TicketDeskAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ticket_desk.make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'AUTOSKILL_WORKSPACE': self.tmp.name, 'AUTOSKILL_HEADED': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = httpx.Client(base_url=self.base, trust_env=False)
        self.addCleanup(self.client.close)
        self.client.post('/api/test/reset')
        self.client.post('/api/test/config', json={'title_max': 80})
        self.config = {'base_url': self.base, 'allowed_origins': [self.base], 'timeout_seconds': 2, 'page_load_seconds': 10}

    def test_api_crud_pass_fault_fail_repair_pass_without_changing_oracle(self):
        cases = crud_api_cases()
        original = copy.deepcopy(cases)
        for bug, expected in ((False, 'Pass'), (True, 'Fail'), (False, 'Pass')):
            with self.subTest(bug=bug):
                self.client.post('/api/test/reset')
                self.client.post('/api/test/bug', json={'on': bug})
                variables, results = {}, []
                for case in cases:
                    result = api.run_case(self.client, case, self.config, variables)
                    results.append(result)
                    if result['status'] != 'Pass':
                        break
                self.assertEqual(results[-1]['status'], expected, results[-1]['reason'])
                if bug:
                    failed = [a for a in results[-1]['assertions'] if not a['passed']]
                    self.assertTrue(any(a['expected'] == 'high' and a['actual'] == 'medium' for a in failed))
                else:
                    self.assertEqual(len(results), len(cases))
        self.assertEqual(cases, original)

    def test_requirement_change_needs_new_oracle_and_preserves_v1_failure(self):
        self.client.post('/api/login', json={'username': 'qa_admin', 'password': 'Passw0rd!demo'})
        limit = oracle(1)['title']['max_length']
        before = builder.api_case('TC-API-BOUNDARY', 'V1允许80字', 'POST', '/api/tickets', {'status': 201}, json={'title': 'x' * limit})
        old = copy.deepcopy(before)
        self.assertEqual(api.run_case(self.client, before, self.config, {})['status'], 'Pass')
        self.client.post('/api/test/config', json={'title_max': 40})
        failed = api.run_case(self.client, before, self.config, {})
        self.assertEqual(failed['status'], 'Fail')
        self.assertEqual(failed['response']['status'], 422)
        self.assertEqual(before, old)
        limit = oracle(2)['title']['max_length']
        for size, status in ((limit, 201), (limit + 1, 422)):
            revised = builder.api_case('TC-API-V2', 'V2边界', 'POST', '/api/tickets', {'status': status}, json={'title': 'x' * size})
            self.assertEqual(api.run_case(self.client, revised, self.config, {})['status'], 'Pass')

    def test_delete_obeys_auth_and_returns_absent_detail(self):
        self.assertEqual(self.client.delete('/api/tickets/T-1001').status_code, 401)
        self.client.post('/api/login', json={'username': 'qa_viewer', 'password': 'Passw0rd!view'})
        self.assertEqual(self.client.delete('/api/tickets/T-1001').status_code, 403)
        self.assertEqual(self.client.get('/api/tickets/T-1001').status_code, 200)
        self.client.post('/api/login', json={'username': 'qa_admin', 'password': 'Passw0rd!demo'})
        self.assertEqual(self.client.delete('/api/tickets/T-1001').json(), {'deleted': True, 'id': 'T-1001'})
        self.assertEqual(self.client.get('/api/tickets/T-1001').status_code, 404)

    @unittest.skipUnless(paths.agent_browser() and paths.chrome(), 'requires local recorder browser')
    def test_real_recording_keeps_failed_check_and_replays_after_business_fix(self):
        from autoskill.replay.playwright_driver import PlaywrightDriver

        pid = projects.create('真实录制验收', self.base)['id']
        self.client.post('/api/test/bug', json={'on': True})
        case = crud_ui_case()
        checks = []
        try:
            session.start(pid, login=False)
            automation = case['extensions']['automation']
            for step in [*automation['actions'], *automation['assertions']]:
                selector = '[data-testid="' + step['locators'][0]['value'] + '"]'
                if 'action' in step:
                    command = [step['action'], selector]
                    if 'value' in step:
                        command.append(step['value'])
                    session.browser(command)
                else:
                    expected = str(step['expected']) if 'expected' in step else None
                    checks.append(session.check(step['assertion'], selector, expected, step.get('attribute'),
                                                timeout_seconds=2, stable_for_seconds=step.get('stable_for_seconds', 0)))
            with patch('autoskill.server.jobs.ensure_worker'):
                saved = session.stop('录制时已发现优先级业务失败')
        finally:
            session.cancel()
        self.assertTrue(any(row['status'] == 'Fail' and row['expected'] == 'high' and row['actual'] == 'medium' for row in checks))
        self.assertEqual(saved['ui_case']['recording_status'], 'Fail')
        self.assertGreater(saved['conversation_captures']['requests'], 0)
        cases = saved['candidates']
        self.assertEqual(projects.cases(pid), [])
        recorded = next(item for item in cases if item['kind'] == 'ui')
        self.assertTrue(all(item['extensions']['oracle']['source'] == 'observation' for item in cases if item['kind'] == 'api'))
        self.client.post('/api/test/bug', json={'on': False})
        driver = PlaywrightDriver(self.config)
        try:
            driver.reset()
            ir.set_rand(uuid.uuid4().hex[:6])
            replay = ui.run_case(driver, recorded, self.config, Path(self.tmp.name), 'fixed.png')
            self.assertEqual(replay['status'], 'Pass', replay['reason'])
        finally:
            driver.close()
        self.assertEqual(recorded['extensions']['recording_result']['status'], 'Fail')

    @unittest.skipUnless(paths.agent_browser() and paths.chrome(), 'requires local recorder browser')
    def test_chat_page_recording_keeps_real_http_sse_auth_body_and_context(self):
        from autoskill import conversation_capture

        pid = projects.create('页面问数捕获', self.base)['id']
        for protocol in ('http', 'sse'):
            with self.subTest(protocol=protocol):
                try:
                    session.start(pid, login=False)
                    session.browser(['fill', '#username', 'qa_admin'])
                    session.browser(['fill', '#password', 'Passw0rd!demo'])
                    session.browser(['click', '[data-testid="login-submit"]'])
                    session.check('text', '#current-user', '测试管理员', timeout_seconds=2)
                    session.browser(['open', '/chat.html'])
                    session.browser(['select', '#chat-protocol', protocol])
                    for question, expected in (('2025年销售额是多少？', '700'), ('北京呢？', '200')):
                        session.browser(['fill', '#chat-question', question])
                        session.browser(['click', '#chat-send'])
                        result = session.check('text', '#chat-messages p:last-child span', expected, timeout_seconds=2)
                        self.assertTrue(result['passed'], result['actual'])
                    with patch('autoskill.server.jobs.ensure_worker'):
                        summary = session.stop('真实页面两轮问数 ' + protocol)
                finally:
                    session.cancel()
                capture = conversation_capture.get(pid, summary['conversation_captures']['id'])
                calls = [entry for entry in capture['requests'] if '/api/chat' in entry['request']['url']]
                self.assertEqual(len(calls), 2)
                self.assertEqual([entry['protocol'] for entry in calls], [protocol, protocol])
                first, second = calls
                self.assertEqual(second['body_json']['question'], '北京呢？')
                if protocol == 'http':
                    conversation_id = first['response_json']['conversation_id']
                    self.assertEqual(second['response_json']['answer'], 200)
                else:
                    text = first['response']['content']['text']
                    events = [json.loads(line[5:]) for line in text.splitlines() if line.startswith('data:')]
                    conversation_id = events[0]['conversation_id']
                    self.assertTrue(any(event.get('done') is True for event in events))
                    self.assertIn('event: done', second['response']['content']['text'])
                self.assertEqual(second['body_json']['conversation_id'], conversation_id)
                headers = {item['name'].lower() for item in second['request']['headers']}
                self.assertTrue('cookie' in headers or second['request'].get('cookies'))
                self.assertGreaterEqual(len(summary['conversation_captures']['conversation_candidates']), 2)
                api_cases = [case for case in summary['candidates'] if case['kind'] == 'api']
                login_ids = {case['id'] for case in api_cases if case['path'] == '/api/login'}
                chat_cases = [case for case in api_cases if case['path'].startswith('/api/chat')]
                self.assertTrue(chat_cases)
                self.assertTrue(all(login_ids & set(projects.depends_on(case)) for case in chat_cases))
                self.verify_captured_conversation_via_cli(pid, summary['conversation_captures']['id'], calls, protocol)

    def verify_captured_conversation_via_cli(self, pid, capture_id, calls, protocol):
        mapping = {'turns': [row['number'] for row in calls], 'question_pointer': '/question',
                   'conversation_pointer': '/conversation_id', 'conversation_response_pointer': '/conversation_id',
                   'turn_overrides': [{'expected': 700}, {'expected': 200}], 'load': {'concurrency': 1, 'iterations': 1}}
        if protocol == 'http':
            mapping['answer_pointer'] = '/answer'
        file = Path(self.tmp.name) / ('mapping-' + protocol + '.json')
        file.write_text(json.dumps(mapping, ensure_ascii=False), encoding='utf-8')
        env = {**os.environ, 'PYTHONPATH': str(APP)}

        def command(*args, expected_code=0):
            completed = subprocess.run([sys.executable, '-m', 'autoskill', '--session', 'page-to-api-' + protocol, *args],
                                       env=env, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(completed.returncode, expected_code, completed.stderr)
            return json.loads(completed.stdout)

        asset = command('conversation', 'build', pid, capture_id, '--file', str(file), '--asset-id', 'page-chat-' + protocol)
        single = command('native', 'run', pid, asset['id'], '--wait')
        self.assertEqual(single['outcome']['business_status'], 'Pass')
        self.assertEqual(single['outcome']['metrics']['sent_turns'], 2)
        load = Path(self.tmp.name) / ('load-' + protocol + '.json')
        load.write_text(json.dumps({'load': {'concurrency': 3, 'iterations': 6}}), encoding='utf-8')
        multiple = command('native', 'run', pid, asset['id'], '--wait', '--file', str(load))
        outcome = command('native', 'status', multiple['id'], '--all')['outcome']
        self.assertEqual(outcome['business_status'], 'Pass')
        self.assertEqual(outcome['metrics']['sent_turns'], 12)
        self.assertEqual(len({case['conversation_id'] for case in outcome['cases']}), 6)
        self.assertTrue(all([str(turn['answer']) for turn in case['turns']] == ['700', '200'] for case in outcome['cases']))
        self.client.post('/api/login', json={'username': 'qa_admin', 'password': 'Passw0rd!demo'})
        self.client.post('/api/chat/config', json={'wrong_answer': True})
        wrong = command('native', 'run', pid, asset['id'], '--wait', expected_code=1)
        self.assertEqual(wrong['outcome']['business_status'], 'Fail')
        self.client.post('/api/chat/config', json={'wrong_answer': False})
        repaired = command('native', 'run', pid, asset['id'], '--wait')
        self.assertEqual(repaired['outcome']['business_status'], 'Pass')
        self.assertEqual(single['asset_version'], wrong['asset_version'])
        self.assertEqual(single['asset_version'], repaired['asset_version'])

    @unittest.skipUnless(paths.chrome() and paths.chromedriver(), 'requires local Chromium and chromedriver')
    def test_both_browser_drivers_prove_real_crud_fail_and_repair(self):
        from autoskill.replay.playwright_driver import PlaywrightDriver
        from autoskill.replay.selenium_driver import SeleniumDriver

        case = crud_ui_case()
        original = copy.deepcopy(case)
        for driver_type in (PlaywrightDriver, SeleniumDriver):
            driver = driver_type(self.config)
            try:
                for bug, expected in ((False, 'Pass'), (True, 'Fail'), (False, 'Pass')):
                    with self.subTest(engine=driver.name, bug=bug):
                        self.client.post('/api/test/reset')
                        self.client.post('/api/test/bug', json={'on': bug})
                        token = uuid.uuid4().hex[:6]
                        ir.set_rand(token)
                        driver.reset()
                        result = ui.run_case(driver, case, self.config, Path(self.tmp.name), driver.name + '.png')
                        self.assertEqual(result['status'], expected, result['reason'])
                        self.assertEqual(result['business_status'], expected)
                        self.assertTrue(result['screenshots'])
                        if bug:
                            self.assertTrue(any(a['expected'] == 'high' and a['actual'] == 'medium' for a in result['assertions']))
                        self.client.post('/api/login', json={'username': 'qa_admin', 'password': 'Passw0rd!demo'})
                        self.assertEqual(self.client.get('/api/tickets', params={'q': '验收-' + token}).json()['items'], [])
            finally:
                driver.close()
        self.assertEqual(case, original)

    @unittest.skipUnless(paths.chrome() and paths.chromedriver(), 'requires local Chromium and chromedriver')
    def test_exported_scripts_use_real_checks_including_zero_minimum(self):
        for engine in ('playwright', 'selenium'):
            case = crud_ui_case()
            file = Path(self.tmp.name) / ('crud_' + engine + '.py')
            file.write_text(codegen.script([case], self.base, engine), encoding='utf-8')
            for bug in (False, True):
                with self.subTest(engine=engine, bug=bug):
                    self.client.post('/api/test/reset')
                    self.client.post('/api/test/bug', json={'on': bug})
                    env = {**os.environ, 'CHROME_BINARY': paths.chrome(), 'CHROMEDRIVER': paths.chromedriver(), 'TIMEOUT': '2', 'HEADED': '0'}
                    result = subprocess.run([sys.executable, str(file)], env=env, capture_output=True, text=True, timeout=45, check=False)
                    if bug:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('medium', result.stderr)
                        self.assertIn('high', result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
