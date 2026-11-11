"""Loopback-only autoSkill API, durable tasks and execution history."""
import argparse,contextlib,datetime,json,re,sqlite3,threading,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from urllib.parse import urlsplit
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from . import engines

UTC=datetime.timezone.utc

def next_due(rule,now,previous=None):
    now=now.astimezone(UTC)
    kind=rule.get('kind')
    if kind=='interval':
        seconds=rule.get('seconds')
        if not isinstance(seconds,int) or isinstance(seconds,bool) or seconds<60:raise ValueError('间隔至少60秒')
        if previous:
            periods=max(1,int((now-previous).total_seconds()//seconds)+1)
            return previous+datetime.timedelta(seconds=seconds*periods)
        return now+datetime.timedelta(seconds=seconds)
    if kind=='at':
        if not isinstance(rule.get('at'),str):raise ValueError('一次任务缺少时间')
        value=datetime.datetime.fromisoformat(rule['at'].replace('Z','+00:00'))
        if value.tzinfo is None or value<=now:raise ValueError('一次任务必须是带时区的未来时间')
        return value.astimezone(UTC)
    if kind=='daily':
        try:zone=ZoneInfo(rule.get('timezone','Asia/Shanghai'))
        except (ZoneInfoNotFoundError,ValueError,TypeError):raise ValueError('无效的IANA时区') from None
        value=rule.get('time','')
        if not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):raise ValueError('时间格式应为HH:mm')
        hour,minute=map(int,value.split(':'));date=now.astimezone(zone).date()
        for offset in range(4):
            candidate=datetime.datetime.combine(date+datetime.timedelta(days=offset),datetime.time(hour,minute),zone)
            if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)!=candidate.replace(tzinfo=None):continue
            if candidate>now:return candidate.astimezone(UTC)
        raise ValueError('无法计算下一次任务时间')
    raise ValueError('不支持的定时规则')

class Manager:
    def __init__(self,root,ui_executable=None):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        for folder in ['projects','runs']:(self.root/folder).mkdir(exist_ok=True)
        self.db_path=self.root/'autoskill.sqlite';self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=2);self.stop=threading.Event();self.ui_executable=ui_executable
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL, enabled INTEGER NOT NULL, next_due TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            for row in db.execute('SELECT id,data FROM runs').fetchall():
                run=json.loads(row[1])
                if run['status'] in ('Queued','Running'):
                    run.update(status='Blocked',reason='宿主重启，中断的运行未自动重放',finished_at=engines.stamp());db.execute('UPDATE runs SET data=? WHERE id=?',(json.dumps(run),row[0]))
    @contextlib.contextmanager
    def db(self):
        conn=sqlite3.connect(self.db_path,timeout=30);conn.execute('PRAGMA journal_mode=WAL')
        try:
            with conn:yield conn
        finally:conn.close()
    def project(self,id):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',id):raise ValueError('项目ID格式无效')
        p=self.root/'projects'/id
        if not (p/'project.json').is_file():raise ValueError('项目不存在')
        return p
    def projects(self):
        return [{'id':p.name,'name':p.name,'cases':len(engines.core.load_cases(p)),'authorized':engines.core.read(p/'qa-config.json').get('execution_authorized',False)} for p in sorted((self.root/'projects').iterdir()) if p.is_dir() and (p/'project.json').exists()]
    def create_project(self,id,requirements,cases):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',id):raise ValueError('项目名称使用小写字母数字和短横线')
        p=self.root/'projects'/id
        if p.exists():raise ValueError('项目已存在')
        engines.core.init(p)
        try:
            engines.core.write(p/'analysis/requirements.json',requirements)
            engines.core.save_cases(p,cases)
        except Exception:
            import shutil;shutil.rmtree(p);raise
        return {'id':id,'cases':len(cases)}
    def request_ok(self,spec):
        if spec.get('engine') not in ('ai','script') or spec.get('kind') not in ('ui','api'):raise ValueError('请选择AI/脚本和UI/接口类型')
        p=self.project(spec['project_id']);engines.core.require_valid(p,check_results=False)
        cfg=engines.core.read(p/'qa-config.json')
        if cfg.get('execution_authorized') is not True:raise ValueError('项目尚未授权，先配置授权origin与执行范围')
        selected=[c for c in engines.core.load_cases(p) if c['kind']==spec['kind'] and c['lifecycle']=='active' and (not spec.get('case_ids') or c['id'] in spec['case_ids'])]
        if not selected:raise ValueError('没有匹配的活动用例')
        if spec.get('case_ids') and set(spec['case_ids'])!={c['id'] for c in selected}:raise ValueError('选择的用例不存在或类型不匹配')
        if spec['engine']=='script' and spec['kind']=='ui':
            for case in selected:engines.validate_automation(case.get('extensions',{}).get('automation'))
        return p
    def run(self,id):
        if not re.fullmatch(r'run-[0-9a-f]{32}',id):raise ValueError('运行ID无效')
        with self.db() as db:row=db.execute('SELECT data FROM runs WHERE id=?',(id,)).fetchone()
        if not row:raise ValueError('运行不存在')
        return json.loads(row[0])
    def runs(self):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT data FROM runs ORDER BY rowid DESC LIMIT 100').fetchall()]
    def put_run(self,run):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO runs VALUES (?,?)',(run['id'],json.dumps(run,ensure_ascii=False)))
    def queue(self,spec,task_id=None):
        self.request_ok(spec);run={'id':'run-'+uuid.uuid4().hex,**spec,'status':'Queued','created_at':engines.stamp(),'task_id':task_id,'results':[]}
        self.put_run(run);self.pool.submit(self.work,run['id']);return run
    def work(self,id):
        run=self.run(id);run.update(status='Running',started_at=engines.stamp());self.put_run(run)
        try:
            project=self.request_ok(run)
            rows=engines.execute(project,self.root/'runs'/id/'project',run['kind'],run['engine'],run.get('case_ids'),self.ui_executable)
            status='Fail' if any(r['status']=='Fail' for r in rows) else ('Blocked' if any(r['status']!='Pass' for r in rows) else 'Pass')
            run.update(status=status,results=rows,reason='')
            summary=self.root/'runs'/id/'project/ai-execute-turn-summary.json'
            if run['engine']=='ai' and summary.is_file():run['ai_reply_path']=engines.core.read(summary).get('replyArtifact')
        except Exception as e:run.update(status='Blocked',reason=str(e) if isinstance(e,ValueError) else type(e).__name__)
        run['finished_at']=engines.stamp();self.put_run(run)
    def update_cases(self,id,cases):
        with self.lock:return engines.core.save_cases(self.project(id),cases)
    def generate(self,id,requirements,kind,count):
        p=self.project(id)
        if kind not in ('ui','api') or not 1<=count<=200:raise ValueError('用例类型或数量无效')
        run={'id':'run-'+uuid.uuid4().hex,'project_id':id,'engine':'ai','kind':'generate','generation_kind':kind,'status':'Queued','created_at':engines.stamp(),'results':[]}
        self.put_run(run);self.pool.submit(self.generate_work,run['id'],requirements,kind,count);return run
    def generate_work(self,id,requirements,kind,count):
        import shutil
        run=self.run(id);run.update(status='Running',started_at=engines.stamp());self.put_run(run)
        try:
            p=self.project(run['project_id']);snapshot=self.root/'runs'/id/'project';snapshot.mkdir(parents=True)
            authority=['analysis/requirements.json',*[f'testcases/{f}' for f in engines.core.FILES.values()]]
            baseline={rel:(p/rel).read_bytes() for rel in authority}
            for rel in ['project.json','qa-config.json',*authority]:
                target=snapshot/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p/rel,target)
            (snapshot/'input').mkdir();(snapshot/'output').mkdir();(snapshot/'input/requirements.md').write_text(requirements)
            request={'mode':'generate','project':str(snapshot),'kind':kind,'count':count,'mainSkill':str(engines.ROOT/'skills/autoskill/SKILL.md'),'caseSchema':str(engines.ROOT/'skills/autoskill/references/case.schema.json'),'requirementsFile':str(snapshot/'input/requirements.md'),'generatedFile':str(snapshot/'output/generated-cases.json')}
            engines.invoke_ai(snapshot,request)
            generated=engines.core.read(snapshot/'output/generated-cases.json')
            if not isinstance(generated,dict) or not isinstance(generated.get('cases'),list) or not isinstance(generated.get('requirements'),list):raise ValueError('AI生成文件结构不符合契约')
            if len(generated['cases'])!=count or any(not isinstance(c,dict) or c.get('kind')!=kind for c in generated['cases']):raise ValueError('AI没有生成要求数量及类型的用例')
            if kind=='ui':
                for case in generated['cases']:engines.validate_automation(case.get('extensions',{}).get('automation'))
            old=engines.core.load_cases(p);existing={c['id'] for c in old}
            if any(c.get('id') in existing for c in generated['cases']):raise ValueError('AI生成ID与已有用例冲突，未覆盖原数据')
            reqs=engines.core.read(p/'analysis/requirements.json');known={r['id']:r for r in reqs}
            for req in generated['requirements']:
                if not isinstance(req,dict) or not isinstance(req.get('id'),str):raise ValueError('AI需求格式错误')
                if req['id'] in known and req!=known[req['id']]:raise ValueError('AI生成需求与现有ID冲突')
                if req['id'] not in known:reqs.append(req);known[req['id']]=req
            engines.core.write(snapshot/'analysis/requirements.json',reqs)
            engines.core.save_cases(snapshot,old+generated['cases'])
            for channel in ('ui','api'):engines.core.write(snapshot/'execution'/f'{channel}-results.json',[])
            engines.core.export(snapshot);engines.core.report(snapshot)
            with self.lock:
                if any((p/rel).read_bytes()!=value for rel,value in baseline.items()):raise ValueError('AI生成期间权威用例已变化，结果保留待审阅，未覆盖')
                original=engines.core.read(p/'analysis/requirements.json');engines.core.write(p/'analysis/requirements.json',reqs)
                try:engines.core.save_cases(p,old+generated['cases'])
                except Exception:engines.core.write(p/'analysis/requirements.json',original);raise
            run.update(status='Generated',reason=f'已生成并校验{count}条{kind}用例，项目Skill已同步')
            summary=snapshot/'ai-generate-turn-summary.json'
            if summary.is_file():run['ai_reply_path']=engines.core.read(summary).get('replyArtifact')
        except Exception as error:run.update(status='Blocked',reason=str(error) if isinstance(error,ValueError) else type(error).__name__)
        run['finished_at']=engines.stamp();self.put_run(run)
    def explain(self,id):
        run=self.run(id);snapshot=self.root/'runs'/id/'project'
        if not snapshot.is_dir():raise ValueError('运行尚无可解读的快照')
        analysis_id='analysis-'+uuid.uuid4().hex;relative='output/explanations/'+analysis_id+'.md'
        (snapshot/'output/explanations').mkdir(parents=True,exist_ok=True)
        run['ai_explanation_status']='Queued';run['ai_explanation_path']=relative;self.put_run(run)
        def work():
            try:
                engines.invoke_ai(snapshot,{'mode':'explain','project':str(snapshot),'analysisId':analysis_id,'explanationFile':str(snapshot/relative)})
                if not (snapshot/relative).is_file():raise ValueError('AI未保存解读文件')
                outcome='Complete'
            except Exception:outcome='Blocked'
            with self.lock:
                current=self.run(id);current['ai_explanation_status']=outcome;self.put_run(current)
        self.pool.submit(work);return {'status':'Queued'}
    def create_task(self,spec):
        self.request_ok(spec)
        due=next_due(spec['rule'],datetime.datetime.now(UTC));task={'id':'task-'+uuid.uuid4().hex,**spec,'created_at':engines.stamp()}
        with self.db() as db:db.execute('INSERT INTO tasks VALUES (?,?,?,?)',(task['id'],json.dumps(task,ensure_ascii=False),1,due.isoformat()))
        return {**task,'enabled':True,'next_due':due.isoformat()}
    def tasks(self):
        with self.db() as db:return [{**json.loads(r[1]),'enabled':bool(r[2]),'next_due':r[3]} for r in db.execute('SELECT * FROM tasks ORDER BY rowid DESC').fetchall()]
    def toggle_task(self,id,enabled):
        with self.lock,self.db() as db:
            row=db.execute('SELECT data FROM tasks WHERE id=?',(id,)).fetchone()
            if not row:raise ValueError('任务不存在')
            task=json.loads(row[0])
            if enabled:self.request_ok(task);due=next_due(task['rule'],datetime.datetime.now(UTC)).isoformat()
            else:due=next((t['next_due'] for t in self.tasks() if t['id']==id))
            db.execute('UPDATE tasks SET enabled=?, next_due=? WHERE id=?',(int(enabled),due,id))
        return {'id':id,'enabled':enabled}
    def tick(self,now=None):
        now=now or datetime.datetime.now(UTC);dispatch=[];claimed=0
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT * FROM tasks WHERE enabled=1 AND next_due<=?',(now.isoformat(),)).fetchall():
                task=json.loads(row[1]);due=datetime.datetime.fromisoformat(row[3]);rule=task['rule']
                active=any(record.get('task_id')==task['id'] and record.get('status') in ('Queued','Running') for record in (json.loads(item[0]) for item in db.execute('SELECT data FROM runs').fetchall()))
                successor=next_due(rule,now,due) if rule['kind']!='at' else due
                db.execute('UPDATE tasks SET enabled=?,next_due=? WHERE id=?',(int(rule['kind']!='at'),successor.isoformat(),task['id']))
                if active:continue
                claimed+=1;spec={k:task[k] for k in ('project_id','engine','kind','case_ids') if k in task}
                run={'id':'run-'+uuid.uuid4().hex,**spec,'task_id':task['id'],'created_at':engines.stamp(),'status':'Queued','results':[]}
                try:self.request_ok(spec);dispatch.append(run['id'])
                except ValueError as error:run.update(status='Blocked',reason=str(error),finished_at=engines.stamp())
                # Claim and durable run receipt commit together; crashes cannot erase the occurrence.
                db.execute('INSERT INTO runs VALUES (?,?)',(run['id'],json.dumps(run,ensure_ascii=False)))
        for id in dispatch:self.pool.submit(self.work,id)
        return claimed
    def loop(self):
        while not self.stop.wait(1):
            try:self.tick()
            except Exception:pass # Durable state remains; no unbounded retry or false success.
    def close(self):self.stop.set();self.pool.shutdown(wait=True,cancel_futures=True)

class RunRequest(BaseModel):
    project_id:str
    engine:str
    kind:str
    case_ids:list[str]=Field(default_factory=list)
class TaskRequest(RunRequest):
    title:str=Field(min_length=1,max_length=100)
    rule:dict
class ProjectRequest(BaseModel):
    id:str
    requirements:list[dict]
    cases:list[dict]
class CaseUpdate(BaseModel):cases:list[dict]
class Toggle(BaseModel):enabled:bool
class Generation(BaseModel):
    requirements:str=Field(min_length=5,max_length=50000)
    kind:str
    count:int=Field(default=20,ge=1,le=200)


def create_app(root,port=8787,ui_executable=None,start_worker=True):
    manager=Manager(root,ui_executable)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        if start_worker:threading.Thread(target=manager.loop,daemon=True).start()
        yield;manager.close()
    app=FastAPI(title='autoSkill',lifespan=lifespan);app.state.manager=manager
    @app.middleware('http')
    async def same_origin(request,call_next):
        host=urlsplit('http://'+request.headers.get('host','')).hostname
        if host not in ('127.0.0.1','localhost','::1'):return JSONResponse({'detail':'仅允许本机Host'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('origin')
            if (origin and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}')) or request.headers.get('sec-fetch-site')=='cross-site':return JSONResponse({'detail':'拒绝跨站写请求'},status_code=403)
        return await call_next(request)
    import jsonschema
    @app.exception_handler(jsonschema.ValidationError)
    async def schema_error(request,error):return JSONResponse({'detail':'用例数据不符合Schema；请校验字段与版本'},status_code=400)
    @app.exception_handler(ValueError)
    async def value_error(request,error):return JSONResponse({'detail':str(error)},status_code=400)
    @app.get('/api/status')
    def status():return {'name':'autoSkill','schema_version':engines.core.VERSION,'projects':len(manager.projects()),'runs':len(manager.runs()),'tasks':len(manager.tasks()),'ai_runtime':'需要用户配置DSH SDK与模型','script_runtime':'UI: Playwright / API: HTTP','instance_id':__import__('os').environ.get('AUTOSKILL_INSTANCE_ID')}
    @app.get('/api/projects')
    def projects():return manager.projects()
    @app.post('/api/projects')
    def create_project(req:ProjectRequest):return manager.create_project(req.id,req.requirements,req.cases)
    @app.get('/api/projects/{id}/cases')
    def cases(id:str):return engines.core.load_cases(manager.project(id))
    @app.put('/api/projects/{id}/cases')
    def update_cases(id:str,req:CaseUpdate):return manager.update_cases(id,req.cases)
    @app.post('/api/projects/{id}/generate')
    def generate(id:str,req:Generation):return manager.generate(id,req.requirements,req.kind,req.count)
    @app.post('/api/examples/import')
    def example(req:dict):
        name=req.get('example')
        if name not in ('selenium','todomvc'):raise ValueError('不支持的示例')
        id=name+'-demo-'+uuid.uuid4().hex[:5];source=engines.ROOT/'examples'/name
        return manager.create_project(id,engines.core.read(source/'requirements.json'),engines.core.read(source/'ui-cases.json')+engines.core.read(source/'api-cases.json'))
    @app.get('/api/projects/{id}/config')
    def config(id:str):return engines.core.read(manager.project(id)/'qa-config.json')
    @app.put('/api/projects/{id}/config')
    async def update_config(id:str,request:Request):
        data=await request.json();allowed={'mode','execution_authorized','base_url','allowed_origins','approvals','timeout_seconds','cleanup_status','screenshot_masks','ai_profile','ai_timeout_seconds'}
        if not isinstance(data,dict) or set(data)-allowed:raise ValueError('配置存在不支持的字段')
        cfg=engines.core.read(manager.project(id)/'qa-config.json');cfg.update(data)
        if not isinstance(cfg.get('allowed_origins'),list) or any(not isinstance(o,str) or urlsplit(o).scheme not in ('http','https') or urlsplit(o).path for o in cfg['allowed_origins']):raise ValueError('allowed_origins必须是精确HTTP(S) origin数组')
        if not isinstance(cfg.get('execution_authorized'),bool):raise ValueError('execution_authorized必须是布尔值')
        engines.core.write(manager.project(id)/'qa-config.json',cfg);return cfg
    @app.get('/api/runs')
    def runs():return manager.runs()
    @app.post('/api/runs')
    def run(req:RunRequest):return manager.queue(req.model_dump())
    @app.get('/api/runs/{id}')
    def run_detail(id:str):return manager.run(id)
    @app.post('/api/runs/{id}/replay')
    def replay(id:str,req:dict):
        old=manager.run(id)
        return manager.queue({'project_id':old['project_id'],'engine':req.get('engine',old['engine']),'kind':old['kind'],'case_ids':old.get('case_ids',[])})
    @app.post('/api/runs/{id}/explain')
    def explain(id:str):return manager.explain(id)
    @app.get('/api/runs/{id}/artifact/{path:path}')
    def artifact(id:str,path:str):
        manager.run(id)
        if not path.startswith(('execution/','output/','case-skills/')):raise ValueError('不允许访问该产物')
        file=engines.core.safe_file(manager.root/'runs'/id/'project',path)
        if not file.is_file() or file.suffix not in ('.json','.md','.png','.xlsx','.txt'):raise HTTPException(404)
        return FileResponse(file)
    @app.get('/api/tasks')
    def tasks():return manager.tasks()
    @app.post('/api/tasks')
    def create_task(req:TaskRequest):return manager.create_task(req.model_dump())
    @app.patch('/api/tasks/{id}')
    def toggle(id:str,req:Toggle):return manager.toggle_task(id,req.enabled)
    app.mount('/',StaticFiles(directory=engines.ROOT/'ui',html=True),name='dashboard')
    return app

if __name__=='__main__':
    import uvicorn,os
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--port',type=int,default=8787);parser.add_argument('--chromium');args=parser.parse_args()
    uvicorn.run(create_app(args.root,args.port,args.chromium),host='127.0.0.1',port=args.port,log_level='warning')
