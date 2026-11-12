"""Build source-grounded, deterministic TodoMVC cases (no model call)."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'examples/todomvc'
def action(op,selector=None,**kw):return {'action':op,**({'selector':selector} if selector else {}),**kw}
def assertion(op,selector,expected=None,**kw):return {'assertion':op,'selector':selector,**({'expected':expected} if expected is not None else {}),**kw}
def add(text):return [action('fill','.new-todo',value=text),action('press','.new-todo',key='Enter')]
one=add('autoSkill A');two=one+add('autoSkill B');complete_two=two+[action('check','.todo-list li:first-child .toggle')]
rows=[
('空列表展示',[],[assertion('visible','h1'),assertion('count','.todo-list li',0)]),
('新增一条任务',one,[assertion('count','.todo-list li',1),assertion('text','.todo-list li label','autoSkill A')]),
('连续新增与顺序',two,[assertion('count','.todo-list li',2),assertion('text','.todo-list li:first-child label','autoSkill A'),assertion('text','.todo-list li:last-child label','autoSkill B')]),
('拒绝仅空白的任务',add('   '),[assertion('count','.todo-list li',0)]),
('新任务裁剪首尾空白',add('   autoSkill A   '),[assertion('text','.todo-list li label','autoSkill A')]),
('特殊字符按纯文本显示',add('<b>QA & test</b>'),[assertion('text','.todo-list li label','<b>QA & test</b>'),assertion('count','.todo-list li b',0)]),
('提交后清空输入',one,[assertion('value','.new-todo','')]),
('Unicode任务录入',add('待办测试 ✨'),[assertion('text','.todo-list li label','待办测试 ✨')]),
('任务标记完成',one+[action('check','.todo-list .toggle')],[assertion('count','.todo-list li.completed',1),assertion('text','.todo-count','0 items left')]),
('完成状态可恢复',one+[action('check','.todo-list .toggle'),action('uncheck','.todo-list .toggle')],[assertion('count','.todo-list li.completed',0),assertion('text','.todo-count','1 item left')]),
('回车保存编辑',one+[action('dblclick','.todo-list li label'),action('fill','.todo-list li .edit',value='autoSkill edited'),action('press','.todo-list li .edit',key='Enter')],[assertion('text','.todo-list li label','autoSkill edited')]),
('Escape取消编辑',one+[action('dblclick','.todo-list li label'),action('fill','.todo-list li .edit',value='discard this draft'),action('press','.todo-list li .edit',key='Escape')],[assertion('text','.todo-list li label','autoSkill A')]),
('失焦保存编辑',one+[action('dblclick','.todo-list li label'),action('fill','.todo-list li .edit',value='saved on blur'),action('click','h1')],[assertion('text','.todo-list li label','saved on blur')]),
('编辑后裁剪空白',one+[action('dblclick','.todo-list li label'),action('fill','.todo-list li .edit',value='  trimmed  '),action('press','.todo-list li .edit',key='Enter')],[assertion('text','.todo-list li label','trimmed')]),
('Active仅展示未完成',complete_two+[action('click','.filters a[href="#/active"]')],[assertion('count','.todo-list li',1),assertion('text','.todo-list li label','autoSkill B')]),
('Completed仅展示完成',complete_two+[action('click','.filters a[href="#/completed"]')],[assertion('count','.todo-list li',1),assertion('text','.todo-list li label','autoSkill A')]),
('All恢复显示全部',complete_two+[action('click','.filters a[href="#/completed"]'),action('click','.filters a[href="#/"]')],[assertion('count','.todo-list li',2)]),
('剩余数量复数展示',two,[assertion('text','.todo-count','2 items left')]),
('刷新后列表清空（原版为内存存储）',complete_two+[action('reload')],[assertion('count','.todo-list li',0),assertion('value','.new-todo','')]),
('刷新后保留筛选路由',complete_two+[action('click','.filters a[href="#/active"]'),action('reload')],[assertion('attribute','.filters a[href="#/active"]','selected',attribute='class'),assertion('count','.todo-list li',0)])]
source='https://github.com/tastejs/todomvc/tree/master/examples/javascript-es5'
requirements=[];ui=[]
for i,(title,actions,checks) in enumerate(rows,1):
 rid=f'REQ-TODO-{i:03}';requirements.append({'id':rid,'module':'TodoMVC','title':title,'source':source})
 ui.append({'schema_version':'1.1.0','id':f'TC-TODO-UI-{i:03}','kind':'ui','revision':1,'lifecycle':'active','requirement_ids':[rid],'module':'TodoMVC','title':title,'priority':'P0' if i in [2,9,11,19] else 'P1','preconditions':'官方原版TodoMVC通过本地8765端口提供；每条用例使用全新浏览器上下文，仅合成任务数据','steps':['打开本地官方TodoMVC页面']+[json.dumps(a,ensure_ascii=False) for a in actions]+['逐项校验预期断言'],'test_data':'合成autoSkill任务；无个人数据','expected':'；'.join(json.dumps(c,ensure_ascii=False) for c in checks),'source':source,'risk':'write' if actions else 'read','tags':['todomvc','script-ready','local-open-source'],'extensions':{'automation':{'start_url':'http://127.0.0.1:8765/','actions':actions,'assertions':checks},'source_subtree':'fa45eeebb0e99339ebd77269bb4596fc5a2f047f'}})
api=[]
for i,(path,title,check) in enumerate([('/api/status','autoSkill服务身份',{'json_equals':{'/name':'autoSkill'}}),('/api/projects','项目列表结构',{'schema':{'type':'array','items':{'type':'object','required':['id','name','cases','authorized']}}}),('/api/runs','运行列表结构',{'schema':{'type':'array','items':{'type':'object','required':['id','status']}}}),('/api/tasks','定时任务列表结构',{'schema':{'type':'array','items':{'type':'object','required':['id','enabled','rule']}}})],1):
 rid=f'REQ-CONTROL-{i:03}';requirements.append({'id':rid,'module':'autoSkill本地控制台API','title':title,'source':'runtime/service.py 中已实现的GET接口；测试不是TodoMVC业务API'})
 api.append({'schema_version':'1.1.0','id':f'TC-CONTROL-API-{i:03}','kind':'api','revision':1,'lifecycle':'active','requirement_ids':[rid],'module':'autoSkill本地控制台API','title':title,'priority':'P1','preconditions':'autoSkill本地8787后台已启动且用户已授权只读接口验证','steps':['独立GET请求'+path,'校验HTTP200及JSON断言'],'test_data':{},'expected':'HTTP200且响应符合结构或身份断言','source':'runtime/service.py:create_app','risk':'read','tags':['local-control-api','script-ready'],'extensions':{},'api_source':'source-and-official-docs','source_ref':'runtime/API.md','method':'GET','path':path,'headers':{},'params':{},'assertions':{'status':200,**check}})
for name,data in [('ui-cases.json',ui),('api-cases.json',api),('requirements.json',requirements)]: (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
lines=['# 本地开源 TodoMVC 测试需求','',f'源项目：{source}','本地site文件与上游Git blob校验值一致，未修改业务代码；许可证和来源清单随附。','', '范围：20条UI用例，包括新增、空白校验、字符转义、状态、编辑、筛选和刷新后的行为（原版使用内存存储，刷新即清空）。每条使用新浏览器上下文，仅合成数据。不包含删除、批量清空、兼容性和全部无障碍验收。','', 'TodoMVC本身是本地存储页面，没有业务CRUD API。附带4条API用例只验证autoSkill控制台自身的公开只读接口，不冒充TodoMVC接口。','', '| 用例 | 功能 |','| --- | --- |']+[f"| {c['id']} | {c['title']} |" for c in ui]
(OUT/'requirements.md').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n')
print('Built20 executable UI definitions and4 clearly scoped control API definitions')
