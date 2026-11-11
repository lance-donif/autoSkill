import unittest,tempfile,sys,copy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime import engines
from runtime.service import Manager,create_app
from fastapi.testclient import TestClient
class GenerationTests(unittest.TestCase):
 def fixture(self,m):
  m.create_project('demo',[],[])
  case=engines.core.read(engines.ROOT/'examples/selenium/api-cases.json')[0]
  requirement=next(r for r in engines.core.read(engines.ROOT/'examples/selenium/requirements.json') if r['id']==case['requirement_ids'][0])
  run={'id':'run-'+'c'*32,'project_id':'demo','engine':'ai','kind':'generate','status':'Queued','results':[]};m.put_run(run)
  return case,requirement,run
 def test_ai_generated_cases_are_validated_and_stored_as_project_skill(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=Manager(tmp);case,req,run=self.fixture(m)
   def generate(snapshot,request):engines.core.write(Path(request['generatedFile']),{'cases':[case],'requirements':[req]})
   with patch.object(engines,'invoke_ai',side_effect=generate):m.generate_work(run['id'],'official requirement','api',1)
   self.assertEqual(m.run(run['id'])['status'],'Generated');self.assertEqual(len(engines.core.load_cases(m.project('demo'))),1)
   self.assertTrue((m.project('demo')/'case-skills/api/SKILL.md').is_file());m.close()
 def test_wrong_ai_case_count_does_not_overwrite_project(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=Manager(tmp);case,req,run=self.fixture(m)
   def generate(snapshot,request):engines.core.write(Path(request['generatedFile']),{'cases':[],'requirements':[req]})
   with patch.object(engines,'invoke_ai',side_effect=generate):m.generate_work(run['id'],'official requirement','api',1)
   self.assertEqual(m.run(run['id'])['status'],'Blocked');self.assertEqual(engines.core.load_cases(m.project('demo')),[]);m.close()
 def test_ui_script_rejects_untrusted_code_actions(self):
  with self.assertRaises(ValueError):engines.validate_automation({'start_url':'https://qa.invalid','actions':[{'action':'eval','value':'alert(1)'}],'assertions':[{'assertion':'visible','selector':'h1'}]})
 def test_dashboard_assets_and_sample_import_are_connected(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=create_app(tmp,start_worker=False)
   with TestClient(app,base_url='http://127.0.0.1:8787') as client:
    for url in ['/','/app.js','/style.css']:self.assertEqual(client.get(url).status_code,200)
    response=client.post('/api/examples/import',json={'example':'selenium'});self.assertEqual(response.status_code,200)
    id=response.json()['id'];cases=client.get('/api/projects/'+id+'/cases').json();self.assertEqual(len(cases),24)
    self.assertFalse(client.get('/api/projects/'+id+'/config').json()['execution_authorized'])
if __name__=='__main__':unittest.main()
