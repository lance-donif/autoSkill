"""Real recording, both engines and standalone exports across tabs/windows."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/autoskill/app'))
__import__("os").environ.setdefault("AUTOSKILL_HEADED", "0")  # tests never open browser windows
from test_bridge import Sandbox

from autoskill import paths, projects
from autoskill.recorder import session
from autoskill.replay import codegen, ir, ui
from autoskill.replay.playwright_driver import PlaywrightDriver
from autoskill.replay.selenium_driver import SeleniumDriver
from autoskill.replay.tabs import Tabs


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            return  # Closing a tab can reset an idle HTTP connection on Windows.

    def do_GET(self):
        if self.path.startswith('/child'):
            html = '''<title>Child</title><h1>Child page</h1><input id=note>
            <button id=send onclick="window.opener.document.querySelector('#result').textContent=document.querySelector('#note').value;window.close()">Send and close</button>'''
        else:
            html = '''<title>Parent</title><h1>Parent page</h1><div id=result>waiting</div>
            <a id=blank target=_blank href='/child?source=link&token=must-not-leak&code=oauth-one-time#private'>Open link</a>
            <button id=popup onclick="window.open('/child?source=popup','_blank')">Popup</button>
            <button id=delayed onclick="setTimeout(()=>window.open('/child?source=delayed','_blank'),250)">Delayed</button>
            <button id=multi onclick="window.open('/child?source=one','_blank');window.open('/child?source=two','_blank')">Two windows</button>
            <button id=none>Nothing opens</button>'''
        data = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class BrowserSiteServer(ThreadingHTTPServer):
    # Chrome preconnects while closing/creating several contexts. Windows' small
    # default accept backlog can reject those connections under CI load.
    request_queue_size = 64
    daemon_threads = True


@unittest.skipUnless(paths.agent_browser() and paths.chrome() and paths.chromedriver(), 'complete browser runtime required')
class TabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = BrowserSiteServer(('127.0.0.1', 0), Site)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.before = os.environ.get('AUTOSKILL_WORKSPACE')
        os.environ['AUTOSKILL_WORKSPACE'] = self.tmp.name
        projects.create('Tabs', self.base, '', 'tabs')

    def tearDown(self):
        try:
            session.cancel()
        finally:
            if self.before is None:
                os.environ.pop('AUTOSKILL_WORKSPACE', None)
            else:
                os.environ['AUTOSKILL_WORKSPACE'] = self.before
            self.tmp.cleanup()

    def finish(self):
        result = session.stop('跨标签页')
        return next(c for c in projects.cases('tabs') if c['id'] == result['ui_case']['id'])

    def replay(self, case, exports=False, repeat=False):
        config = {**projects.config('tabs'), 'page_load_seconds': 5, 'timeout_seconds': 2}
        for engine, cls in [('playwright', PlaywrightDriver), ('selenium', SeleniumDriver)]:
            with self.subTest(engine=engine):
                driver = cls(config)
                try:
                    for _ in range(2 if repeat else 1):
                        driver.reset()
                        result = ui.run_case(driver, case, config, Path(self.tmp.name), engine + '.png')
                        self.assertEqual(result['status'], 'Pass', result)
                finally:
                    driver.close()
                if exports:
                    source = codegen.script([case], self.base, engine)
                    self.assertNotIn('from autoskill', source)
                    file = Path(self.tmp.name) / (engine + '_standalone.py')
                    file.write_text(source)
                    env = {**paths.child_env(), 'CHROME_BINARY': paths.chrome(), 'CHROMEDRIVER': paths.chromedriver(),
                           'NO_PROXY': 'localhost,127.0.0.1,::1', 'no_proxy': 'localhost,127.0.0.1,::1', 'TIMEOUT': '5'}
                    run = subprocess.run([sys.executable, str(file)], env=env, capture_output=True, text=True, timeout=45, check=False)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_target_blank_record_replay_and_standalone_exports(self):
        self.popup_roundtrip('#blank')

    def test_window_open_record_replay_and_standalone_exports(self):
        self.popup_roundtrip('#popup')

    def test_delayed_popup_record_replay_and_standalone_exports(self):
        self.popup_roundtrip('#delayed')

    def popup_roundtrip(self, selector):
        session.start('tabs')
        opened = session.browser(['click', selector])
        self.assertEqual(opened['tab'], 't2')
        self.assertIn('Child page', opened['snapshot'])
        session.check('contains', 'h1', 'Child page')
        session.browser(['fill', '#note', 'child data'])
        session.browser(['tab', 't1'])
        session.check('contains', 'h1', 'Parent page')
        session.browser(['tab', 't2'])
        session.check('value', '#note', 'child data')
        closed = session.browser(['tab', 'close'])
        self.assertEqual(closed['tab'], 't1')
        session.check('contains', 'h1', 'Parent page')
        case = self.finish()
        self.assertNotIn('must-not-leak', json.dumps(case))
        self.assertNotIn('oauth-one-time', json.dumps(case))
        self.replay(case, exports=True)

    def test_script_closes_child_and_returns_result_to_parent(self):
        session.start('tabs')
        session.browser(['click', '#popup'])
        session.browser(['fill', '#note', 'result from child'])
        result = session.browser(['click', '#send'])
        self.assertEqual(result['tab'], 't1')
        session.check('text', '#result', 'result from child')
        self.replay(self.finish(), exports=True)

    def test_manual_new_background_close_nonreused_ids_and_reset(self):
        session.start('tabs')
        before = len(session.current()['steps'])
        self.assertEqual(session.browser(['tab', 'list'])['tabs'][0]['id'], 't1')
        self.assertEqual(len(session.current()['steps']), before)
        with self.assertRaises(session.RecordError):
            session.browser(['tab', 'close'])
        with self.assertRaises(session.RecordError):
            session.browser(['tab', 't99'])
        session.browser(['tab', 'new', '/child?manual'])
        session.browser(['tab', 't1'])
        session.browser(['tab', 'close', 't2'])
        self.assertEqual(session.browser(['tab', 'new', '/child?second'])['tab'], 't3')
        session.browser(['tab', 'new', '/child?third'])
        session.browser(['tab', 'close', 't1'])
        session.browser(['tab', 't3'])
        session.browser(['tab', 'close', 't4'])
        with self.assertRaises(session.RecordError):
            session.browser(['tab', 'close'])
        session.check('contains', 'h1', 'Child page')
        self.replay(self.finish(), exports=True, repeat=True)

    def test_simultaneous_popups_matched_by_destination(self):
        session.start('tabs')
        session.browser(['click', '#multi'])
        tabs = session.browser(['tab', 'list'])['tabs']
        self.assertEqual(len(tabs), 3)
        for tab in tabs:
            if '/child' in tab['url']:
                session.browser(['tab', tab['id']])
                session.check('url_contains', None, tab['url'].split('?')[1])
        session.browser(['tab', 'close', 't2'])
        session.browser(['tab', 'close', 't3'])
        session.check('contains', 'h1', 'Parent page')
        self.replay(self.finish(), exports=True)

    def test_missing_popup_fails_and_does_not_select_existing_tab(self):
        automation = {'start_url': '/', 'actions': [{'action': 'click', 'locators': [{'by': 'css', 'value': '#none'}],
                       'tabs': {'opened': [{'id': 't2', 'url': '/child'}], 'closed': [], 'active': 't2'}}],
                      'assertions': [{'assertion': 'url_contains', 'expected': '/child'}]}
        case = {'id': 'MISSING', 'kind': 'ui', 'extensions': {'automation': automation}}
        for cls in (PlaywrightDriver, SeleniumDriver):
            # This tests a missing popup, not a sub-second initial navigation.
            config = {'base_url': self.base, 'page_load_seconds': 5, 'timeout_seconds': 1}
            driver = cls(config)
            try:
                driver.reset()
                result = ui.run_case(driver, case, config, Path(self.tmp.name), cls.__name__ + '.png')
                self.assertEqual(result['status'], 'Fail', result)
                self.assertIn('标签页', result['reason'])
                self.assertNotIn('/child', driver.url())
            finally:
                driver.close()


class TabValidationTests(unittest.TestCase):
    def test_popup_hints_remove_credentials_and_login_parameters(self):
        self.assertEqual(Tabs.destination('https://user:pass@example.test/child?source=link&token=secret&code=one-time#private'),
                         'https://example.test/child?source=link')

    def test_invalid_tab_steps_are_rejected_before_replay(self):
        for step in ({'action': 'tab_switch', 'tab': -1}, {'action': 'tab_close', 'tab': 't1'},
                     {'action': 'click', 'selector': '#x', 'tabs': {'active': 'oops'}},
                     {'action': 'tab_sync', 'tabs': {'active': 't1', 'opened': 't2'}}):
            with self.subTest(step=step), self.assertRaises(ValueError):
                ir.validate({'start_url': '/', 'actions': [step]})


@unittest.skipUnless(paths.agent_browser() and paths.chrome() and paths.chromedriver(), 'complete browser runtime required')
class TabBridgeTests(Sandbox):
    setUpClass = classmethod(TabsTests.setUpClass.__func__)
    tearDownClass = classmethod(TabsTests.tearDownClass.__func__)

    def test_sandboxed_cli_records_switches_and_replays_through_service(self):
        # Sandbox.setUp reserves self.server for the autoSkill service process.
        self.start_service()
        commands = [
            ['project', 'create', '--name', 'Bridge tabs', '--url', self.base, '--id', 'bridge-tabs'],
            ['record', 'start', 'bridge-tabs'], ['browser', 'click', '#blank'],
            ['check', 'contains', 'h1', 'Child page'], ['browser', 'tab', 't1'],
            ['check', 'contains', 'h1', 'Parent page'], ['browser', 'tab', 't2'],
            ['browser', 'tab', 'close'], ['check', 'contains', 'h1', 'Parent page'],
            ['record', 'stop', '--title', '后台跨标签页'],
            ['run', 'bridge-tabs', '--engine', 'playwright'], ['run', 'bridge-tabs', '--engine', 'selenium'],
        ]
        for args in commands:
            code, out, err = self.cli(*args)
            self.assertEqual(code, 0, str(args) + out + err)
            if args[0] == 'run':
                self.assertEqual(json.loads(out)['status'], 'Pass', out)
