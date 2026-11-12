// Projects and cases: see what exists, run one case or a batch, open a case to read its steps.
import { h, icon, replace, api, scope, scopeQuery, go, statusPill, copyText, attempt, toast, fmtTime, route, refresh } from '../core.js';
import { empty, errorState, runButton, startRun, drawer, closeOverlay } from '../ui.js';
import { state, refreshShell } from '../shell.js';

const RISKS = { write: '写入数据（新建、修改）', delete: '删除数据', payment: '支付', refund: '退款', message: '发送真实消息' };
const selected = new Map();  // "project/case" -> {project_id, case_id}
let kind = '';
let query = '';

const keyOf = (item) => item.project_id + '/' + item.case_id;
const kindTag = (value) => h('span', { class: 'tag' }, icon(value === 'ui' ? 'globe' : 'api'), value === 'ui' ? '页面' : '接口');

function agentPrompt(project) {
  const skill = state.status ? state.status.skill_path : '<Skill 目录>';
  return `请使用 autoSkill 技能（目录：${skill}）为项目「${project.name}」录制用例：先阅读 SKILL.md，运行 autoskill context ${project.id} 了解登录方式和已有用例，然后录制 <要验证的业务流程>，用 autoskill check 写上断言，结束后回放确认通过。`;
}

export async function render(view) {
  let data;
  try {
    data = await api('/cases' + scopeQuery());
  } catch (error) {
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  const s = scope();
  const projects = s.projects.length ? state.projects.filter((p) => s.projects.includes(p.id)) : state.projects;
  for (const key of [...selected.keys()]) if (!data.items.some((item) => keyOf(item) === key)) selected.delete(key);

  const list = h('div', { class: 'stack' });
  const bar = h('div', { class: 'select-bar', hidden: true });
  const search = h('input', { class: 'input', type: 'search', placeholder: '搜索用例编号、标题或模块', value: query, 'aria-label': '搜索用例', style: 'width:280px' });
  search.addEventListener('input', () => { query = search.value; paint(); });
  const kinds = h('div', { class: 'seg', role: 'group', 'aria-label': '用例类型' },
    [['', '全部'], ['ui', '页面'], ['api', '接口']].map(([value, label]) => h('button', {
      'aria-pressed': String(kind === value),
      onClick: (event) => { kind = value; for (const b of kinds.children) b.setAttribute('aria-pressed', String(b === event.currentTarget)); paint(); },
    }, label)));

  function visible(project) {
    const text = query.trim().toLowerCase();
    return data.items.filter((item) => item.project_id === project.id && (!kind || item.kind === kind)
      && (!text || [item.case_id, item.title, item.module].join(' ').toLowerCase().includes(text)));
  }

  function paintBar() {
    const items = [...selected.values()];
    bar.hidden = !items.length;
    if (!items.length) return;
    const byProject = new Map();
    for (const item of items) byProject.set(item.project_id, [...(byProject.get(item.project_id) || []), item.case_id]);
    replace(bar,
      h('span', { class: 'grow' }, h('b', {}, `已选 ${items.length} 条用例`), byProject.size > 1 ? ` · ${byProject.size} 个项目` : '',
        h('span', { class: 'muted' }, '　前置用例（如登录）会自动先执行')),
      h('button', { class: 'btn ghost', onClick: () => { selected.clear(); paint(); } }, '清除'),
      runButton('运行所选', () => [...byProject].map(([project_id, case_ids]) => ({ project_id, case_ids }))));
  }

  function paint() {
    replace(list, projects.map((project) => projectCard(project, visible(project), paint)));
    if (!projects.length) {
      replace(list, h('div', { class: 'card' }, empty({ glyph: 'folder', title: '没有符合筛选的项目', text: '换一个项目筛选，或新建一个项目。' })));
    }
    paintBar();
  }

  replace(view,
    h('div', { class: 'page-head' },
      h('div', { class: 'grow' }, h('h1', {}, '项目与用例'),
        h('p', { class: 'sub' }, `${projects.length} 个项目 · ${data.total} 条用例。用例由 AI 录制得到，在这里可以单条或批量回放。`)),
      h('button', { class: 'btn', onClick: () => newProject() }, icon('plus'), '新建项目')),
    state.projects.length ? h('div', { class: 'toolbar' }, kinds, search) : null,
    list, bar);
  if (!state.projects.length) {
    replace(list, h('div', { class: 'card' }, empty({
      glyph: 'spark', title: '还没有项目', text: '新建一个项目并填写要测试的地址，或先加载示例项目看看效果。',
      actions: [h('button', { class: 'btn primary', onClick: () => newProject() }, icon('plus'), '新建项目'),
        h('a', { class: 'btn', href: '#/dashboard' }, '回到仪表盘加载示例')],
    })));
  } else {
    paint();
  }
  if (route().params.get('new')) newProject();
}

function projectCard(project, items, repaint) {
  const allKeys = items.map(keyOf);
  const allOn = allKeys.length > 0 && allKeys.every((key) => selected.has(key));
  const master = h('input', { class: 'check', type: 'checkbox', checked: allOn, 'aria-label': `选择「${project.name}」的全部用例` });
  master.indeterminate = !allOn && allKeys.some((key) => selected.has(key));
  master.addEventListener('change', () => {
    for (const item of items) { if (master.checked) selected.set(keyOf(item), item); else selected.delete(keyOf(item)); }
    repaint();
  });
  const missing = project.missing_secrets || [];
  const head = h('div', { class: 'card-head project-head' },
    h('div', { class: 'grow' },
      h('h2', { class: 'row' }, project.name, project.category && h('span', { class: 'tag' }, project.category)),
      h('p', { class: 'sub ellipsis' }, `${project.base_url} · 页面 ${project.cases.ui} · 接口 ${project.cases.api} · `,
        project.login.ui_case || project.login.api_case ? '已定义登录方式，运行时自动登录' : '未定义登录方式')),
    project.cases.total > 0 && runButton('运行全部', () => [{ project_id: project.id }], { small: true, primary: false }),
    h('button', { class: 'btn sm icon', 'aria-label': `「${project.name}」设置`, title: '项目设置', onClick: () => projectSettings(project.id) }, icon('edit')));

  const banner = missing.length ? h('div', { class: 'card-body', style: 'padding-bottom:0' }, h('div', { class: 'banner' }, icon('lock'),
    h('span', {}, `缺少凭据 ${missing.join('、')}：依赖它的用例会被阻塞。凭据只保存在本机，不随项目分享。`),
    h('button', { class: 'btn sm', onClick: () => projectSettings(project.id) }, '去填写'))) : null;

  let body;
  if (!project.cases.total) {
    const prompt = agentPrompt(project);
    body = empty({ glyph: 'spark', title: '这个项目还没有用例',
      text: '把下面这句话交给你的 AI 助手，它会操作页面并录制成可回放的用例。',
      actions: h('button', { class: 'btn primary', onClick: () => copyText(prompt) }, icon('copy'), '复制给 AI 的指令') });
    body.append(h('pre', { class: 'prompt' }, prompt));
  } else if (!items.length) {
    body = empty({ glyph: 'cases', title: '没有符合条件的用例', text: '换一个类型或搜索词试试。' });
  } else {
    body = h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, h('th', { style: 'width:36px' }, master), h('th', {}, '用例'), h('th', {}, '类型'), h('th', {}, '模块'),
        h('th', {}, '最近结果'), h('th', {}, '最近执行'), h('th', { class: 'actions' }, ''))),
      h('tbody', {}, items.map((item) => {
        const key = keyOf(item);
        const box = h('input', { class: 'check', type: 'checkbox', checked: selected.has(key), 'aria-label': `选择 ${item.case_id}` });
        box.addEventListener('click', (event) => event.stopPropagation());
        box.addEventListener('change', () => { if (box.checked) selected.set(key, item); else selected.delete(key); repaint(); });
        const deps = JSON.parse(item.depends_on || '[]');
        const run = h('button', { class: 'btn sm', title: '只运行这一条', onClick: (event) => { event.stopPropagation(); startRun([{ project_id: item.project_id, case_ids: [item.case_id] }], run); } }, icon('play'), '运行');
        return h('tr', { class: 'click' + (selected.has(key) ? ' selected' : ''), onClick: () => caseDrawer(item.project_id, item.case_id) },
          h('td', {}, box),
          h('td', {}, h('div', { class: 'title' }, item.title),
            h('div', { class: 'sub mono' }, item.case_id, deps.length ? `　前置 ${deps.join('、')}` : '')),
          h('td', {}, kindTag(item.kind)),
          h('td', { class: 'sub' }, item.module),
          h('td', {}, item.last_status ? statusPill(item.last_status) : h('span', { class: 'status idle' }, icon('blocked'), '未执行')),
          h('td', { class: 'sub num' }, fmtTime(item.last_run_at)),
          h('td', { class: 'actions' }, run));
      })));
  }
  return h('section', { class: 'card' }, head, banner, h('div', { style: 'margin-top:12px' }, body));
}

function newProject() {
  const name = h('input', { class: 'input', id: 'np-name', placeholder: '例如：订单中心', autofocus: true });
  const url = h('input', { class: 'input', id: 'np-url', placeholder: 'http://10.0.0.8:8080', inputMode: 'url' });
  const category = h('input', { class: 'input', id: 'np-cat', placeholder: '可选，例如：交易' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const save = h('button', { class: 'btn primary' }, '创建项目');
  save.addEventListener('click', async () => {
    error.hidden = true;
    try {
      save.disabled = true;
      const project = await api('/projects', { method: 'POST', body: { name: name.value, url: url.value, category: category.value } });
      await refreshShell();
      closeOverlay();
      toast(`项目「${project.name}」已创建`);
      go('cases', { projects: project.id, new: null }, false);
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
    } finally { save.disabled = false; }
  });
  drawer({
    title: '新建项目', subtitle: '填写要测试的地址即表示允许 autoSkill 访问这个地址；其他地址一律不会访问。',
    body: [
      h('div', { class: 'field' }, h('label', { for: 'np-name' }, '项目名称'), name),
      h('div', { class: 'field' }, h('label', { for: 'np-url' }, '要测试的地址'), url, h('span', { class: 'hint' }, '内网地址即可，例如 http://10.0.0.8:8080。')),
      h('div', { class: 'field' }, h('label', { for: 'np-cat' }, '分类'), category),
      error,
    ],
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save],
  });
}

async function projectSettings(projectId) {
  let project;
  try { project = await api('/projects/' + projectId); } catch (error) { toast(error.message, { bad: true }); return; }
  const name = h('input', { class: 'input', value: project.name, id: 'ps-name' });
  const category = h('input', { class: 'input', value: project.category, id: 'ps-cat' });
  const extra = project.allowed_origins.filter((origin) => origin !== project.base_url);
  const origins = h('input', { class: 'input', value: extra.join(', '), id: 'ps-origins', placeholder: '例如 http://10.0.0.9:9000，多个用逗号分隔' });
  const approvals = Object.entries(RISKS).map(([value, label]) => {
    const box = h('input', { class: 'check', type: 'checkbox', checked: project.approvals.includes(value), value });
    return h('label', { class: 'row' }, box, label);
  });
  const save = h('button', { class: 'btn primary' }, '保存');
  save.addEventListener('click', async () => {
    const done = await attempt(() => api('/projects/' + projectId, { method: 'PATCH', body: {
      name: name.value, category: category.value,
      allowed_origins: origins.value.split(/[,，\s]+/).filter(Boolean),
      approvals: approvals.map((row) => row.firstChild).filter((box) => box.checked).map((box) => box.value),
    } }), save);
    if (done) { await refreshShell(); closeOverlay(); toast('已保存'); go('cases'); }
  });

  const secretRows = project.missing_secrets.map((secret) => {
    const input = h('input', { class: 'input', type: 'password', placeholder: '输入后保存', autocomplete: 'off', 'aria-label': secret });
    const button = h('button', { class: 'btn' }, '保存');
    const row = h('div', { class: 'row' }, h('span', { class: 'mono', style: 'min-width:150px' }, secret), input, button);
    button.addEventListener('click', async () => {
      const done = await attempt(() => api('/secrets/' + secret, { method: 'PUT', body: { value: input.value } }), button);
      if (done) { replace(row, h('span', { class: 'mono', style: 'min-width:150px' }, secret), statusPill('Pass', '已保存到本机')); refreshShell(); }
    });
    return row;
  });
  const session = project.session;
  const forget = h('button', { class: 'btn' }, '清除登录态');
  forget.addEventListener('click', async () => {
    const done = await attempt(() => api(`/projects/${projectId}/session`, { method: 'DELETE' }), forget);
    if (done) { toast('已清除，下次运行会重新登录'); closeOverlay(); }
  });

  drawer({
    title: '项目设置', subtitle: project.base_url,
    body: [
      h('div', { class: 'stack' }, h('h3', {}, '基本信息'),
        h('div', { class: 'field' }, h('label', { for: 'ps-name' }, '项目名称'), name),
        h('div', { class: 'field' }, h('label', { for: 'ps-cat' }, '分类'), category)),
      h('div', { class: 'stack' }, h('h3', {}, '登录'),
        h('p', { class: 'sub' }, project.login.ui_case || project.login.api_case
          ? `登录用例：${[project.login.ui_case, project.login.api_case].filter(Boolean).join('、')}。运行依赖登录的用例时会自动先登录，并在本机复用登录态。`
          : '还没有登录用例。让 AI 用 autoskill record start … --as-login 录制一次登录即可。'),
        session.saved && h('div', { class: 'row' }, h('span', { class: 'sub grow' }, `登录态保存于 ${fmtTime(session.saved_at)}（已加密，仅本机可用）`), forget)),
      h('div', { class: 'stack' }, h('h3', {}, '凭据'),
        secretRows.length ? secretRows : h('p', { class: 'sub' }, '用例需要的凭据都已保存在本机。'),
        h('p', { class: 'hint sub' }, '凭据加密保存在本机，只能写入、不能查看，不会进入用例、报告或分享包。')),
      h('details', { class: 'more' }, h('summary', {}, '访问范围与高风险操作'),
        h('div', { class: 'stack', style: 'margin-top:12px' },
          h('div', { class: 'field' }, h('label', { for: 'ps-origins' }, '额外允许访问的地址'), origins,
            h('span', { class: 'hint' }, `始终允许：${project.base_url}。页面用到其他内网服务（如统一登录）时在这里添加。`)),
          h('div', { class: 'field' }, h('span', { class: 'label' }, '允许用例执行的操作'), approvals,
            h('span', { class: 'hint' }, '未勾选的操作类型，相关用例会被阻塞而不是执行。')))),
      h('details', { class: 'more' }, h('summary', {}, '导出'),
        h('div', { class: 'row wrap', style: 'margin-top:12px' },
          h('a', { class: 'btn', href: `/api/projects/${projectId}/report`, download: '' }, icon('table'), 'Excel 测试报告'),
          h('a', { class: 'btn', href: `/api/projects/${projectId}/postman?part=collection`, download: '' }, icon('api'), 'Postman 集合'),
          h('a', { class: 'btn', href: `/api/projects/${projectId}/postman?part=environment`, download: '' }, 'Postman 环境'),
          h('span', { class: 'hint sub' }, '凭据值不会导出，在 Postman 环境里自行填写。'))),
    ],
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save],
  });
}

async function caseDrawer(projectId, caseId) {
  let item;
  try { item = await api(`/projects/${projectId}/cases/${encodeURIComponent(caseId)}`); } catch (error) { toast(error.message, { bad: true }); return; }
  const body = [];
  if (item.depends_on.length) {
    body.push(h('div', { class: 'banner info' }, icon('link'), h('span', {}, `前置用例：${item.depends_on.join('、')}。运行这条用例时会自动先执行。`)));
  }
  if (item.kind === 'ui') {
    body.push(h('div', { class: 'stack', style: 'gap:8px' }, h('h3', {}, '回放步骤'),
      h('ol', { class: 'steps' },
        h('li', {}, h('span', { class: 'step-text' }, '打开 ' + item.start_url)),
        (item.steps || []).map((step) => h('li', { class: step.check ? 'is-check' : '' },
          h('span', { class: 'step-text' }, step.check && h('span', { class: 'tag accent' }, '断言'), step.text),
          step.locators.length > 1 && h('span', { class: 'step-sub' }, `备选定位：${step.locators.slice(1).join(' / ')}`))))));
    if (!item.replayable) body.push(h('div', { class: 'banner' }, icon('warn'), '这条用例没有可回放的步骤。'));
    else body.push(h('div', { class: 'row wrap' }, h('span', { class: 'sub' }, '独立脚本（不依赖 autoSkill）：'),
      ['playwright', 'selenium'].map((engine) => h('a', { class: 'btn sm', download: '', href: `/api/projects/${projectId}/cases/${encodeURIComponent(caseId)}/script?engine=${engine}` },
        engine === 'playwright' ? 'Playwright .py' : 'Selenium .py'))));
  } else {
    const request = item.request;
    body.push(h('div', { class: 'stack', style: 'gap:8px' }, h('h3', {}, '请求'),
      h('div', { class: 'req-line' }, h('span', { class: 'method ' + request.method.toLowerCase() }, request.method), h('span', { class: 'mono' }, request.path)),
      ['headers', 'params', 'json'].filter((part) => request[part] && Object.keys(request[part]).length).map((part) => h('div', {},
        h('div', { class: 'sub', style: 'margin:6px 0 4px' }, { headers: '请求头', params: '查询参数', json: '请求体' }[part]),
        h('pre', { class: 'code' }, JSON.stringify(request[part], null, 2))))));
    const rules = item.assertions || {};
    const rows = [['状态码', rules.status]];
    for (const [pointer, value] of Object.entries(rules.json_equals || {})) rows.push([`字段 ${pointer}`, JSON.stringify(value)]);
    for (const [name, value] of Object.entries(rules.header_contains || {})) rows.push([`响应头 ${name} 包含`, value]);
    for (const value of rules.body_contains || []) rows.push(['响应内容包含', value]);
    if (rules.schema) rows.push(['响应结构', '与录制时一致（字段和类型）']);
    if (rules.max_ms) rows.push(['响应时间不超过', rules.max_ms + ' ms']);
    body.push(h('div', { class: 'stack', style: 'gap:8px' }, h('h3', {}, '断言'),
      h('table', { class: 'table kv' }, h('tbody', {}, rows.map(([label, value]) => h('tr', {}, h('td', { class: 'sub' }, label), h('td', { class: 'mono' }, String(value))))))));
    const extract = Object.entries(item.extract || {});
    if (extract.length) body.push(h('p', { class: 'sub' }, '为后续用例提供变量：' + extract.map(([name, pointer]) => `{{${name}}} ← ${pointer}`).join('，')));
    if (item.traffic === 'resource' || item.traffic === 'static') body.push(h('div', { class: 'banner info' }, icon('info'), '这是「页面资源或静态数据文件可访问」检查，不是业务接口测试。'));
  }
  const run = h('button', { class: 'btn primary' }, icon('play'), '运行这条用例');
  run.addEventListener('click', async () => { const started = await startRun([{ project_id: projectId, case_ids: [caseId] }], run); if (started) closeOverlay(); });
  drawer({ title: item.title, subtitle: `${item.id} · ${item.kind === 'ui' ? '页面用例' : '接口用例'} · ${item.module} · 第 ${item.revision} 版`, body, wide: true,
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '关闭'), item.replayable && run] });
}
