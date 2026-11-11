#!/usr/bin/env python3
import datetime,hashlib,json,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def run(output):
 import httpx,jsonschema
 cases=json.loads((ROOT/'references/cases.json').read_text());schema=json.loads((ROOT/'references/case.schema.json').read_text())
 for c in cases:jsonschema.Draft202012Validator(schema).validate(c)
 run_id='RUN-'+uuid.uuid4().hex;run_dir=output/run_id;results=[]
 with httpx.Client(timeout=40,follow_redirects=False) as client:
  for c in cases:
   r={'run_id':run_id,'case_id':c['id'],'case_revision':c['revision'],'case_content_sha256':hashlib.sha256(json.dumps(c,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'Blocked','actual':'','reason':'','assertions':[],'evidence':[]}
   path=c['path']
   if c['method']!='GET' or not path.startswith('/repos/SeleniumHQ/selenium/git/trees/') or any(x in path for x in ['?','#','..','%','\\']):raise ValueError('Out of scope request')
   try:
    response=client.get('https://api.github.com'+path,params=c['params'],headers=c['headers'])
    if response.status_code in [401,403,429] or response.status_code>=500:raise ValueError('访问或服务限制 HTTP '+str(response.status_code))
    checks=[{'name':'HTTP 状态码','passed':response.status_code==c['assertions']['status']}]
    try:
     body=response.json();errors=list(jsonschema.Draft202012Validator(c['assertions']['schema']).iter_errors(body))
     checks.append({'name':'JSON 响应结构','passed':not errors})
     paths={x.get('path') for x in body.get('tree',[]) if isinstance(x,dict)} if isinstance(body,dict) else set()
     extra=c['extensions']['extra_assertions']
     for wanted in extra.get('required_paths',[]):
      if wanted not in paths and body.get('truncated') is True:raise ValueError('响应截断，无法确认必需路径 '+wanted)
      checks.append({'name':'目录包含 '+wanted,'passed':wanted in paths})
     if extra.get('no_nested_paths'):checks.append({'name':'非递归树不含嵌套路径','passed':not any('/' in x for x in paths if isinstance(x,str))})
    except (ValueError,AttributeError,TypeError) as e:
     if str(e).startswith('响应截断'):raise
     checks.append({'name':'JSON 可解析并可校验','passed':False});body={}
    r.update(status='Pass' if all(x['passed'] for x in checks) else 'Fail',actual=f'HTTP {response.status_code}; {len(response.content)} bytes',assertions=checks,reason='' if all(x['passed'] for x in checks) else '断言不符合预期')
    ev={'case_id':c['id'],'status_code':response.status_code,'response_sha256':hashlib.sha256(response.content).hexdigest(),'response_bytes':len(response.content),'elapsed_seconds':response.elapsed.total_seconds(),'tree_sha':body.get('sha') if isinstance(body,dict) else None,'truncated':body.get('truncated') if isinstance(body,dict) else None,'assertions':checks}
    save(run_dir/(c['id']+'-evidence.json'),ev);r['evidence']=[run_id+'/'+c['id']+'-evidence.json']
   except (httpx.HTTPError,ValueError) as e:r['reason']=type(e).__name__+': '+str(e)[:250]
   results.append(r)
 save(run_dir/'api-results.json',results);print(json.dumps({'run_dir':str(run_dir),'results':results},ensure_ascii=False,indent=2))
 return results
if __name__=='__main__':
 try:
  result=run(Path(sys.argv[1]));sys.exit(0 if result and all(x['status']=='Pass' for x in result) else 2)
 except (ImportError,ValueError,IndexError) as e:print('执行器受阻: '+str(e),file=sys.stderr);sys.exit(1)
