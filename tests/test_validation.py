import os
import subprocess
import sys
import unittest

import test_qa_engine as fixtures

qa = fixtures.qa

class ValidationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.Tests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.Tests.tearDownClass.__func__)
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown
    cases = fixtures.Tests.cases
    config = fixtures.Tests.config

    def test_vacuous_schema_rejected(self):
        for schema in [True, {}, {'description': 'anything'}, {'anyOf': [True, {'required': ['code']}]}]:
            with self.subTest(schema=schema):
                self.c['assertions'] = {'status': 200, 'schema': schema}
                self.cases([self.c])
                self.assertTrue(qa.validate(self.p))

    def test_literal_credential_rejected_before_export(self):
        for field, value in [
            ('headers', {'Authorization': 'Bearer QA_SYNTHETIC_SECRET'}),
            ('json', {'nested': {'password': 'QA_SYNTHETIC_SECRET'}}),
            ('extensions', {'access_token': 'QA_SYNTHETIC_SECRET'}),
        ]:
            with self.subTest(field=field):
                c = dict(self.c, **{field: value})
                self.cases([c])
                with self.assertRaises(ValueError):
                    qa.export(self.p)
        self.assertFalse((self.p / 'output/接口测试用例.xlsx').exists())

    def test_env_credential_reference_can_be_exported_without_expansion(self):
        from openpyxl import load_workbook
        os.environ['QA_REGRESSION_TOKEN'] = 'PRIVATE_SYNTHETIC_VALUE'
        self.addCleanup(os.environ.pop, 'QA_REGRESSION_TOKEN', None)
        self.c['headers'] = {'Authorization': '${ENV:QA_REGRESSION_TOKEN}'}
        self.cases([self.c])
        qa.export(self.p)
        w = load_workbook(self.p / 'output/接口测试用例.xlsx')
        text = ' '.join(str(c.value) for row in w['测试用例'] for c in row)
        w.close()
        self.assertIn('${ENV:QA_REGRESSION_TOKEN}', text)
        self.assertNotIn('PRIVATE_SYNTHETIC_VALUE', text)

    def test_invalid_container_gives_errors_not_traceback(self):
        targets = ['testcases/api-cases.json', 'analysis/requirements.json', 'execution/api-results.json']
        for rel in targets:
            before = qa.read(self.p / rel)
            for bad in [{'cases': []}, [None], [[]], ['bad']]:
                with self.subTest(rel=rel, bad=bad):
                    qa.write(self.p / rel, bad)
                    self.assertTrue(qa.validate(self.p))
                    qa.write(self.p / rel, before)
        qa.write(self.p / 'testcases/api-cases.json', {'cases': []})
        result = subprocess.run([sys.executable, str(qa.ROOT / 'scripts/qa.py'), 'validate', str(self.p)], capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)

    def test_invalid_pointer_escapes_rejected(self):
        for ptr in ['/bad~2key', '/trailing~', 'bad']:
            with self.subTest(ptr=ptr):
                self.c['assertions']['json_equals'] = {ptr: 0}
                self.cases([self.c])
                self.assertTrue(qa.validate(self.p))
                with self.assertRaises(ValueError):
                    qa.pointer({}, ptr)
        self.assertEqual(qa.pointer({'a/b': {'~': 0}}, '/a~1b/~0'), 0)
    def test_auth_header_variants_are_not_exported(self):
        for key in ['X-Api-Key','X-Auth-Token','client_secret']:
            with self.subTest(key=key):
                self.c['headers']={key:'QA_SYNTHETIC_SECRET'}
                self.cases([self.c])
                self.assertTrue(qa.validate(self.p))

    def test_external_response_schema_is_rejected_without_request(self):
        self.c['assertions']={'status':200,'schema':{'type':'object','required':['code'],'properties':{'code':{'$ref':'https://external.invalid/schema'}}}}
        self.cases([self.c])
        self.assertTrue(qa.validate(self.p))
        with self.assertRaises(ValueError):
            qa.run_api(self.p)

    def test_save_rejects_bad_schema_before_mutating_cases(self):
        before=(self.p/'testcases/api-cases.json').read_bytes()
        self.c['assertions']={'status':200,'schema':{'type':5}}
        with self.assertRaises(ValueError):
            qa.save_cases(self.p,[self.c])
        self.assertEqual((self.p/'testcases/api-cases.json').read_bytes(),before)

    def test_execution_error_cannot_leave_previous_pass_current(self):
        from unittest.mock import patch

        import httpx
        qa.run_api(self.p)
        fake=httpx.Response(200,json={'code':0})
        # This transport result has no elapsed timer: force a genuine executor error after request return.
        with patch.object(httpx.Client,'request',return_value=fake):
            rows=qa.run_api(self.p)
        self.assertEqual(rows[0]['status'],'Blocked')
        self.assertEqual(qa.read(self.p/'execution/api-results.json')[0]['status'],'Blocked')
        self.assertTrue(list((self.p/'execution/history').rglob('api-results.json')))

if __name__ == '__main__':
    unittest.main(verbosity=2)
