"""The public workflow design example accepts UI candidates without copying locator metadata into its oracle."""
import copy
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from autoskill import builder, projects, runner, workflow
from autoskill.workflow_state import WorkflowError


class WorkflowUIOracleContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'AUTOSKILL_WORKSPACE': temporary.name, 'AUTOSKILL_CASE_SKILL': '',
                                              'AUTOSKILL_SESSION': 'ui-oracle-contract', 'AUTOSKILL_HEADED': '0'})
        environment.start()
        self.addCleanup(environment.stop)
        self.project = projects.create('预约检查格式', 'http://127.0.0.1:9', project_id='ui-oracle')['id']
        worker = patch('autoskill.server.jobs.ensure_worker', return_value={'status': 'running', 'pid': None})
        worker.start()
        self.addCleanup(worker.stop)
        spawn = patch.object(runner, 'spawn', return_value=None)
        spawn.start()
        self.addCleanup(spawn.stop)

    def next(self, identity):
        return workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)['task']

    def submit(self, identity, task, payload):
        return workflow.submit(identity, task['item_id'], lease_token=task['lease_token'], attempt_id=task['attempt_id'],
                               idempotency_key=uuid.uuid4().hex, payload=payload)

    def test_public_ui_example_accepts_recorded_metadata_but_rejects_changed_expectation(self):
        identity = workflow.create(self.project, {'title': '预约状态和金额',
                                   'sources': [{'id': 'source-1', 'text': '预约完成后显示已预约，合计160元。'}]},
                                   idempotency_key=uuid.uuid4().hex)['workflow_id']
        self.submit(identity, self.next(identity), {'requirements': [
            {'id': 'req-1', 'text': '预约完成后显示已预约，合计160元。', 'source_ids': ['source-1']}]})
        task = self.next(identity)
        design = copy.deepcopy(task['payload_schema']['ui_example'])
        self.assertIn('attribute', task['payload_schema']['oracle_checks']['ui'])
        checks = design['oracles'][0]['checks']
        self.submit(identity, task, design)
        actions = [{**checks[0], 'locators': [{'by': 'testid', 'value': 'state'}],
                    'oracle': {'source': 'explicit'}, 'recording_result': {'passed': True}}]
        assertions = [{**checks[1], 'locators': [{'by': 'testid', 'value': 'total'}], 'timeout_seconds': 3}]
        candidate = builder.ui_case('UI-BOOKING', '预约状态和金额', {'start_url': '/', 'actions': actions, 'assertions': assertions})
        candidate['requirement_ids'] = ['req-1']
        materialize = self.next(identity)
        changed = copy.deepcopy(candidate)
        changed['extensions']['automation']['assertions'][0]['expected'] = '合计：80 元'
        with self.assertRaises(WorkflowError) as raised:
            self.submit(identity, materialize, {'cases': [changed], 'bindings': {'scenario-ui-1': ['UI-BOOKING']}})
        self.assertEqual(raised.exception.code, 'oracle_change')
        accepted = self.submit(identity, materialize, {'cases': [candidate], 'bindings': {'scenario-ui-1': ['UI-BOOKING']}})
        self.assertTrue(accepted['accepted'])
        self.assertEqual(workflow.read(identity, kind='oracles')['items'][0]['checks'], checks)
        self.assertEqual(projects.cases(self.project), [])
