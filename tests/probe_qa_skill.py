import importlib.util,json,contextlib,io,traceback
from pathlib import Path
from copy import deepcopy
s=importlib.util.spec_from_file_location('baseline',str(Path(__file__).resolve().parent/'test_qa_engine.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.Tests.setUpClass();output=[]
def probe(name,func):
 t=m.Tests('test_real_assertions_export_and_no_response_secret');t.setUp()
 try:
  result=func(t);output.append({'name':name,**result})
 except Exception as e:output.append({'name':name,'unexpected_error':repr(e)})
 finally:t.tearDown()
def schema_true(t):
 t.c['assertions']={'status':200,'schema':True};t.cases([t.c]);errs=m.qa.validate(t.p);r=m.qa.run_api(t.p)
 return {'validation_errors':errs,'actual_status':r[0]['status'],'finding':'schema=true 可以代替业务断言，仅 HTTP 200 即 Pass'}
def literal_credential(t):
 from openpyxl import load_workbook
 t.c['headers']={'Authorization':'Bearer QA_SYNTHETIC_SECRET'};t.cases([t.c]);m.qa.export(t.p)
 w=load_workbook(t.p/'output/接口测试用例.xlsx');v=[c.value for row in w['测试用例'] for c in row if c.value and 'QA_SYNTHETIC_SECRET' in str(c.value)];w.close()
 return {'export_contains_synthetic_token':bool(v),'finding':'导出器未自动拦截或脱敏明文认证字段；使用纯合成凭据验证'}
def bad_container(t):
 m.qa.write(t.p/'testcases/api-cases.json',{'cases':[t.c]})
 try:m.qa.validate(t.p);return {'actual':'accepted'}
 except Exception as e:return {'exception':type(e).__name__,'finding':'JSON 顶层类型错误触发未处理异常，未返回规范校验信息'}
def change_no_revision(t):
 m.qa.run_api(t.p);t.c['expected']='修改后的业务预期';t.cases([t.c]);errors=m.qa.validate(t.p)
 return {'validation_errors':errors,'status_in_export_model':m.qa.merged(t.p)[0]['status'],'finding':'内容变更但未递增 revision 时，旧结果仍视为有效'}
def stale_reexecution(t):
 m.qa.run_api(t.p);t.c['revision']=2;t.cases([t.c])
 try:m.qa.run_api(t.p);return {'actual':'executed'}
 except ValueError as e:return {'actual':'blocked_before_archive','reason':str(e),'finding':'递增版本后 run-api 在归档前被旧结果校验阻断，需手动归档；缺专用命令'}
def json_pointer(t):
 t.c['assertions']['json_equals']={'/bad~2key':0};t.cases([t.c]);return {'validation_errors':m.qa.validate(t.p),'finding':'非法 JSON Pointer ~2 转义未被校验器拒绝'}
def redirected(t):
 from unittest.mock import patch
 import httpx
 t.c['assertions']={'status':200,'json_equals':{'/code':0}};t.cases([t.c]);n=[]
 def request(client,method,url,**kwargs):
  n.append(url);return httpx.Response(302,headers={'location':'https://unauthorized.invalid'},request=httpx.Request(method,url),extensions={})
 # use real local server alternative omitted: mock response needs elapsed
 return {'actual':'covered_by_existing_client_configuration','finding':'本探针未计入断言测试数量'}
with contextlib.redirect_stdout(io.StringIO()):
 for name,fn in [('weak_schema',schema_true),('credential_export',literal_credential),('malformed_container',bad_container),('revision_not_incremented',change_no_revision),('revision_reexecution',stale_reexecution),('invalid_pointer',json_pointer)]:probe(name,fn)
m.Tests.tearDownClass()
p=Path(__file__).resolve().parents[1]/'reports/qa-probe-results.json';p.write_text(json.dumps(output,ensure_ascii=False,indent=2));print(p.read_text())
