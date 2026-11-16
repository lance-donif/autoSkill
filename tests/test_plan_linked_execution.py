"""Saved-case replay binds the original task before enqueue and shares lock order with readers."""
import json
import os
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_execution_inputs as inputs
from openpyxl import load_workbook

from autoskill import builder, candidate_publication, case_plans, conversation_capture, db, plan, plan_execution, projects, runner, task_reports


class LinkedExecutionTests(unittest.TestCase):
    setUpClass = classmethod(inputs.ExecutionInputTests.setUpClass.__func__)
    tearDownClass = classmethod(inputs.ExecutionInputTests.tearDownClass.__func__)
    execute = inputs.ExecutionInputTests.execute

    def setUp(self):
        inputs.ExecutionInputTests.setUp(self)
        environment = patch.dict(os.environ, {"AUTOSKILL_SESSION": "linked-sheet-task", "AUTOSKILL_WORKFLOW_BINDING": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.source = self.folder / "design.json"
        self.source.write_text(json.dumps([{"id": "DESIGN-1", "title": "First business result"},
                                           {"id": "DESIGN-2", "title": "Other business result"}]), encoding="utf-8")
        self.written = plan.write(self.pid, str(self.source))
        plan.link("DESIGN-1", self.pid, self.case["id"], 1)

    def statuses(self, file):
        book = load_workbook(file)
        try:
            return [row[9] for row in book["功能测试用例"].values if row[9] in {"未执行", "通过", "失败", "业务未验证", "阻塞"}]
        finally:
            book.close()

    def test_prepare_only_binds_selected_frozen_cases_and_rollback_is_not_execution(self):
        other = builder.api_case("TC-API-002", "Other result", "GET", "/other", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [self.case, other])
        plan.link("DESIGN-2", self.pid, other["id"], 1)
        original = Path(self.written["delivery_excel"]).read_bytes()
        with patch.object(db, "connect", side_effect=AssertionError("Preparation must not use SQLite")), \
                patch.object(task_reports, "for_design", side_effect=AssertionError("Preparation must not render")):
            prepared = runner.prepare(self.pid, [self.case["id"]])
        bindings = plan_execution._read()["bindings"]
        self.assertEqual([(item["plan_case"], item["run_id"]) for item in bindings], [("DESIGN-1", prepared["id"])])
        self.assertEqual(bindings[0]["task_id"], self.written["task_id"])
        self.assertEqual(Path(self.written["delivery_excel"]).read_bytes(), original)
        self.assertEqual(db.rows("SELECT * FROM runs"), [])
        with self.assertRaisesRegex(ValueError, "rollback"), db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            runner.enqueue(prepared, connection=connection)
            raise ValueError("rollback")
        plan.sync(run_id=prepared["id"])
        shown = plan.show()
        self.assertEqual([row["status"] for row in shown["cases"]], ["未执行", "未执行"])
        self.assertIn("尚无执行回执", shown["cases"][0]["note"])
        self.assertEqual(self.statuses(shown["delivery_excel"]), ["未执行", "未执行"])
        self.assertEqual(self.server.requests, [])

    def test_old_prepared_run_updates_its_original_delivery_after_identical_task_replacement(self):
        prepared = runner.prepare(self.pid, [self.case["id"]])
        old_initial = Path(self.written["initial_excel"]).read_bytes()
        replacement = plan.write(self.pid, str(self.source))
        plan.link("DESIGN-1", self.pid, self.case["id"], 1)
        current_excel = Path(replacement["delivery_excel"]).read_bytes()
        current_internal = Path(replacement["excel"]).read_bytes()
        self.assertNotEqual(self.written["task_id"], replacement["task_id"])
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.folder / "unrelated")}):
            final, _ = self.execute(prepared)
        self.assertEqual(final["status"], "Pass")
        self.assertEqual(self.server.requests, [("/business", None)])
        self.assertEqual(self.statuses(self.written["delivery_excel"]), ["通过", "未执行"])
        self.assertEqual(Path(self.written["initial_excel"]).read_bytes(), old_initial)
        self.assertEqual(Path(replacement["delivery_excel"]).read_bytes(), current_excel)
        self.assertEqual(Path(replacement["excel"]).read_bytes(), current_internal)
        self.assertEqual([row["status"] for row in plan.show()["cases"]], ["未执行", "未执行"])

    def test_later_recording_archive_and_failure_keep_earlier_task_passes_consistent(self):
        self.source.write_text(json.dumps([{'id': f'DESIGN-{number}', 'title': f'Business row {number}'}
                                           for number in range(1, 5)]), encoding='utf-8')
        written = plan.write(self.pid, str(self.source))
        initial = Path(written['initial_excel']).read_bytes()

        def capture(label):
            request = {'request': {'method': 'GET', 'url': self.server_url + '/business'},
                       'response': {'status': 200, 'content': {'mimeType': 'application/json', 'text': '{"ok":true}'}},
                       '_resourceType': 'fetch'}
            return conversation_capture.capture(self.pid, {'session_id': label, 'steps': []}, {'log': {'entries': [request]}})

        def queued(row_id, identity, expected):
            case = builder.api_case(identity, row_id, 'GET', '/business', {'status': 200, 'json_equals': {'/ok': expected}})
            case['extensions']['execution'] = {'api_only': True}
            row = next(item for item in plan.show()['cases'] if item['id'] == row_id)
            case['extensions']['test_case_document'] = {'source': row['execution_source']}
            task = candidate_publication.stage(self.pid, [case], {}, {'fixture': row_id})
            return candidate_publication.queue(task['id'], start=False)['run_id']

        self.server_url = f'http://127.0.0.1:{self.server.server_port}'
        first_capture = capture('first-recording')
        first = queued('DESIGN-1', 'API-FIRST', True)
        self.assertEqual(runner.execute(first)['status'], 'Pass')
        plan.link('DESIGN-4', self.pid, 'API-FIRST', 1)
        repeated = runner.create(self.pid, ['API-FIRST'])
        self.assertEqual(runner.execute(repeated['id'])['status'], 'Pass')
        self.assertEqual(case_plans.get(self.pid)['summary']['Pass'], 2)
        capture('next-recording')
        second = queued('DESIGN-2', 'API-SECOND', False)
        self.assertEqual(runner.execute(second)['status'], 'Fail')
        view = case_plans.get(self.pid)
        self.assertEqual([row['status'] for row in view['rows']], ['Pass', 'Fail', 'NotRun', 'Pass'])
        self.assertEqual((view['summary']['Pass'], view['test_report']['summary_counts']['Pass']), (2, 2))
        self.assertEqual(self.statuses(written['delivery_excel']), ['通过', '失败', '未执行', '通过'])
        self.assertEqual(Path(written['initial_excel']).read_bytes(), initial)
        self.assertEqual(view['rows'][0]['evidence']['latest_execution']['run_id'], repeated['id'])
        self.assertEqual(view['rows'][3]['evidence']['latest_execution']['run_id'], repeated['id'])
        self.assertIsNotNone(conversation_capture.get(self.pid, first_capture['id']))
        self.assertEqual({case['id'] for case in projects.cases(self.pid)}, {self.case['id'], 'API-FIRST'})

    def process(self, code):
        environment = {**os.environ, "PYTHONPATH": str(inputs.ROOT / "skills/autoskill/app")}
        child = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=environment)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate()
        self.addCleanup(cleanup)
        return child

    def line(self, child):
        output = queue.Queue()
        threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True).start()
        return output.get(timeout=10).strip()

    def test_dashboard_read_and_project_locked_plan_writer_finish_without_lock_inversion(self):
        writer = self.process("from autoskill import plan\nfrom autoskill.locking import project_lock\n"
                              "with project_lock('inputs'):\n print('locked', flush=True)\n input()\n"
                              " plan.mark('DESIGN-1', '阻塞', 'synthetic lock-order verification')\nprint('written', flush=True)")
        self.assertEqual(self.line(writer), "locked")
        reader = self.process("from autoskill import case_plans\nprint('reading', flush=True)\n"
                              "value = case_plans.get('inputs', session='linked-sheet-task')\nprint(value['rows'][0]['id'], flush=True)")
        self.assertEqual(self.line(reader), "reading")
        threading.Event().wait(0.25)
        written, writer_error = writer.communicate("continue\n", timeout=15)
        read, reader_error = reader.communicate(timeout=15)
        self.assertEqual((writer.returncode, reader.returncode), (0, 0), writer_error + reader_error)
        self.assertIn("written", written)
        self.assertIn("DESIGN-1", read)


if __name__ == "__main__":
    unittest.main()
