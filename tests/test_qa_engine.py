import copy, importlib.util, json, tempfile, threading, unittest, os
from unittest.mock import patch
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
spec=importlib.util.spec_from_file_location('qa',str(Path(__file__).resolve().parents[1]/'skills/autoskill/scripts/qa.py'))
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)
class Handler(BaseHTTPRequestHandler):
    count=0
    def do_GET(self):
        type(self).count+=1
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"code":0,"secret":"must-never-be-saved"}')
    def log_message(self,*a):pass
class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        cls.origin='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)/'project';qa.init(self.p)
        qa.write(self.p/'analysis/requirements.json',[{'id':'REQ-1','module':'M','title':'reads code','source':'PRD#1'}])
        self.c={'schema_version':'1.0.0','id':'TC-API-1','kind':'api','revision':1,'lifecycle':'active','requirement_ids':['REQ-1'],'module':'M','title':'read','priority':'P1','preconditions':'available','steps':['GET /ok'],'test_data':{},'expected':'200 and code 0','source':'OpenAPI','risk':'read','tags':[],'extensions':{},'api_source':'openapi','source_ref':'input/spec.json#/ok','method':'GET','path':'/ok','assertions':{'status':200,'json_equals':{'/code':0}}}
        self.cases([self.c]);self.cfg={'mode':'auto','execution_authorized':True,'base_url':self.origin,'allowed_origins':[self.origin],'approvals':[]};self.config(self.cfg)
    def tearDown(self):self.tmp.cleanup()
    def cases(self,cases):qa.write(self.p/'testcases/api-cases.json',cases)
    def config(self,cfg):qa.write(self.p/'qa-config.json',cfg)
    def test_real_assertions_export_and_no_response_secret(self):
        self.assertEqual(qa.run_api(self.p)[0]['status'],'Pass');self.assertFalse(qa.validate(self.p))
        qa.export(self.p);qa.report(self.p)
        manifest=qa.read(self.p/'output/export-manifest.json');self.assertEqual(manifest['schema_version'],qa.VERSION)
        for f in (self.p/'execution/logs').glob('*.json'):self.assertNotIn('must-never-be-saved',f.read_text(encoding='utf-8'))
        self.assertEqual(qa.coverage(self.p)['ratio'],1)
    def test_http_clients_use_configured_proxy_for_public_targets(self):
        from autoskill.replay import api
        env={'HTTP_PROXY':self.origin,'http_proxy':self.origin,'NO_PROXY':'','no_proxy':''}
        with patch.dict(os.environ,env):
            with api.new_client(self.cfg) as client:
                response=client.get('http://unresolvable.invalid/ok')
                self.assertEqual(response.json()['code'],0)
            self.cfg.update(base_url='http://unresolvable.invalid',allowed_origins=[])
            self.config(self.cfg)
            self.assertEqual(qa.run_api(self.p)[0]['status'],'Pass')
    def test_http_clients_accept_dsh_bracketed_ipv6_no_proxy(self):
        from autoskill.replay import api
        with patch.dict(os.environ,{'NO_PROXY':'localhost,127.0.0.1,::1,[::1]','no_proxy':'localhost,127.0.0.1,::1,[::1]'}):
            with api.new_client(self.cfg) as client:
                self.assertEqual(client.get(self.origin+'/ok').status_code,200)
            self.assertEqual(qa.run_api(self.p)[0]['status'],'Pass')
        with patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:9','http_proxy':'http://127.0.0.1:9','NO_PROXY':'','no_proxy':''}):
            with api.new_client(self.cfg) as client:
                self.assertEqual(client.get(self.origin+'/ok').status_code,200)
            self.assertEqual(qa.run_api(self.p)[0]['status'],'Pass')
    def test_no_authorization_means_no_request(self):
        self.cfg['execution_authorized']=False;self.config(self.cfg);n=Handler.count
        self.assertEqual(qa.run_api(self.p)[0]['status'],'Blocked');self.assertEqual(Handler.count,n)
    def test_inferred_is_blocked(self):
        self.c['api_source']='inferred';self.cases([self.c]);n=Handler.count
        self.assertEqual(qa.run_api(self.p)[0]['status'],'Blocked');self.assertEqual(Handler.count,n)
    def test_write_needs_specific_approval(self):
        self.c.update(method='POST',risk='read');self.cases([self.c]);n=Handler.count
        self.assertEqual(qa.run_api(self.p)[0]['status'],'Blocked');self.assertEqual(Handler.count,n)
    def test_bad_assertion_fails_and_archive_preserves_evidence(self):
        self.c['assertions']['json_equals']['/code']=1;self.cases([self.c]);self.assertEqual(qa.run_api(self.p)[0]['status'],'Fail')
        qa.run_api(self.p);self.assertTrue(list((self.p/'execution/history').rglob('api-results.json')))
    def test_stale_result_rejected(self):
        qa.run_api(self.p);self.c['revision']=2;self.cases([self.c]);self.assertTrue(any('旧版本' in x for x in qa.validate(self.p)))
    def test_no_200_only_or_unknown_assertions(self):
        self.c['assertions']={'status':200};self.cases([self.c]);self.assertTrue(qa.validate(self.p))
        self.c['assertions']={'status':200,'made_up':1};self.cases([self.c]);self.assertTrue(qa.validate(self.p))
    def test_path_escape_and_origin(self):
        for bad in ['//evil.test/a','/%2e%2e/x','/a/../x','/a\\x','https://evil.test','/x?q=y','/%252f%252fevil.test']:
            with self.subTest(bad=bad):
                c=dict(self.c,path=bad)
                with self.assertRaises(ValueError):qa.request_url(self.cfg,c)
        self.assertEqual(qa.request_url(dict(self.cfg,allowed_origins=[]),self.c),qa.request_url(self.cfg,self.c))
    def test_excel_formula_is_text(self):
        from openpyxl import load_workbook
        self.c['title']='=1+1';self.cases([self.c]);qa.export(self.p)
        wb=load_workbook(self.p/'output/接口测试用例.xlsx');ws=wb['测试用例'];col=[c.value for c in ws[1]].index('用例标题')+1
        self.assertEqual(ws.cell(2,col).data_type,'s');wb.close()
    def test_pointer_array_negative_rejected(self):
        with self.assertRaises(KeyError):qa.pointer([1], '/-1')
    def test_schema_version_and_unknown_requirement(self):
        self.c['schema_version']='2.0.0';self.c['requirement_ids']=['unknown'];self.cases([self.c]);self.assertTrue(qa.validate(self.p))
if __name__=='__main__':unittest.main(verbosity=2)
