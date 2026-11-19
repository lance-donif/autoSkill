"""Complete design delivery binds unpublished candidates to exact local execution evidence."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/autoskill/app'))

from autoskill import builder, candidate_publication, case_plans, data_refs, db, execution_inputs, plan, projects, runner, task_bugs, task_reports
from autoskill.recorder import session
from autoskill.replay import api


class CandidateSheetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, {'AUTOSKILL_WORKSPACE': str(self.root / 'data'),
                                              'AUTOSKILL_CASE_SKILL': str(self.root / 'sheet'),
                                              'AUTOSKILL_SESSION': '完整设计任务', 'AUTOSKILL_WORKFLOW_BINDING': '',
                                              'AUTOSKILL_BRIDGE': 'off', 'AUTOSKILL_HEADED': '0'})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create('订单测试', 'http://127.0.0.1:9', project_id='candidate-sheet')['id']
        self.rows = [{'id': f'DESIGN-{number}', 'title': f'订单场景{number}', 'module': '订单',
                      'steps': [f'1. 查询指定订单{number}。'], 'expected': f'1. 订单{number}处于可处理状态。'} for number in range(1, 4)]
        source = self.root / 'design.json'
        source.write_text(json.dumps(self.rows, ensure_ascii=False), encoding='utf-8')
        self.written = plan.write(self.pid, str(source))

    def candidate(self, identity='TC-API-CANDIDATE'):
        return builder.api_case(identity, '独立预期', 'GET', '/result', {'status': 200, 'json_equals': {'/ok': True}})

    def queue(self, candidate, row='DESIGN-1'):
        run = runner.create(self.pid, [candidate['id']], candidate_cases=[candidate])
        frozen = execution_inputs.load(run)['cases'][candidate['id']]
        bound = plan.bind_execution(row, self.pid, frozen, run['id'], candidate_id='candidate-local')
        self.assertNotIn('error', bound, bound)
        return run

    def execute(self, run, passed=False):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={'ok': passed}))
        with patch.object(api, 'new_client', side_effect=lambda *args: httpx.Client(transport=transport)):
            result = runner.execute(run['id'])
        plan.sync(run_id=run['id'])
        return result

    def workbook_rows(self, file):
        book = load_workbook(file)
        try:
            sheet = book['功能测试用例']
            self.assertEqual(sheet.max_column, 14)
            return [list(row) for row in sheet.values if row[9] in {'未执行', '失败', '通过', '业务未验证', '阻塞', '已取消'}]
        finally:
            book.close()

    def test_failed_unpublished_candidate_updates_original_complete_excel_and_web_view(self):
        initial = Path(self.written['initial_excel']).read_bytes()
        before = Path(self.written['delivery_excel']).read_bytes()
        self.assertEqual([row[9] for row in self.workbook_rows(self.written['delivery_excel'])], ['未执行'] * 3)
        run = self.queue(self.candidate())
        self.assertEqual(self.execute(run)['status'], 'Fail')
        self.assertEqual(projects.cases(self.pid), [])
        shown = plan.show()
        self.assertEqual(shown['delivery_excel'], self.written['delivery_excel'])
        self.assertNotEqual(Path(shown['delivery_excel']).read_bytes(), before)
        self.assertEqual(Path(shown['initial_excel']).read_bytes(), initial)
        rows = self.workbook_rows(shown['delivery_excel'])
        self.assertEqual([row[9] for row in rows], ['失败', '未执行', '未执行'])
        self.assertEqual(rows[0][5], self.rows[0]['steps'][0])
        self.assertEqual(rows[0][6], self.rows[0]['expected'])
        view = case_plans.get(self.pid)
        self.assertEqual(view['session'], '完整设计任务')
        self.assertEqual(view['rows'][0]['status'], 'Fail')
        self.assertEqual(view['rows'][0]['evidence']['run_id'], run['id'])
        self.assertEqual(len(self.workbook_rows(case_plans.export(self.pid))), 3)

    def test_new_task_with_same_design_does_not_overwrite_previous_delivery(self):
        self.execute(self.queue(self.candidate()))
        previous = Path(self.written['delivery_excel']).read_bytes()
        replacement = plan.write(self.pid, str(self.root / 'design.json'))
        self.assertNotEqual(replacement['delivery_excel'], self.written['delivery_excel'])
        self.assertEqual(Path(self.written['delivery_excel']).read_bytes(), previous)
        self.assertEqual([row[9] for row in self.workbook_rows(replacement['delivery_excel'])], ['未执行'] * 3)

    def test_old_pending_run_and_late_candidate_cannot_bind_to_new_identical_task(self):
        candidate = self.candidate()
        old = plan.show()
        recording = {'project_id': self.pid, 'plan_at_start': old}
        self.assertIsNone(session._plan_design(recording, 'DESIGN-1'))
        candidate['extensions']['test_case_document'] = recording['test_case_document']
        run = self.queue(candidate)
        frozen = execution_inputs.load(run)['cases'][candidate['id']]
        plan.mark('DESIGN-3', '阻塞', '必要测试账号尚未提供')
        replacement = plan.write(self.pid, str(self.root / 'design.json'))
        before = Path(replacement['delivery_excel']).read_bytes()
        self.assertIsNone(session._plan_design(recording, 'DESIGN-1'))
        self.assertEqual(recording['test_case_document']['source']['task_id'], old['task_id'])
        self.assertIn('error', plan.bind_execution('DESIGN-1', self.pid, candidate, None))
        self.assertIn('error', plan.bind_execution('DESIGN-1', self.pid, frozen, run['id']))
        self.execute(run)
        self.assertEqual(Path(replacement['delivery_excel']).read_bytes(), before)
        self.assertEqual([row['status'] for row in plan.show()['cases']], ['未执行'] * 3)
        self.assertEqual([row[9] for row in self.workbook_rows(replacement['delivery_excel'])], ['未执行'] * 3)
        old_rows = self.workbook_rows(old['delivery_excel'])
        self.assertEqual([row[9] for row in old_rows], ['失败', '未执行', '未执行'])
        self.assertIn('必要测试账号尚未提供', old_rows[2][8])
        self.assertEqual(old_rows[0][6], self.rows[0]['expected'])
        synchronized = plan.sync(run_id=run['id'])
        self.assertEqual(synchronized['task_id'], old['task_id'])
        self.assertTrue(synchronized['archived_task'])
        self.assertEqual(synchronized['test_report']['status'], 'ready')
        self.assertEqual(synchronized['test_report']['summary_counts']['Fail'], 1)

    def test_unrelated_latest_success_cannot_replace_bound_failure(self):
        candidate = self.candidate()
        original = self.queue(candidate)
        self.execute(original)
        unrelated = runner.create(self.pid, [candidate['id']], candidate_cases=[candidate])
        self.execute(unrelated, passed=True)
        shown = plan.show()
        self.assertEqual(shown['cases'][0]['status'], '失败')
        self.assertEqual(self.workbook_rows(shown['delivery_excel'])[0][9], '失败')

    def test_recording_failure_without_run_and_unverified_observation_are_retained(self):
        failed = self.candidate()
        failed['extensions']['recording_result'] = {'status': 'Fail', 'actual': '错误值', 'expected': '独立预期'}
        self.assertNotIn('error', plan.bind_execution('DESIGN-1', self.pid, failed, None))
        unverified = self.candidate('TC-API-UNVERIFIED')
        self.assertNotIn('error', plan.bind_execution('DESIGN-2', self.pid, unverified, None))
        rows = self.workbook_rows(plan.show()['delivery_excel'])
        self.assertEqual([row[9] for row in rows], ['失败', '未执行', '未执行'])
        self.assertIn('录制观察', rows[0][8])
        self.assertEqual(db.one('SELECT COUNT(*) AS n FROM runs')['n'], 0)
        self.assertEqual(projects.cases(self.pid), [])

    def test_recorded_failure_then_actual_failed_replay_is_reported_as_business_failure(self):
        candidate = self.candidate()
        candidate['extensions']['recording_result'] = {'status': 'Fail', 'actual': '错误值', 'expected': '独立预期'}
        self.assertNotIn('error', plan.bind_execution('DESIGN-1', self.pid, candidate, None))
        run = self.queue(candidate)
        self.assertEqual(self.execute(run)['status'], 'Fail')
        shown = plan.show()
        report = shown['test_report']
        markdown = Path(report['markdown']).read_text(encoding='utf-8')
        bugs = Path(report['bugs']).read_text(encoding='utf-8')
        self.assertNotIn('尚未回放', markdown)
        self.assertIn('原观察与后续实际回放分别保留', markdown)
        snapshot = json.loads((task_reports._folder(report['report_id']) / 'snapshot.json').read_bytes())
        issue = task_bugs.issues(snapshot)[0]
        self.assertIn('business', issue['kinds'])
        self.assertIn('recording', issue['kinds'])
        self.assertIn(run['id'], issue['run_ids'])
        self.assertIn('业务断言失败', bugs)
        self.assertNotIn('尚未回放', self.workbook_rows(shown['delivery_excel'])[0][8])
        self.assertEqual(self.workbook_rows(shown['delivery_excel'])[0][6], self.rows[0]['expected'])
        self.assertEqual(projects.cases(self.pid), [])

    def test_bound_retest_history_and_publication_are_explicit_without_borrowing_latest(self):
        candidate = self.candidate()
        candidate['extensions']['execution'] = {'api_only': True}
        candidate['extensions']['test_case_document'] = {'source': plan.show()['cases'][0]['execution_source']}
        staged = candidate_publication.stage(self.pid, [candidate], {}, {'fixture': 'exact bound retest'})
        first = candidate_publication.queue(staged['id'], start=False)
        first_run = db.one('SELECT * FROM runs WHERE id = ?', (first['run_id'],))
        self.assertEqual(self.execute(first_run)['status'], 'Fail')
        second = candidate_publication.queue(staged['id'], start=False)
        second_run = db.one('SELECT * FROM runs WHERE id = ?', (second['run_id'],))
        self.assertEqual(self.execute(second_run, passed=True)['status'], 'Pass')
        evidence = case_plans.get(self.pid)['rows'][0]['evidence']
        self.assertEqual(evidence['status'], 'Fail')
        self.assertTrue(evidence['has_historical_failure'])
        self.assertEqual([(item['run_id'], item['status']) for item in evidence['execution_history']],
                         [(first['run_id'], 'Fail'), (second['run_id'], 'Pass')])
        latest = evidence['latest_execution']
        self.assertTrue(latest['evidence_valid'])
        self.assertEqual(latest['publication']['published_ids'], [candidate['id']])
        self.assertEqual(latest['case_id'], candidate['id'])
        self.assertIn(first['run_id'] + '：失败', evidence['actual'])
        self.assertIn(second['run_id'] + '：通过', evidence['actual'])
        unrelated = runner.create(self.pid, [candidate['id']], candidate_cases=[candidate], api_only=True)
        self.execute(unrelated)
        self.assertEqual(case_plans.get(self.pid)['rows'][0]['evidence']['latest_execution']['run_id'], second['run_id'])
        result = db.one('SELECT * FROM results WHERE run_id = ?', (second['run_id'],))
        receipt = json.loads(result['detail'])['attempts'][0]
        data_refs.resolve(receipt['path']).unlink()
        invalid = case_plans.get(self.pid)['rows'][0]['evidence']['latest_execution']
        self.assertEqual(invalid['status'], 'Fail')
        self.assertFalse(invalid['evidence_valid'])

    def test_failed_export_retry_uses_frozen_rows_without_execution_or_publication(self):
        run = self.queue(self.candidate())
        self.execute(run)
        shown = plan.show()
        report = shown['test_report']
        before_runs = db.one('SELECT COUNT(*) AS n FROM runs')['n']
        before_results = db.one('SELECT COUNT(*) AS n FROM results')['n']
        Path(report['excel']).write_bytes(b'broken')
        self.assertEqual(task_reports.get(report['report_id'])['status'], 'failed')
        retried = task_reports.retry(report['report_id'])
        self.assertEqual(retried['status'], 'ready', retried)
        self.assertEqual([row[9] for row in self.workbook_rows(retried['excel'])], ['失败', '未执行', '未执行'])
        self.assertEqual(db.one('SELECT COUNT(*) AS n FROM runs')['n'], before_runs)
        self.assertEqual(db.one('SELECT COUNT(*) AS n FROM results')['n'], before_results)
        self.assertEqual(projects.cases(self.pid), [])

    def test_design_change_does_not_relabel_old_task_or_accept_old_recording(self):
        candidate = self.candidate()
        declared = plan.show()['cases'][0]
        candidate['extensions']['test_case_document'] = {'source': {'business_row_hash': plan._source_hash(declared)}}
        run = self.queue(candidate)
        self.execute(run)
        original_report = plan.show()['test_report']['report_id']
        book, sheet, columns = plan._open()
        sheet.cell(2, columns['expected'], '新的独立需求')
        plan._save(book)
        book.close()
        self.assertIn('error', plan.bind_execution('DESIGN-1', self.pid, candidate, None))
        plan.sync(run_id=run['id'])
        self.assertEqual(plan.show()['cases'][0]['status'], '未执行')
        self.assertEqual(self.workbook_rows(task_reports.download(original_report))[0][9], '失败')
        self.assertEqual(self.workbook_rows(plan.show()['delivery_excel'])[0][6], self.rows[0]['expected'])

    def test_retry_of_initial_report_does_not_overwrite_later_results(self):
        initial_report = self.written['test_report']['report_id']
        run = self.queue(self.candidate())
        self.execute(run)
        current = Path(plan.show()['delivery_excel']).read_bytes()
        initial = task_reports.get(initial_report)
        Path(initial['excel']).unlink()
        restored = task_reports.retry(initial_report)
        self.assertEqual(restored['status'], 'ready', restored)
        self.assertFalse(restored['delivery_updated'])
        self.assertEqual(Path(plan.show()['delivery_excel']).read_bytes(), current)
        self.assertEqual([row[9] for row in self.workbook_rows(restored['excel'])], ['未执行'] * 3)
