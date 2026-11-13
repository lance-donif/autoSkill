"""CDP source evidence preserves stream bytes and isolates real tabs and recording sessions."""
import base64
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/autoskill/app'))

from test_ticket_desk_acceptance import ticket_desk

from autoskill import conversation_capture, paths, projects, task_context
from autoskill.recorder import capture, session


class CaptureUnitTests(unittest.TestCase):
    def setUp(self):
        self.observer = capture.Observer.__new__(capture.Observer)
        for name in ('pending', 'targets', 'requests', 'extras', 'response_extras', 'bodies'):
            setattr(self.observer, name, {})
        self.observer.entries = []
        self.observer.folder = Path('/tmp/capture-test')
        self.observer.token = 'test-token'
        self.observer.ready = True
        self.observer.send = Mock()
        self.observer.log = Mock()

    def request(self, tab='tab-1', identity='request-1'):
        self.observer.receive({'method': 'Network.requestWillBeSent', 'sessionId': tab, 'params': {
            'requestId': identity, 'type': 'Fetch', 'request': {
                'method': 'POST', 'url': 'https://example.test/chat', 'headers': {'Content-Type': 'application/json'},
                'postData': '{"question":"hello"}', 'hasPostData': True,
            },
        }})
        return self.observer.entries[-1]

    def test_paused_noopener_popup_resumes_without_waiting_for_network_enable_reply(self):
        self.observer.receive({'method': 'Target.attachedToTarget', 'params': {
            'sessionId': 'popup', 'waitingForDebugger': True,
            'targetInfo': {'targetId': 'popup-target', 'type': 'page', 'canAccessOpener': False},
        }})
        calls = self.observer.send.call_args_list
        self.assertEqual([item.args[0] for item in calls],
                         ['Network.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])
        self.assertTrue(all(item.args[2] == 'popup' for item in calls))
        self.assertEqual(calls[1].args[1]['filter'],
                         [{'type': kind, 'exclude': True} for kind in ('browser', 'tab', 'browser_ui')] + [{}])
        self.assertEqual(self.observer.targets['popup']['targetId'], 'popup-target')

    def test_extra_headers_keep_real_cookie_even_when_the_event_precedes_request(self):
        self.observer.receive({'method': 'Network.requestWillBeSentExtraInfo', 'sessionId': 'tab-1', 'params': {
            'requestId': 'request-1', 'headers': {'Content-Type': 'application/json', 'Cookie': 'synthetic=one'},
            'associatedCookies': [{'cookie': {'name': 'synthetic', 'value': 'one'}, 'blockedReasons': []}],
        }})
        entry = self.request()
        self.assertEqual(entry['_request_headers_source'], 'requestWillBeSentExtraInfo')
        self.assertIn({'name': 'Cookie', 'value': 'synthetic=one'}, entry['request']['headers'])
        self.assertEqual(entry['request']['cookies'], [{'name': 'synthetic', 'value': 'one'}])

    def test_identical_request_ids_in_two_tabs_never_mix_bodies(self):
        first, second = self.request('one'), self.request('two')
        for target, text in (('one', 'first'), ('two', 'second')):
            self.observer.receive({'method': 'Network.dataReceived', 'sessionId': target,
                                   'params': {'requestId': 'request-1', 'data': base64.b64encode(text.encode()).decode()}})
        self.assertEqual(bytes(self.observer.bodies[id(first)]), b'first')
        self.assertEqual(bytes(self.observer.bodies[id(second)]), b'second')

    def test_buffered_prefix_precedes_live_chunks_and_authoritative_body_is_not_duplicated(self):
        entry = self.request()
        self.observer._data(entry, base64.b64encode(b'after').decode())
        self.observer._data(entry, base64.b64encode(b'before-').decode(), buffered=True)
        self.assertEqual(bytes(self.observer.bodies[id(entry)]), b'before-after')
        self.observer.pending[1] = ('Network.getResponseBody', 'tab-1', entry)
        self.observer._reply({'id': 1, 'result': {'body': 'complete', 'base64Encoded': False}})
        self.observer._data(entry, base64.b64encode(b'duplicate').decode(), buffered=True)
        self.assertEqual(bytes(self.observer.bodies[id(entry)]), b'complete')

    def test_cancellation_retains_captured_sse_and_binary_bytes_are_lossless(self):
        stream = self.request()
        stream['response'] = {'status': 200, 'content': {'mimeType': 'text/event-stream'}}
        self.observer._data(stream, base64.b64encode(b'event: done\ndata: {"done":true}\n\n').decode())
        self.observer._finished('tab-1', {'requestId': 'request-1', 'errorText': 'net::ERR_ABORTED'}, True)
        binary = self.request('tab-2')
        binary['response'] = {'status': 200, 'content': {'mimeType': 'audio/wav'}}
        self.observer.bodies[id(binary)] = bytearray(b'RIFF\x00\xff\x8f')
        self.observer.finish()
        self.assertFalse(stream['_body_complete'])
        self.assertIn('event: done', stream['response']['content']['text'])
        self.assertEqual(binary['response']['content']['encoding'], 'base64')
        self.assertEqual(base64.b64decode(binary['response']['content']['text']), b'RIFF\x00\xff\x8f')

    def test_missing_body_is_explicit_and_does_not_become_an_empty_success(self):
        entry = self.request()
        entry['response'] = {'status': 200, 'content': {'mimeType': 'text/event-stream'}}
        self.observer.finish()
        self.assertNotIn('text', entry['response']['content'])
        self.assertFalse(entry['_body_complete'])


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), 'requires local recorder browser')
class CaptureBrowserTests(unittest.TestCase):
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
        env = patch.dict(os.environ, {'AUTOSKILL_WORKSPACE': self.tmp.name, 'AUTOSKILL_HEADED': '0', 'AUTOSKILL_SESSION': ''})
        env.start()
        self.addCleanup(env.stop)
        self.pid = projects.create('CDP实际来源验收', self.base)['id']

    def login(self):
        session.start(self.pid, login=False)
        session.browser(['fill', '#username', 'qa_admin'])
        session.browser(['fill', '#password', 'Passw0rd!demo'])
        session.browser(['click', '[data-testid="login-submit"]'])
        result = session.check('text', '#current-user', '测试管理员', timeout_seconds=3)
        self.assertTrue(result['passed'])

    def ask(self, protocol='http'):
        session.browser(['select', '#chat-protocol', protocol])
        session.browser(['fill', '#chat-question', '2025年销售额是多少？'])
        session.browser(['click', '#chat-send'])
        self.assertTrue(session.check('text', '#chat-messages p:last-child span', '700', timeout_seconds=3)['passed'])

    def test_new_tabs_and_navigation_keep_separate_real_requests(self):
        try:
            self.login()
            session.browser(['open', '/chat.html'])
            self.ask()
            session.browser(['tab', 'new', '/chat.html'])
            self.ask('sse')
            session.browser(['tab', 't1'])
            session.browser(['reload'])
            self.ask()
            result = session.stop('跨标签页真实网络来源')
        finally:
            session.cancel()
        evidence = conversation_capture.get(self.pid, result['conversation_captures']['id'])
        calls = [row for row in evidence['requests'] if '/api/chat' in row['request']['url']]
        self.assertEqual(len(calls), 3)
        self.assertEqual([row['protocol'] for row in calls], ['http', 'sse', 'http'])
        self.assertEqual(len({row['cdp']['target_id'] for row in calls}), 2)
        self.assertTrue(all(row['capture_source'] == 'cdp_network' for row in calls))
        self.assertTrue(all(any(item['name'].lower() == 'cookie' for item in row['request']['headers']) for row in calls))
        self.assertIn('event: done', calls[1]['response']['content']['text'])
        self.assertFalse(list(session.recording_dir().glob('network-*')))

    def test_cancel_one_simultaneous_recorder_keeps_the_other_observer_working(self):
        ready, cancelled = threading.Barrier(2), threading.Event()
        handles = {}

        def record(identity):
            with task_context.use_session(identity):
                try:
                    self.login()
                    handles[identity] = session.current()['network_capture']
                    ready.wait(timeout=30)
                    if identity == 'A':
                        session.cancel()
                        cancelled.set()
                        return None
                    self.assertTrue(cancelled.wait(timeout=30))
                    session.browser(['open', '/chat.html'])
                    self.ask('sse')
                    return session.stop('另一会话取消后仍可完成问数')
                finally:
                    session.cancel()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.submit(record, 'A'), executor.submit(record, 'B')
            first.result(timeout=60)
            summary = second.result(timeout=60)
        self.assertNotEqual(handles['A']['pid'], handles['B']['pid'])
        self.assertNotEqual(handles['A']['folder'], handles['B']['folder'])
        evidence = conversation_capture.get(self.pid, summary['conversation_captures']['id'])
        self.assertEqual(evidence['origin']['session_id'], 'B')
        calls = [row for row in evidence['requests'] if '/api/chat/stream' in row['request']['url']]
        self.assertEqual(len(calls), 1)
        self.assertIn('event: done', calls[0]['response']['content']['text'])
        self.assertTrue(all(not Path(handle['folder']).exists() for handle in handles.values()))


if __name__ == '__main__':
    unittest.main()
