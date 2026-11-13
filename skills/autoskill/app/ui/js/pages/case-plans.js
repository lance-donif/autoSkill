// Editable design definitions and evidence-only execution columns, scoped by project and session.
import { h, icon, replace, api, scope, route, go, href, refresh, attempt, toast, fmtTime } from '../core.js';
import { drawer, closeOverlay, empty, preferredEngine, preferredHeaded } from '../ui.js';

const picked = new Set();
const READ_ONLY = new Set(['actual', 'conclusion', 'executor', 'environment', 'screenshots']);
const field = (label, control, note) => h('div', { class: 'field' }, h('label', { htmlFor: control.id }, label), control, note && h('p', { class: 'hint' }, note));
const url = (project, session, suffix = '') => `/case-plans/${encodeURIComponent(project)}${suffix}?session=${encodeURIComponent(session)}`;

export async function render(view) {
  const all = await api('/projects');
  const scoped = scope().projects;
  const projects = all.filter((item) => !scoped.length || scoped.includes(item.id));
  const params = route().params;
  const projectId = projects.find((item) => item.id === params.get('project'))?.id || projects[0]?.id;
  const session = params.get('session') || '';
  const project = h('select', { class: 'select', id: 'cp-project', 'aria-label': '设计表项目' },
    projects.map((item) => h('option', { value: item.id, selected: item.id === projectId }, item.name)));
  project.addEventListener('change', () => { picked.clear(); go('case-plans', { project: project.value }); });
  const sessionInput = h('input', { class: 'input', id: 'cp-session', value: session, placeholder: '默认会话', 'aria-label': '设计表会话', style: 'max-width:220px' });
  const switchSession = () => { picked.clear(); go('case-plans', { project: projectId, session: sessionInput.value || null }); };
  const selectors = h('div', { class: 'toolbar wrap' }, h('label', { htmlFor: 'cp-project' }, '项目'), project,
    h('label', { htmlFor: 'cp-session' }, '会话'), sessionInput, h('button', { class: 'btn', onClick: switchSession }, '切换会话'));
  if (!projectId) {
    replace(view, heading(), selectors, h('section', { class: 'card' }, empty({ title: '先创建项目，再设计用例',
      actions: h('a', { class: 'btn', href: href('cases', { new: '1' }) }, '新建项目') })));
    return;
  }
  const data = await api(url(projectId, session));
  for (const identity of [...picked]) if (!data.rows.some((item) => item.id === identity)) picked.delete(identity);
  const selection = h('div', { class: 'select-bar' });
  const updateSelection = () => {
    selection.hidden = !picked.size;
    replace(selection, h('span', { class: 'grow' }, `已选 ${picked.size} 条设计`),
      h('button', { class: 'btn', onClick: () => { picked.clear(); refresh(); } }, '清除'),
      h('button', { class: 'btn primary', onClick: () => run(data, [...picked]) }, icon('play'), '回放所选关联用例'));
  };
  const sync = h('button', { class: 'btn', disabled: !data.exists }, icon('reset'), '同步真实结果');
  sync.addEventListener('click', async () => {
    if (await attempt(() => api(url(projectId, session, '/sync'), { method: 'POST', body: { expected_revision: data.revision } }), sync)) refresh();
  });
  const adopt = h('button', { class: 'btn' }, '采用当前会话旧表');
  adopt.addEventListener('click', async () => {
    if (await attempt(() => api(url(projectId, session, '/adopt'), { method: 'POST', body: { expected_revision: data.revision, source_hash: data.legacy.sha256 } }), adopt)) {
      toast('旧表已复制到项目资料；原文件保留，关联后重新验证执行结果'); refresh();
    }
  });
  replace(view, heading(data), selectors,
    h('div', { class: 'toolbar wrap' },
      h('button', { class: 'btn primary', onClick: () => editor(data) }, icon('plus'), '新建设计'),
      h('button', { class: 'btn', disabled: !data.available_cases.length, onClick: () => importCases(data) }, '从已有用例导入'), data.legacy && adopt, sync,
      data.exists && h('a', { class: 'btn', href: '/api' + url(projectId, session, '/excel'), download: '' }, icon('table'), '下载 14 列 Excel'),
      h('button', { class: 'btn', disabled: !data.history.length, onClick: () => history(data) }, icon('clock'), '历史版本')),
    h('div', { class: 'banner info' }, icon('info'), data.note),
    h('section', { class: 'card', style: 'overflow:auto' }, data.rows.length ? table(data, { updateSelection })
      : empty({ glyph: 'table', title: '这份设计表还没有设计用例', text: '创建待实现的设计，或从已有可执行用例导入；每个项目与会话独立保存。' })), selection);
  updateSelection();
}

function heading(data) {
  return h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '设计用例表'),
    h('p', { class: 'sub' }, data ? `${data.project.name} · ${data.session || '默认会话'} · 版本 ${data.version} · ${data.rows.length} 条设计` : '设计、需求关联与可执行用例分开，执行字段按真实证据显示。')));
}

function table(data, { updateSelection, historical = false } = {}) {
  return h('table', { class: 'table', style: 'min-width:1900px' },
    h('thead', {}, h('tr', {}, data.columns.map((column) => h('th', { style: 'min-width:130px' }, column.label,
      READ_ONLY.has(column.key) && h('span', { class: 'sub' }, '（只读）'))))),
    h('tbody', {}, data.rows.map((row) => h('tr', {}, data.columns.map((column) =>
      h('td', { style: 'vertical-align:top;white-space:pre-wrap;max-width:320px' }, cell(data, row, column.key, { updateSelection, historical })))))));
}

function cell(data, row, name, { updateSelection, historical }) {
  if (name === 'id') {
    return h('div', { class: 'stack', style: 'min-width:220px;gap:8px' },
      historical ? h('b', {}, row.id) : h('label', { class: 'row' }, h('input', { class: 'check', type: 'checkbox', checked: picked.has(row.id), 'aria-label': `选择设计 ${row.id}`,
        onChange: (event) => { if (event.target.checked) picked.add(row.id); else picked.delete(row.id); updateSelection(); } }), h('b', {}, row.id)),
      h('p', { class: 'sub' }, row.evidence.materialized_case_id ? `关联：${row.evidence.materialized_case_id}` : '设计草稿 · 未关联可执行用例'),
      !historical && h('div', { class: 'row wrap' }, h('button', { class: 'btn sm', onClick: () => editor(data, row) }, '编辑设计'),
        h('button', { class: 'btn sm', onClick: () => link(data, row) }, row.evidence.materialized_case_id ? '重新关联' : '关联用例'),
        row.evidence.materialized_case_id && h('button', { class: 'btn sm', onClick: () => applyDesign(data, row) }, '应用设计到用例'),
        row.evidence.binding_current && h('button', { class: 'btn sm', onClick: () => run(data, [row.id]) }, '回放')));
  }
  if (name === 'trace') return h('div', {}, row.trace, row.requirement_ids.map((identity) => {
    const requirement = data.requirements.find((item) => item.id === identity);
    return h('p', { class: 'sub' }, requirement?.workflow_id ? h('a', { href: href('workflows/' + requirement.workflow_id) }, requirement.requirement_id) : identity);
  }));
  if (name === 'conclusion') return h('div', {}, h('b', {}, row.conclusion), h('p', { class: 'sub' }, row.evidence.reason),
    row.evidence.run_id && h('a', { href: href('runs/' + row.evidence.run_id) }, '查看真实执行证据'));
  if (name === 'screenshots') return (row.screenshots || []).map((source) => h('a', { href: source, target: '_blank', rel: 'noreferrer' }, '查看截图'));
  return row[name] || (['author', 'preconditions', 'steps', 'expected'].includes(name) ? '【待确认】' : '—');
}

function editor(data, row = null) {
  const definitions = [
    ['id', '用例编号', false], ['title', '用例标题', false], ['module', '模块 / 业务路径', false], ['type', '操作类型', false], ['priority', '优先级', false],
    ['description', '测试说明', true], ['preconditions', '前置条件', true], ['steps', '操作步骤', true], ['expected', '预期结果', true], ['source', '设计来源', true],
    ['business_path', '业务层级（每行一级）', true], ['prefix', '功能编号前缀', false], ['author', '用例编制人', false], ['remarks', '设计备注', true],
  ];
  const values = new Map();
  const structuredSource = h('input', { class: 'check', type: 'checkbox', checked: row?.source != null && typeof row.source === 'object', id: 'cp-source-json' });
  const fields = definitions.map(([name, label, multiline]) => {
    const value = row?.[name] ?? ({ id: 'PLAN-' + String(data.rows.length + 1).padStart(3, '0'), module: '未分组', priority: 'P1' }[name] || '');
    const displayed = name === 'business_path' && Array.isArray(value) ? value.join('\n') : name === 'source' && typeof value === 'object' ? JSON.stringify(value, null, 2) : value;
    const input = h(multiline ? 'textarea' : 'input', { class: 'input', id: 'cp-' + name, value: displayed, rows: multiline ? 4 : null,
      required: ['id', 'title'].includes(name), readOnly: name === 'id' && !!row });
    values.set(name, input);
    return field(label, input);
  });
  const selectedRequirements = new Set(row?.requirement_ids || []);
  const requirements = h('div', { class: 'case-pick' }, data.requirements.map((item) => h('label', { class: 'row' },
    h('input', { class: 'check', type: 'checkbox', checked: selectedRequirements.has(item.id), onChange: (event) => {
      if (event.target.checked) selectedRequirements.add(item.id); else selectedRequirements.delete(item.id);
    } }), h('span', {}, item.requirement_id + ' · ' + item.title))));
  const form = h('form', { class: 'stack' }, fields, h('label', { class: 'row', htmlFor: 'cp-source-json' }, structuredSource, '来源为结构化 JSON（保留对象或数组）'),
    h('div', { class: 'field' }, h('b', {}, '需求关联'), requirements,
    !data.requirements.length && h('p', { class: 'sub' }, '项目还没有已保存需求；可先在需求与工作流中拆分来源。')));
  const save = h('button', { class: 'btn primary' }, '保存设计');
  save.addEventListener('click', async () => {
    if (!form.reportValidity()) return;
    const definition = Object.fromEntries([...values].map(([name, input]) => [name, input.value]));
    definition.business_path = definition.business_path.split('\n').filter((part) => part.trim());
    if (!definition.business_path.length) definition.business_path = undefined;
    definition.requirement_ids = [...selectedRequirements];
    if (structuredSource.checked) {
      try { definition.source = JSON.parse(definition.source); }
      catch { toast('结构化设计来源不是有效 JSON，原内容未覆盖', { bad: true }); return; }
    }
    const result = await attempt(() => api(url(data.project.id, data.session, '/rows' + (row ? '/' + encodeURIComponent(row.id) : '')), {
      method: row ? 'PATCH' : 'POST', body: { row: definition, expected_revision: data.revision },
    }), save);
    if (result) { closeOverlay(); toast('设计已保存，原版本与执行证据保留'); refresh(); }
  });
  drawer({ title: row ? '编辑设计：' + row.id : '新建设计用例', wide: true,
    subtitle: '按需求写真人操作与真实【按钮】、业务预期和来源；业务层级逐行原样保存。修改预期后须录制独立检查或通过需求维护更新断言并真实回归。',
    body: form, foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), save] });
}

function importCases(data) {
  const existing = new Set(data.rows.map((row) => row.id));
  const items = data.available_cases.filter((item) => !existing.has(item.id));
  const selected = new Set();
  const save = h('button', { class: 'btn primary', disabled: true }, '导入为设计');
  const body = h('div', { class: 'case-pick' }, items.map((item) => h('label', { class: 'row' },
    h('input', { class: 'check', type: 'checkbox', 'aria-label': `导入 ${item.id}`, onChange: (event) => {
      if (event.target.checked) selected.add(item.id); else selected.delete(item.id); save.disabled = !selected.size;
    } }), h('span', {}, item.id + ' · ' + item.title))));
  save.addEventListener('click', async () => {
    const result = await attempt(() => api(url(data.project.id, data.session, '/import-cases'), { method: 'POST', body: { case_ids: [...selected], expected_revision: data.revision } }), save);
    if (result) { closeOverlay(); refresh(); }
  });
  drawer({ title: '从已有可执行用例导入设计', subtitle: '保留原用例；设计按当前版本关联，只有实际有效证据才能显示通过。',
    body: items.length ? body : h('p', {}, '已有用例都已导入这份设计表。'), foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), save] });
}

function link(data, row) {
  const choice = h('select', { class: 'select', id: 'cp-replay' }, data.available_cases.map((item) =>
    h('option', { value: item.id, selected: item.id === row.evidence.materialized_case_id }, item.id + ' · ' + item.title)));
  const save = h('button', { class: 'btn primary', disabled: !data.available_cases.length }, '关联可执行用例');
  save.addEventListener('click', async () => {
    const result = await attempt(() => api(url(data.project.id, data.session, `/rows/${encodeURIComponent(row.id)}/link`), {
      method: 'POST', body: { replay_case_id: choice.value, expected_revision: data.revision },
    }), save);
    if (result) { closeOverlay(); toast('已关联；回放只证明既有断言，新功能预期仍需落实到独立检查'); refresh(); }
  });
  drawer({ title: '关联设计与可执行用例', subtitle: '保留执行关联；仅重新关联不会证明旧断言覆盖新预期。按设计录制独立检查或通过需求维护更新断言后再回归。',
    body: field('可执行用例', choice), foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), save] });
}

async function run(data, identities) {
  const result = await attempt(() => api(url(data.project.id, data.session, '/run'), { method: 'POST',
    body: { case_ids: identities, engine: preferredEngine(), headed: preferredHeaded() } }));
  if (result) go('runs/' + result.id);
}

async function applyDesign(data, row) {
  const result = await attempt(() => api(url(data.project.id, data.session, `/rows/${encodeURIComponent(row.id)}/apply`), {
    method: 'POST', body: { expected_revision: data.revision },
  }));
  if (result) { toast(`功能设计已保存为用例版本 ${result.case_revision}；既有断言的覆盖范围待核对，尚未确认业务通过`); refresh(); }
}

function history(data) {
  const content = h('div', { class: 'stack' }, data.history.map((item) => {
    const view = h('button', { class: 'btn sm' }, `查看版本 ${item.version}`);
    view.addEventListener('click', async () => {
      const snapshot = await attempt(() => api(url(data.project.id, data.session, '/history/' + item.version)), view);
      if (snapshot) replace(content, h('p', {}, `历史版本 ${snapshot.version} · 保存于 ${fmtTime(snapshot.saved_at)}。这里保留当时的设计与证据，不代表当前设计验证。`),
        h('div', { style: 'overflow:auto' }, table(snapshot, { historical: true })));
    });
    return h('div', { class: 'row wrap' }, h('span', { class: 'grow' }, `版本 ${item.version} · ${item.rows} 条 · ${fmtTime(item.saved_at)}`), view,
      h('a', { class: 'btn sm', href: '/api' + url(data.project.id, data.session, `/history/${item.version}/excel`), download: '' }, '下载历史 Excel'));
  }));
  drawer({ title: '设计历史版本', subtitle: '修改前保存原设计与当时的真实证据；旧失败和旧预期不会被改写。', wide: true, body: content,
    foot: h('button', { class: 'btn', onClick: closeOverlay }, '关闭') });
}
