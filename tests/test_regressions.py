import json, subprocess, sys, unittest, os
from pathlib import Path
import test_qa_engine as fixtures
qa = fixtures.qa

class RegressionTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.Tests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.Tests.tearDownClass.__func__)
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown
    cases = fixtures.Tests.cases
    config = fixtures.Tests.config

    def test_changed_content_without_revision_cannot_reuse_pass(self):
        qa.run_api(self.p)
        self.c['expected'] = 'changed expectation'
        self.cases([self.c])
        self.assertTrue(qa.validate(self.p))
        self.assertEqual(qa.merged(self.p)[0]['status'],'Not Run')
        with self.assertRaises(ValueError):
            qa.export(self.p)

    def test_revision_change_can_archive_and_rerun(self):
        qa.run_api(self.p)
        self.c['revision'] = 2
        self.cases([self.c])
        r = qa.run_api(self.p)[0]
        self.assertEqual(r['status'], 'Pass')
        self.assertEqual(r['case_revision'], 2)
        old = list((self.p / 'execution/history').rglob('api-results.json'))
        self.assertTrue(old)
        self.assertEqual(qa.read(old[0])[0]['case_revision'], 1)

    def test_save_cases_increments_revision_and_keeps_snapshot(self):
        self.c['schema_version']=qa.VERSION
        self.cases([self.c])
        qa.save_cases(self.p, [self.c])
        self.assertEqual(qa.load_cases(self.p)[0]['revision'], 1)
        changed = dict(self.c, title='new title')
        qa.save_cases(self.p, [changed])
        self.assertEqual(qa.load_cases(self.p)[0]['revision'], 2)
        self.assertTrue(list((self.p / 'versions').rglob('api-cases.json')))

    def test_legacy_schema_migration_is_explicit_and_invalidates_old_content(self):
        qa.run_api(self.p)
        qa.save_cases(self.p,[self.c])
        saved=qa.load_cases(self.p)[0]
        self.assertEqual(saved['schema_version'],'1.1.0')
        self.assertEqual(saved['revision'],2)
        self.assertTrue(qa.validate(self.p))
        qa.save_cases(self.p,[self.c])
        self.assertEqual(qa.load_cases(self.p)[0]['revision'],2)
        snapshot=list((self.p/'versions').rglob('api-cases.json'))
        self.assertTrue(any(qa.read(path)[0]['schema_version']=='1.0.0' for path in snapshot))

    def test_project_skill_lists_cases_and_how_to_replay_them(self):
        qa.sync_skills(self.p)
        text = (self.p / 'SKILL.md').read_text(encoding='utf-8')
        self.assertTrue(text.startswith('---\nname: '))
        self.assertIn('autoskill run ', text)
        self.assertIn('| TC-API-1 | 接口 |', text)
        self.assertFalse((self.p / 'case-skills').exists())
if __name__ == '__main__':
    unittest.main(verbosity=2)
