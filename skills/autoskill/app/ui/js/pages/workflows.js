// Requirement sources, traceable changes and durable workflow review.
import { h, icon, replace, api, scopeQuery, go, href, refresh, attempt, toast, fmtTime, live } from '../core.js';
import { drawer, closeOverlay, empty } from '../ui.js';

const PHASE = { sources: '来源整理', requirements: '需求拆分', design: '用例设计', coverage: '覆盖审查', completion: '补充设计', materialize: '候选用例', execute: '真实回归', diagnose: '失败分析', maintain: '维护候选', knowledge: '知识整理', investigate: '恢复调查' };
const STATE = { active: '进行中', cancelled: '已暂停', awaiting_host: '等待 AI 处理', waiting_execution: '正在回归', settled: '已完成', recovering: '等待恢复调查', ready: '待处理', leased: '处理中', completed: '已完成', obsolete: '已替代', publishing: '正在保存' };
const CONCLUSION = { satisfied: '业务满足', violated: '业务不满足', mixed: '存在业务失败', incomplete: '尚未完整验证', unassessed: '尚未验证' };
const PUBLICATION = { published: '候选已发布', bound_existing: '已验证已有用例', conflict: '发布冲突，原用例保留', unverified: '证据不足，候选未发布', rejected: '回归未满足，候选未发布', pending_validation: '等待全部回归完成', historical: '旧周期证据', interrupted: '回归中断' };
const VERDICT = { Pass: '通过', Fail: '失败', Blocked: '阻塞', Cancelled: '已取消', Unverified: '未验证' };
const key = () => crypto.randomUUID();
const text = (value) => JSON.stringify(value, null, 2);
const field = (title, input, hint) => h('div', { class: 'field' }, h('label', { htmlFor: input.id }, title), input, hint && h('p', { class: 'hint' }, hint));

export async function render(view, { id } = {}) {
  if (id) { await detail(view, id); return; }
  const data = await api('/workflows' + scopeQuery());
  if (data.items.some((item) => item.lifecycle === 'active' && !item.complete)) live.until = Date.now() + 8000;
  replace(view, h('div', { class: 'page-head' },
    h('div', { class: 'grow' }, h('h1', {}, '需求与工作流'), h('p', { class: 'sub' }, '从需求来源、用例设计到真实回归，查看关联、候选更新和历史结果。')),
    h('button', { class: 'btn primary', onClick: () => createWorkflow() }, icon('plus'), '新建工作流')),
  h('section', { class: 'card' }, data.items.length ? h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['工作流', '状态', '业务结论', '版本', ''].map((label) => h('th', {}, label)))),
    h('tbody', {}, data.items.map((item) => h('tr', { class: 'click', onClick: () => go('workflows/' + item.workflow_id) },
      h('td', {}, h('div', { class: 'title' }, item.title), h('div', { class: 'sub' }, item.project_id)),
      h('td', {}, STATE[item.cycle_state] || item.cycle_state), h('td', {}, CONCLUSION[item.conclusion] || item.conclusion),
      h('td', { class: 'num' }, item.revision), h('td', {}, h('a', { class: 'btn sm', href: href('workflows/' + item.workflow_id) }, '查看'))))))
    : empty({ glyph: 'link', title: '还没有需求工作流', text: '导入需求正文后，AI 可以持续完成拆分、设计、执行与维护。',
      actions: h('button', { class: 'btn primary', onClick: () => createWorkflow() }, '新建工作流') })));
}

async function createWorkflow() {
  const projects = await api('/projects');
  const project = h('select', { class: 'select', id: 'wf-project' }, projects.map((item) => h('option', { value: item.id }, item.name)));
  const title = h('input', { class: 'input', id: 'wf-title', placeholder: '例如：订单需求 V2', required: true });
  const source = h('textarea', { class: 'input', id: 'wf-source', rows: 10, placeholder: '粘贴需求原文，明确业务规则和预期。', required: true });
  const sourceId = h('input', { class: 'input', id: 'wf-source-id', value: 'source-1', required: true });
  const form = h('form', { class: 'stack' }, field('项目', project), field('工作流名称', title), field('来源编号', sourceId), field('需求正文', source));
  const save = h('button', { class: 'btn primary', disabled: !projects.length }, '创建工作流');
  save.addEventListener('click', async () => {
    if (!form.reportValidity()) return;
    const made = await attempt(() => api('/workflows', { method: 'POST', body: {
      project_id: project.value, idempotency_key: key(), spec: { title: title.value, sources: [{ id: sourceId.value, text: source.value }] },
    } }), save);
    if (made) { closeOverlay(); go('workflows/' + made.workflow_id); }
  });
  drawer({ title: '新建需求工作流', subtitle: '来源与预期会保留版本；实际结果由执行证据判定。',
    body: projects.length ? form : h('p', {}, '请先在「项目与用例」创建项目。'),
    foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), save] });
}

async function detail(view, identity) {
  const data = await api('/workflow-dashboard/' + encodeURIComponent(identity));
  const flow = data.workflow;
  if (flow.lifecycle === 'active' && !flow.complete) live.until = Date.now() + 8000;
  const control = (label, action) => {
    const button = h('button', { class: 'btn' }, label);
    button.addEventListener('click', async () => {
      const result = await attempt(() => api(`/workflows/${identity}/${action}`, { method: 'POST', body: { idempotency_key: key() } }), button);
      if (result) refresh();
    });
    return button;
  };
  const documents = data.documents;
  const title = h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, flow.title),
    h('p', { class: 'sub' }, `${STATE[flow.cycle_state] || flow.cycle_state} · ${CONCLUSION[flow.conclusion] || flow.conclusion} · 版本 ${flow.revision}`)),
  h('button', { class: 'btn', onClick: () => impactDrawer(data) }, icon('edit'), '需求迭代'),
  flow.lifecycle === 'active' ? control('暂停', 'cancel') : control('恢复', 'resume'),
  h('button', { class: 'btn primary', disabled: flow.lifecycle !== 'active', onClick: () => dispatchDrawer(identity) }, icon('spark'), '交给 AI 处理'));
  replace(view, h('a', { class: 'back', href: href('workflows') }, icon('right'), '需求与工作流'), title,
    documents['definition.json']?.extensions?.change_scope && maintenanceSummary(documents['definition.json'].extensions.change_scope),
    h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '覆盖与进度'),
      h('p', {}, `已设计覆盖 ${data.coverage.covered} / ${data.coverage.required} 条需求。`, data.coverage.missing_requirement_ids.length ? ` 待补充：${data.coverage.missing_requirement_ids.join('、')}` : ''),
      h('div', { class: 'row wrap' }, control('刷新执行与恢复状态', 'recover'),
        h('button', { class: 'btn', onClick: () => stageEditor(identity) }, '审阅并处理下一阶段')),
      h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['阶段', '状态', '执行与证据', '时间'].map((label) => h('th', {}, label)))),
        h('tbody', {}, data.items.map((item) => h('tr', {}, h('td', {}, PHASE[item.phase] || item.phase), h('td', {}, STATE[item.state] || item.state),
          h('td', {}, item.run_id ? h('a', { href: href('runs/' + item.run_id) }, '查看真实执行') : '',
            item.context.publication && h('p', { class: 'sub' }, PUBLICATION[item.context.publication] || item.context.publication),
            item.context.summary && executionSummary(item.context.summary)),
          h('td', { class: 'sub' }, fmtTime(item.finished_at || item.created_at)))))))),
    sourceCards(documents), traceTable(data.traceability), candidateCard(documents), workflowReport(flow),
    h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, 'AI 处理投递'),
      h('p', { class: 'sub' }, '等待投递、宿主接受和业务完成分别记录。宿主需要保持运行。'),
      data.deliveries.length ? data.deliveries.map((item) => h('p', {}, `${item.state} · ${fmtTime(item.updated_at)}${item.reason ? ' · ' + item.reason : ''}`)) : h('p', { class: 'sub' }, '尚未投递。'))));
}

function executionSummary(summary) {
  return h('details', { class: 'more' }, h('summary', {}, CONCLUSION[summary.conclusion] || summary.conclusion),
    h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['回归用例', '执行', '业务验证', '证据'].map((label) => h('th', {}, label)))),
      h('tbody', {}, (summary.results || []).map((item) => h('tr', {}, h('td', {}, item.case_id), h('td', {}, VERDICT[item.status] || '未执行'),
        h('td', {}, VERDICT[item.business_status] || item.business_status), h('td', {}, item.evidence_valid ? '已核验' : (item.reason || '待补充')))))));
}

function workflowReport(flow) {
  const receipt = flow.test_report;
  if (!receipt) return null;
  return h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '工作流报告'),
    receipt.status === 'ready' ? h('div', { class: 'row wrap' },
      [['download_url', 'Excel'], ['markdown_download_url', '测试简报'], ['bugs_download_url', '问题清单']].map(([name, label]) =>
        receipt[name] && h('a', { class: 'btn', href: receipt[name], download: '' }, label)))
      : h('p', { class: 'sub' }, receipt.reason || '报告尚未生成完成')));
}

function sourceCards(documents) {
  return h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '来源与设计'),
    [['sources', '需求来源'], ['requirements', '拆分需求'], ['scenarios', '测试场景'], ['oracles', '独立预期']].map(([name, label]) =>
      h('details', { class: 'more', open: name === 'sources' }, h('summary', {}, `${label}（${(documents[name + '.json'] || []).length}）`),
        (documents[name + '.json'] || []).map((item) => h('div', { class: 'stack', style: 'margin:12px 0' },
          h('b', {}, item.id + (item.title ? ' · ' + item.title : '')),
          item.text && h('pre', { class: 'code' }, item.text),
          item.source_ids && h('p', { class: 'sub' }, '来源：' + item.source_ids.join('、')),
          item.requirement_ids && h('p', { class: 'sub' }, '需求：' + item.requirement_ids.join('、')),
          item.checks && h('pre', { class: 'code' }, text(item.checks))))))));
}

function traceTable(rows) {
  return h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '需求与用例关联')),
    rows.length ? h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['已有用例', '来源 → 需求', '场景 → 预期', '依赖'].map((label) => h('th', {}, label)))),
      h('tbody', {}, rows.map((item) => h('tr', {}, h('td', {}, h('b', {}, item.title), h('p', { class: 'sub' }, item.case_id)),
        h('td', {}, item.mapped ? item.source_ids.join('、') + ' → ' + item.requirement_ids.join('、') : '缺少有效关联，待排查'),
        h('td', {}, item.scenario_ids.join('、') + ' → ' + item.oracle_ids.join('、')), h('td', {}, item.depends_on.join('、') || '无')))))
      : empty({ title: '项目还没有已有用例', text: '完成候选生成与执行后会显示关联。' }));
}

function candidateCard(documents) {
  const materialization = documents['materialization.json'];
  return h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '候选更新与回归范围'),
    materialization ? [h('p', {}, `回归目标：${materialization.case_ids.join('、')}`),
      h('p', { class: 'sub' }, '候选与实际发布状态分开；需求维护候选需要真实回归满足后才发布。'),
      (materialization.cases || []).map((item) => h('details', { class: 'more' }, h('summary', {}, item.id + ' · ' + item.title), h('pre', { class: 'code' }, text(item))))]
      : h('p', { class: 'sub' }, '尚未提交候选；设计与独立预期确定后再提交更新。')));
}

function maintenanceSummary(scope) {
  return h('div', { class: 'banner info' }, icon('link'), h('div', {}, h('b', {}, `需求维护：已固定 ${scope.case_ids.length} 条用例`),
    h('p', {}, scope.case_ids.join('、')), h('a', { href: href('workflows/' + scope.baseline_workflow_id) }, '查看原需求工作流与历史证据')));
}

function impactDrawer(data) {
  const sources = data.documents['sources.json'] || [];
  const source = h('select', { class: 'select', id: 'change-source' }, sources.map((item) => h('option', { value: item.id }, item.id)));
  const content = h('textarea', { class: 'input', id: 'change-text', rows: 8, value: sources[0]?.text || '', required: true });
  const result = h('div', { class: 'stack' });
  const picked = new Set();
  let analysis = null;
  let payload = null;
  const create = h('button', { class: 'btn primary', disabled: true }, '维护所选用例');
  const invalidate = () => { analysis = null; create.disabled = true; replace(result); };
  source.addEventListener('change', () => { content.value = sources.find((item) => item.id === source.value)?.text || ''; invalidate(); });
  content.addEventListener('input', invalidate);
  const analyze = h('button', { class: 'btn' }, '分析影响');
  analyze.addEventListener('click', async () => {
    if (!content.reportValidity()) return;
    payload = { sources: [{ id: source.value || 'new-source', text: content.value }] };
    const found = await attempt(() => api(`/workflow-dashboard/${data.workflow.workflow_id}/impact`, { method: 'POST', body: payload }), analyze);
    if (!found) return;
    analysis = found;
    picked.clear();
    for (const identity of [...found.direct_case_ids, ...found.dependency_case_ids]) picked.add(identity);
    const count = h('p', { class: 'sub' });
    const update = () => { create.disabled = !picked.size; count.textContent = `已选 ${picked.size} 条维护用例`; };
    replace(result, h('h3', {}, `直接影响 ${found.direct_case_ids.length} 条 · 依赖波及 ${found.dependency_case_ids.length} 条`),
      h('p', {}, `待排查 ${found.unmapped_case_ids.length} 条 · 未命中关联 ${found.unaffected_case_ids.length} 条`),
      h('p', { class: 'sub' }, found.limitation), count,
      h('div', { class: 'case-pick' }, found.cases.map((item) => {
        const box = h('input', { class: 'check', type: 'checkbox', checked: picked.has(item.case_id), 'aria-label': `维护 ${item.case_id}` });
        box.addEventListener('change', () => { if (box.checked) picked.add(item.case_id); else picked.delete(item.case_id); update(); });
        return h('label', { class: 'row' }, box, h('div', {}, h('b', {}, item.title),
          h('p', { class: 'sub' }, `${item.case_id} · ${item.reasons.join('；') || '未命中本次变更关联'}`)));
      })));
    update();
  });
  create.addEventListener('click', async () => {
    if (!analysis || !picked.size) return;
    const made = await attempt(() => api(`/workflow-dashboard/${data.workflow.workflow_id}/maintenance`, { method: 'POST', body: {
      ...payload, analysis_hash: analysis.analysis_hash, case_ids: [...picked], idempotency_key: key(),
    } }), create);
    if (made) { closeOverlay(); go('workflows/' + made.workflow_id); }
  });
  drawer({ title: '需求迭代与影响分析', subtitle: '先对照已保存关联找到影响集合，再选择需要维护的已有用例。', wide: true,
    body: [field('修改的需求来源', source), field('新需求正文', content), analyze, result],
    foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), create] });
}

function dispatchDrawer(identity) {
  const session = h('input', { class: 'input', id: 'wf-host-session', placeholder: '可选，留空交给宿主创建或选择会话' });
  const submit = h('button', { class: 'btn primary' }, '投递给 AI');
  submit.addEventListener('click', async () => {
    const result = await attempt(() => api(`/workflow-dashboard/${identity}/dispatch`, { method: 'POST', body: { host: 'dsh', host_session_id: session.value, idempotency_key: key() } }), submit);
    if (result) { closeOverlay(); toast('已进入 AI 处理队列，需要宿主在线接续'); refresh(); }
  });
  drawer({ title: '交给 AI 处理', subtitle: '由运行中的 DeepSeek Harness 宿主接续工作流；进入队列不代表业务已完成。',
    body: field('宿主会话', session), foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), submit] });
}

async function stageEditor(identity) {
  const claimed = await attempt(() => api(`/workflows/${identity}/next`, { method: 'POST', body: { idempotency_key: key(), owner: 'dashboard-review', lease_seconds: 1800 } }));
  if (!claimed?.task) { toast('当前没有可领取阶段；执行中的任务需要先完成或刷新状态'); refresh(); return; }
  const task = claimed.task;
  const content = h('textarea', { class: 'input mono', rows: 18, id: 'wf-proposal', value: text(task.payload_schema.example) });
  const submit = h('button', { class: 'btn primary' }, '提交审阅结果');
  let submitted = false;
  submit.addEventListener('click', async () => {
    const result = await attempt(() => api(`/workflows/${identity}/submit`, { method: 'POST', body: {
      item_id: task.item_id, attempt_id: task.attempt_id, lease_token: task.lease_token, idempotency_key: key(), payload: JSON.parse(content.value),
    } }), submit);
    if (result) { submitted = true; closeOverlay(); refresh(); }
  });
  drawer({ title: '审阅：' + (PHASE[task.phase] || task.phase), wide: true,
    subtitle: task.payload_schema.rule, body: field('阶段结果（JSON）', content, '按独立需求审阅来源、设计和候选；提交后由内核验证关联与执行证据。'),
    foot: [h('button', { class: 'btn', onClick: closeOverlay }, '暂不处理'), submit],
    onClose: () => {
      if (!submitted) api(`/workflows/${identity}/release`, { method: 'POST', body: {
        item_id: task.item_id, attempt_id: task.attempt_id, lease_token: task.lease_token, idempotency_key: key(),
      } }).catch((error) => toast(error.message, { bad: true }));
    } });
}
