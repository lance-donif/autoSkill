import unittest,json,hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime import engines
class TodoFixtureTests(unittest.TestCase):
 def test_twenty_ui_cases_are_script_readable(self):
  cases=engines.core.read(engines.ROOT/'examples/todomvc/ui-cases.json');self.assertEqual(len(cases),20)
  self.assertEqual(len({c['id'] for c in cases}),20)
  for c in cases:engines.validate_automation(c['extensions']['automation'])
 def test_upstream_source_bytes_match_manifest(self):
  folder=engines.ROOT/'examples/todomvc';manifest=engines.core.read(folder/'source-manifest.json')
  for item in manifest['files']:
   value=(folder/'site'/item['file']).read_bytes();sha=hashlib.sha1(b'blob '+str(len(value)).encode()+b'\0'+value).hexdigest();self.assertEqual(sha,item['source_blob'])
  value=(folder/'LICENSE').read_bytes();self.assertEqual(hashlib.sha1(b'blob '+str(len(value)).encode()+b'\0'+value).hexdigest(),manifest['license_blob'])
 def test_api_examples_are_control_api_not_fabricated_todo_crud(self):
  for c in engines.core.read(engines.ROOT/'examples/todomvc/api-cases.json'):
   self.assertEqual(c['method'],'GET');self.assertIn(c['path'],['/api/status','/api/projects','/api/runs','/api/tasks'])
if __name__=='__main__':unittest.main()
