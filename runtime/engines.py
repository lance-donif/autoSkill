"""Deterministic UI/API execution; AI is an explicit separate SDK process."""
import datetime,hashlib,importlib.util,json,os,shutil,subprocess,sys,uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('autoskill_core',ROOT/'skills/autoskill/scripts/qa.py')
core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)

def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def safe_origin(url,allowed):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or p.username or p.password or f'{p.scheme}://{p.netloc}' not in allowed:raise ValueError('URL 不在授权 origin 中')

def locator(page,step):
    if 'selector' in step:return page.locator(step['selector'])
    if 'placeholder' in step:return page.get_by_placeholder(step['placeholder'],exact=True)
    if 'role' in step:return page.get_by_role(step['role'],name=step.get('name'),exact=True)
    raise ValueError('动作缺少 selector、placeholder 或 role 定位')

def validate_automation(automation):
    import jsonschema
    schema=core.read(ROOT/'runtime/automation.schema.json')
    errors=list(jsonschema.Draft202012Validator(schema).iter_errors(automation))
    if errors:raise ValueError('UI自动化动作不符合白名单契约')
    for item in [*automation['actions'],*automation['assertions']]:
        if item.get('action')=='reload':continue
        if not any(key in item for key in ('selector','placeholder','role')):raise ValueError('UI动作或断言缺少定位器')
        op=item.get('action')
        if op in ('fill','select') and 'value' not in item:raise ValueError('输入动作缺少value')
        if op=='press' and 'key' not in item:raise ValueError('键盘动作缺少key')
        check=item.get('assertion')
        if check in ('count','text','value','attribute') and 'expected' not in item:raise ValueError('断言缺少expected')
        if check=='count' and (not isinstance(item['expected'],int) or isinstance(item['expected'],bool) or item['expected']<0):raise ValueError('count断言需要非负整数')
        if check=='attribute' and 'attribute' not in item:raise ValueError('attribute断言缺少属性名')

def prepare_run(project,target,kind,case_ids=None):
    core.require_valid(project,check_results=False)
    cfg=core.read(project/'qa-config.json')
    if cfg.get('execution_authorized') is not True:raise ValueError('项目尚未授权执行')
    cases=[c for c in core.load_cases(project) if c['kind']==kind and c['lifecycle']=='active' and (not case_ids or c['id'] in case_ids)]
    if not cases:raise ValueError('没有匹配的活动用例')
    if case_ids and set(case_ids)!={c['id'] for c in cases}:raise ValueError('存在未知、归档或类型不匹配的用例')
    target.mkdir(parents=True,exist_ok=False)
    for rel in ['project.json','qa-config.json','analysis/requirements.json']:
        dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(project/rel,dest)
    for k,f in core.FILES.items():core.write(target/'testcases'/f,cases if k==kind else [])
    for k in core.FILES:core.write(target/'execution'/f'{k}-results.json',[])
    for folder in ['output','knowledge','execution/logs','execution/screenshots']:(target/folder).mkdir(parents=True,exist_ok=True)
    core.sync_skills(target)
    return cases,cfg

def run_ui(snapshot,executable=None):
    from playwright.sync_api import sync_playwright
    cases=core.read(snapshot/'testcases/functional-cases.json');cfg=core.read(snapshot/'qa-config.json');results=[]
    allowed=cfg.get('allowed_origins',[]);run_id='RUN-'+uuid.uuid4().hex
    with sync_playwright() as driver:
        browser=None;launch_error=None
        try:browser=driver.chromium.launch(headless=True,**({'executable_path':executable} if executable else {}))
        except Exception as e:launch_error=type(e).__name__ # Never log browser launch args or inherited secrets.
        try:
            for case in cases:
                r={'run_id':run_id,'case_id':case['id'],'case_revision':case['revision'],'case_content_sha256':core.case_fingerprint(case),'started_at':stamp(),'status':'Blocked','actual':'','reason':'','assertions':[],'evidence':[]};context=None
                log=[]
                try:
                    if launch_error:raise ValueError('浏览器启动失败: '+launch_error)
                    if cfg.get('execution_authorized') is not True:raise ValueError('未获得执行授权')
                    if case['risk']!='read' and case['risk'] not in cfg.get('approvals',[]):raise ValueError('缺少专门风险授权')
                    automation=case.get('extensions',{}).get('automation')
                    if not isinstance(automation,dict) or not automation.get('assertions'):raise ValueError('用例尚无可执行的自动化动作及断言')
                    validate_automation(automation)
                    safe_origin(automation['start_url'],allowed)
                    context=browser.new_context()
                    def route(request):
                        try:safe_origin(request.request.url,allowed);request.continue_()
                        except ValueError:request.abort()
                    context.route('**/*',route)
                    page=context.new_page();page.set_default_timeout(int(cfg.get('timeout_seconds',20))*1000)
                    page.goto(automation['start_url'],wait_until='domcontentloaded');safe_origin(page.url,allowed)
                    for index,step in enumerate(automation.get('actions',[]),1):
                        op=step.get('action');record={'step':index,'action':step,'started_at':stamp(),'status':'started'}
                        log.append(record)
                        if op=='reload':page.reload(wait_until='domcontentloaded')
                        else:
                            node=locator(page,step)
                            if op=='fill':node.fill(core.resolved(step['value']))
                            elif op=='click':node.click()
                            elif op=='dblclick':node.dblclick()
                            elif op=='press':node.press(step['key'])
                            elif op=='check':node.check()
                            elif op=='uncheck':node.uncheck()
                            elif op=='select':node.select_option(step['value'])
                            elif op=='wait_visible':node.wait_for(state='visible')
                            else:raise ValueError('不支持的动作；禁止执行任意代码')
                        safe_origin(page.url,allowed);record.update(status='done',finished_at=stamp())
                    from playwright.sync_api import expect
                    for assertion in automation['assertions']:
                        check={'name':assertion.get('name',assertion['assertion']),'passed':False};r['assertions'].append(check)
                        try:
                            op=assertion['assertion'];node=locator(page,assertion)
                            if op=='count':expect(node).to_have_count(assertion['expected'])
                            elif op=='text':expect(node).to_have_text(assertion['expected'])
                            elif op=='value':expect(node).to_have_value(assertion['expected'])
                            elif op=='visible':expect(node).to_be_visible()
                            elif op=='checked':expect(node).to_be_checked(checked=assertion.get('expected',True))
                            elif op=='attribute':expect(node).to_have_attribute(assertion['attribute'],assertion['expected'])
                            else:raise ValueError('不支持的断言')
                            check['passed']=True
                        except AssertionError:pass
                    image='execution/screenshots/'+uuid.uuid4().hex+'.png'
                    masks=[page.locator('input[type=password]'),*[page.locator(x) for x in cfg.get('screenshot_masks',[])]]
                    page.screenshot(path=str(snapshot/image),full_page=True,mask=masks)
                    r['evidence'].append(image);r.update(status='Pass' if all(a['passed'] for a in r['assertions']) else 'Fail',actual='真实浏览器动作已完成，逐项断言见证据')
                    if r['status']=='Fail':r['reason']='页面结果不符合预期'
                except Exception as e:
                    if log and any(x['status']=='started' for x in log):
                        r.update(status='Fail',actual='页面动作执行失败',reason=type(e).__name__,assertions=[{'name':'所有动作可完成','passed':False}])
                    else:r['reason']=str(e) if isinstance(e,ValueError) else type(e).__name__
                finally:
                    if context:context.close()
                    rel='execution/logs/'+uuid.uuid4().hex+'.json';core.write(snapshot/rel,{'case_id':case['id'],'steps':log,'assertions':r['assertions'],'status':r['status']});r['evidence'].append(rel);results.append(r)
        finally:
            if browser:browser.close()
    core.write(snapshot/'execution/ui-results.json',results);core.require_valid(snapshot);return results

def invoke_ai(snapshot,request,timeout=1800):
    file=snapshot/('ai-'+request.get('mode','execute')+'-'+uuid.uuid4().hex+'-request.json');core.write(file,request)
    command=[shutil.which('node') or 'node',str(ROOT/'plugin/ai-worker.mjs'),str(file)]
    try:result=subprocess.run(command,cwd=str(snapshot),env=os.environ.copy(),capture_output=True,text=True,timeout=int(timeout))
    except (subprocess.TimeoutExpired,OSError) as e:raise ValueError('DSH AI 执行器受阻: '+type(e).__name__) from None
    if result.returncode:raise ValueError('DSH AI 未成功完成；请检查SDK、模型配置与审批通道')

def run_ai(snapshot,kind,config):
    request={'project':str(snapshot.resolve()),'kind':kind,'results':str((snapshot/'execution'/f'{kind}-results.json').resolve()),'skill':str((snapshot/'case-skills'/kind/'SKILL.md').resolve()),'agentBrowserSkill':str(ROOT/'skills/agent-browser/SKILL.md'),'profile':config.get('ai_profile','sdk'),'coreScript':str(ROOT/'skills/autoskill/scripts/qa.py')}
    file=snapshot/'ai-request.json';core.write(file,request)
    command=[shutil.which('node') or 'node',str(ROOT/'plugin/ai-worker.mjs'),str(file)]
    try:
        result=subprocess.run(command,cwd=str(snapshot),env=os.environ.copy(),capture_output=True,text=True,timeout=int(config.get('ai_timeout_seconds',1800)))
        if result.returncode:raise ValueError('DSH AI 执行器未成功完成；请检查SDK、模型配置与审批通道')
        core.require_valid(snapshot)
        rows=core.read(snapshot/'execution'/f'{kind}-results.json')
        expected={c['id'] for c in core.load_cases(snapshot) if c['kind']==kind}
        if {r['case_id'] for r in rows}!=expected:raise ValueError('AI 未提交全部用例结果')
        return rows
    except (subprocess.TimeoutExpired,OSError) as e:raise ValueError('DSH AI 执行器受阻: '+type(e).__name__) from None

def execute(project,output,kind,engine,case_ids=None,executable=None):
    cases,cfg=prepare_run(project,output,kind,case_ids)
    if engine=='script':
        rows=core.run_api(output) if kind=='api' else run_ui(output,executable)
    elif engine=='ai':rows=run_ai(output,kind,cfg)
    else:raise ValueError('不支持的执行引擎')
    core.export(output);core.report(output)
    return rows
