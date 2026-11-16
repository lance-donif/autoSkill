"""Declared oracles survive recording failures, interruptions and temporal replay."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/autoskill/app'))

from autoskill import builder, projects
from autoskill.recorder import compile as compiler
from autoskill.recorder import session
from autoskill.replay import ir, postman, ui


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)


class Probe:
    def __init__(self, value='medium', count=1):
        self.value, self.number = value, count

    def goto(self, url):
        self.current = url

    def url(self):
        return self.current

    def count(self, locator):
        return self.number

    def read(self, locator, what, arg=None):
        return self.value() if callable(self.value) else self.value

    def act(self, *args):
        pass

    def screenshot(self, path):
        path.write_bytes(b'synthetic evidence')


def check_step(**fields):
    return {'assertion': 'text', 'selector': '#priority', 'expected': 'high', **fields}


class AssertionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'AUTOSKILL_WORKSPACE': self.tmp.name, 'AUTOSKILL_HEADED': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {'base_url': 'https://example.test', 'timeout_seconds': 0}
        self.clock = Clock()

    def poll(self, probe, step, timeout=1):
        with patch.object(ui.time, 'monotonic', self.clock.monotonic), patch.object(ui.time, 'sleep', self.clock.sleep):
            return ui.check(probe, step, timeout)

    def test_failed_live_check_is_saved_with_unchanged_oracle_and_compiles(self):
        pid = projects.create('检查证据', self.config['base_url'])['id']
        rec = {'project_id': pid, 'start_url': '/', 'steps': [], 'refs': {}}
        with patch.object(session, 'current', return_value=rec), patch.object(session, '_sync_tabs'), \
                patch.object(session, '_target', return_value={'locators': [{'by': 'css', 'value': '#priority'}]}), \
                patch.object(session, 'PageProbe', return_value=Probe()):
            result = session.check('text', '#priority', 'high', timeout_seconds=0)
        self.assertEqual((result['status'], result['expected'], result['actual']), ('Fail', 'high', 'medium'))
        persisted = json.loads(session._file().read_text())
        self.assertEqual(persisted['steps'][0]['expected'], 'high')
        self.assertFalse(persisted['steps'][0]['recording_result']['passed'])
        summary = compiler.build(pid, rec, {'log': {'entries': []}}, None, '高优先级必须保留')
        saved = summary['candidates'][0]
        self.assertEqual(projects.cases(pid), [])
        self.assertEqual(saved['extensions']['automation']['assertions'][0]['expected'], 'high')
        self.assertEqual(summary['ui_case']['recording_status'], 'Fail')

    def test_prior_business_failure_survives_missing_secret(self):
        steps = [check_step(), {'action': 'fill', 'selector': '#password', 'value': '${ENV:ABSENT}'}]
        case = builder.ui_case('TC-UI-001', '失败不得被清空', {'start_url': '/', 'actions': steps})

        def resolve(value):
            if value == '${ENV:ABSENT}':
                raise ValueError('缺少凭据 ABSENT')
            return value

        with patch.object(ui.secrets, 'resolve', side_effect=resolve):
            result = ui.run_case(Probe(), case, self.config, Path(self.tmp.name), 'fail.png')
        self.assertEqual((result['status'], result['category']), ('Fail', ui.ASSERTION))
        self.assertEqual(result['execution_blocker']['category'], ui.SECRET)
        self.assertEqual([(a['expected'], a['actual']) for a in result['assertions']], [('high', 'medium')])
        self.assertEqual(result['business_status'], 'Fail')

    def test_action_only_execution_is_compatible_but_unverified(self):
        case = builder.ui_case('TC-UI-001', '打开页面', {'start_url': '/', 'actions': []})
        result = ui.run_case(Probe(), case, self.config, Path(self.tmp.name), 'open.png')
        self.assertEqual((result['status'], result['verification'], result['business_status']), ('Pass', 'unverified', 'Unverified'))
        self.assertEqual(result['assertions'], [])

    def test_wait_longer_than_five_seconds_and_dynamic_zero_count(self):
        probe = Probe(lambda: 'high' if self.clock.now >= 6 else 'medium')
        result = self.poll(probe, check_step(timeout_seconds=8), timeout=1)
        self.assertTrue(result['passed'])
        self.assertGreaterEqual(result['elapsed_ms'], 6000)
        with patch.object(ui.secrets, 'resolve', return_value='0'):
            result = self.poll(Probe(count=0), {'assertion': 'min_count', 'selector': '.missing', 'expected': '${ENV:ZERO}'})
        self.assertTrue(result['passed'])
        self.assertEqual(result['expected'], 0)

    def test_transition_observes_source_then_stable_target(self):
        probe = Probe(lambda: 'open' if self.clock.now < 0.3 else 'resolved')
        step = check_step(expected='resolved', transition_from={'assertion': 'text', 'expected': 'open'}, stable_for_seconds=0.2)
        result = self.poll(probe, step)
        self.assertTrue(result['passed'])
        self.assertEqual(result['source_observation'], {'expected': 'open', 'actual': 'open', 'observed': True})
        self.assertGreaterEqual(result['continuous_ms'], 200)
        result = self.poll(Probe('resolved'), step, timeout=0)
        self.assertFalse(result['passed'])
        self.assertEqual(result['phase'], 'transition_from')

    def test_always_fails_at_first_violation_and_eventually_resets_stability(self):
        probe = Probe(lambda: 'medium' if 0.1 <= self.clock.now < 0.3 else 'high')
        result = self.poll(probe, check_step(mode='always', stable_for_seconds=0.4))
        self.assertFalse(result['passed'])
        self.assertEqual(result['actual'], 'medium')
        self.clock.now = 0
        result = self.poll(probe, check_step(stable_for_seconds=0.4))
        self.assertTrue(result['passed'])
        self.assertGreaterEqual(result['elapsed_ms'], 700)

    def test_invalid_timing_is_rejected_without_running_actions(self):
        for extra in ({'timeout_seconds': -1}, {'stable_for_seconds': True}, {'timeout_seconds': float('inf')},
                      {'mode': 'guess'}, {'transition_from': {'assertion': 'text', 'expected': 'x', 'transition_from': {}}}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ir.validate({'start_url': '/', 'assertions': [check_step(**extra)]})

    def test_postman_dynamic_expected_preserves_extracted_types_and_negative_verdict(self):
        case = builder.api_case('TC-API-001', '变量等价', 'GET', '/x', {'status': 200, 'json_equals': {
            '/id': '{{ticket_id}}', '/label': '工单-{{RAND}}', '/nested': {'count': '{{ticket_id}}'}, '/nullable': '{{nullable}}',
        }})
        script = '\n'.join(postman.test_script(case))
        source = '''const vm = require('node:vm'), assert = require('node:assert/strict');
const script = JSON.parse(process.argv[1]);
function run(actual) {
  const rows = [];
  const values = {ticket_id:'42', RAND:'unique', __autoskill_json_ticket_id:'42', nullable:null, __autoskill_json_nullable:'null'};
  const pm = {
    variables: {get: key => values[key]}, collectionVariables: {get: key => values[key]},
    response: {json: () => actual, to: {have: {status: expected => assert.equal(expected, 200)}}},
    expect: actual => ({to: {eql: expected => assert.deepEqual(actual, JSON.parse(JSON.stringify(expected)))}}),
    test: (name, fn) => {try {fn();rows.push(true);} catch {rows.push(false);}}
  };
  vm.runInNewContext(script, {pm});
  return rows;
}
assert(run({id:42,label:'工单-unique',nested:{count:42},nullable:null}).every(Boolean));
assert(run({id:41,label:'工单-unique',nested:{count:42},nullable:null}).some(value => !value));
assert.throws(() => vm.runInNewContext(script, {pm:{variables:{get:()=>undefined},collectionVariables:{get:()=>undefined}}}), /缺少变量/);
'''
        completed = subprocess.run(['node', '-e', source, json.dumps(script)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(case['assertions']['json_equals']['/id'], '{{ticket_id}}')

    def test_workflow_operation_is_receipted_unknown_without_retry(self):
        rec = {'project_id': 'project', 'workflow_binding': {'workflow_id': 'wf'}}
        from autoskill import workflow

        action = unittest.mock.Mock(side_effect=RuntimeError('connection ended after dispatch'))
        with patch.object(workflow, 'operation_intent', return_value={'operation_id': 'op'}) as intent, \
                patch.object(workflow, 'operation_receipt') as receipt, patch.object(session, '_save'), self.assertRaises(RuntimeError):
            session._operation(rec, 'browser_action', {'command': 'click'}, action)
        action.assert_called_once()
        self.assertEqual(intent.call_args.kwargs['binding'], rec['workflow_binding'])
        self.assertEqual(receipt.call_args.kwargs['status'], 'unknown')
        self.assertEqual(rec['operations'], ['op'])


if __name__ == '__main__':
    unittest.main()
