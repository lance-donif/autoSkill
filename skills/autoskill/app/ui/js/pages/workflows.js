// Requirement sources, traceable changes and durable workflow review.
import { h, icon, replace, api, scopeQuery, go, href, refresh, attempt, toast, fmtTime, live, beforeRouteChange } from '../core.js';
import { empty } from '../ui.js';
import { primaryState, stateResults, stateDetails } from '../status-view.js';
import { updateLiveView } from '../live-refresh.js';

const PHASE = { sources: '来源整理', requirements: '需求拆分', design: '用例设计', coverage: '覆盖审查', completion: '补充设计', materialize: '候选用例', execute: '真实回归', diagnose: '失败分析', maintain: '维护候选', knowledge: '知识整理', investigate: '恢复调查' };
const STATE = { active: '进行中', cancelled: '已暂停', awaiting_host: '等待 AI 处理', waiting_execution: '正在回归', settled: '已完成', recovering: '等待恢复调查', ready: '待处理', leased: '处理中', completed: '已完成', obsolete: '已替代', publishing: '正在保存' };
const CONCLUSION = { satisfied: '业务满足', violated: '业务不满足', mixed: '存在业务失败', incomplete: '尚未完整验证', unassessed: '尚未验证' };
const PUBLICATION = { published: '通过验证的候选已入库', partially_published: '部分候选已入库', bound_existing: '已验证已有用例', conflict: '入库冲突，原用例保留', unverified: '证据不足，候选未入库', rejected: '回归未满足，候选未入库', pending_validation: '等待回归验证', historical: '旧周期证据', interrupted: '回归中断' };
const VERDICT = { Pass: '通过', Fail: '失败', Blocked: '阻塞', Cancelled: '已取消', Unverified: '未验证' };
const key = () => crypto.randomUUID();
const text = (value) => JSON.stringify(value, null, 2);
const readingPositions = new Map();
const workflowContexts = new WeakMap();
const controls = 'a,button,summary';
const readingKey = (node) => node.tagName + '/' + (node.closest('[data-reading-id]')?.dataset.readingId
  || node.getAttribute('href') || node.id || node.textContent);

export function commit(view, next) {
  const current = workflowContexts.get(view.querySelector('.page-head'));
  const incoming = workflowContexts.get(next.querySelector('.page-head'));
  if (!current || !incoming || current.key === incoming.key) { updateLiveView(view, next); return; }
  view.replaceChildren(...next.childNodes);
  const title = view.querySelector('h1');
  title.tabIndex = -1;
  title.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

beforeRouteChange(() => {
  const heading = document.querySelector('#view .page-head');
  const context = workflowContexts.get(heading);
  if (!context) return;
  const view = heading.closest('#view');
  const focused = document.activeElement;
  const focusKey = view.contains(focused) ? readingKey(focused) : null;
  readingPositions.set(context.address, { key: context.key, y: window.scrollY,
    expanded: [...view.querySelectorAll('details[data-reading-id]')].map((node) => [node.dataset.readingId, node.open]),
    focusKey, focusIndex: [...view.querySelectorAll(controls)].filter((node) => readingKey(node) === focusKey).indexOf(focused) });
});

function preserveReadingPosition(heading, identity, cycle) {
  const address = location.hash;
  const key = JSON.stringify([identity, cycle]);
  workflowContexts.set(heading, { address, key });
  requestAnimationFrame(() => {
    if (!heading.isConnected || location.hash !== address) return;
    const saved = readingPositions.get(address);
    readingPositions.delete(address);
    if (!saved || saved.key !== key) return;
    const view = heading.closest('#view');
    const expanded = new Map(saved.expanded);
    for (const details of view.querySelectorAll('details[data-reading-id]')) {
      if (expanded.has(details.dataset.readingId)) details.open = expanded.get(details.dataset.readingId);
    }
    [...view.querySelectorAll(controls)].filter((node) => readingKey(node) === saved.focusKey)[saved.focusIndex]?.focus({ preventScroll: true });
    window.scrollTo(0, saved.y);
  });
}

export async function render(view, { id } = {}) {
  if (id) { await detail(view, id); return; }
  const data = await api('/workflows' + scopeQuery());
  if (data.items.some((item) => item.lifecycle === 'active' && !item.complete)) live.until = Date.now() + 8000;
  replace(view, h('div', { class: 'page-head' },
    h('div', { class: 'grow' }, h('h1', {}, '需求与工作流'), h('p', { class: 'sub' }, '在 DSH 对话中交给 autoSkill 开始或调整测试，在这里查看设计、执行进度与历史结果。'))),
  h('section', { class: 'card' }, data.items.length ? h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['工作流', '执行阶段', '综合与分项结果', '版本', ''].map((label) => h('th', {}, label)))),
    h('tbody', {}, data.items.map((item) => h('tr', { class: 'click', onClick: () => go('workflows/' + item.workflow_id) },
      h('td', {}, h('div', { class: 'title' }, item.title), h('div', { class: 'sub' }, item.project_id)),
      h('td', {}, primaryState(item)), h('td', {}, stateResults(item)),
      h('td', { class: 'num' }, item.revision), h('td', {}, h('a', { class: 'btn sm', href: href('workflows/' + item.workflow_id) }, '查看'))))))
    : empty({ glyph: 'link', title: '还没有需求工作流', text: '在 DSH 对话中让 autoSkill 按需求生成用例 Excel，再按用例执行；这里显示保存的进度与证据。' })));
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
  const title = h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', { tabIndex: -1 }, flow.title),
    h('p', { class: 'sub row wrap' }, primaryState(flow), `版本 ${flow.revision}`),
    h('a', { class: 'btn', href: href('case-plans', { project: flow.project_id, projects: flow.project_id }) }, icon('table'), '查看项目用例 Excel')),
  flow.lifecycle === 'active' ? control('暂停', 'cancel') : control('恢复', 'resume'));
  replace(view, h('a', { class: 'back', href: href('workflows') }, icon('right'), '需求与工作流'), title,
    h('section', { class: 'card card-body stack' }, h('h2', {}, '当前任务状态'), stateResults(flow),
      flow.state?.reason && h('p', { class: 'sub' }, flow.state.reason),
      h('details', { class: 'more', dataset: { readingId: 'state:detail' } }, h('summary', {}, '更多状态与运行说明'), stateDetails(flow),
        h('p', { class: 'sub' }, '继续或调整测试，请回到 DSH 对话交给 autoSkill。'))),
    documents['definition.json']?.extensions?.change_scope && maintenanceSummary(documents['definition.json'].extensions.change_scope),
    h('section', { class: 'card', 'aria-label': '当前周期进度', dataset: { cycle: flow.cycle_id } }, h('div', { class: 'card-body stack' }, h('h2', {}, '当前周期进度'),
      h('p', {}, `已设计覆盖 ${data.coverage.covered} / ${data.coverage.required} 条需求。`, data.coverage.missing_requirement_ids.length ? ` 待补充：${data.coverage.missing_requirement_ids.join('、')}` : ''),
      h('button', { class: 'btn', onClick: refresh }, icon('reset'), '刷新状态'),
      stageTable(data.items.filter((item) => item.cycle_id === flow.cycle_id), '当前周期阶段与执行'))),
    previousCycles(data.items, flow.cycle_id),
    sourceCards(documents), traceTable(data.traceability), candidateCard(documents), workflowReport(flow),
    h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, 'AI 处理投递'),
      h('p', { class: 'sub' }, '等待投递、宿主接受和业务完成分别记录。宿主需要保持运行。'),
      data.deliveries.length ? data.deliveries.map((item) => h('p', {}, `${item.state} · ${fmtTime(item.updated_at)}${item.reason ? ' · ' + item.reason : ''}`)) : h('p', { class: 'sub' }, '尚未投递。'))));
  preserveReadingPosition(title, identity, flow.cycle_id);
}

function stageTable(items, label) {
  return h('div', { class: 'table-scroll', role: 'region', tabIndex: 0, 'aria-label': label },
    h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['阶段', '状态', '执行与证据', '时间'].map((title) => h('th', {}, title)))),
      h('tbody', {}, items.map((item) => h('tr', {}, h('td', {}, PHASE[item.phase] || item.phase), h('td', {}, STATE[item.state] || item.state),
        h('td', {}, item.run_id ? h('a', { href: href('runs/' + item.run_id), dataset: { readingId: 'execution-link:' + item.id } }, '查看真实执行') : '',
          publicationSummary(item.context), item.context.summary && executionSummary(item.context.summary, item.id)),
        h('td', { class: 'sub' }, fmtTime(item.finished_at || item.created_at)))))));
}

function previousCycles(items, currentCycle) {
  const groups = new Map();
  for (const item of items) {
    if (item.cycle_id === currentCycle) continue;
    if (!groups.has(item.cycle_id)) groups.set(item.cycle_id, []);
    groups.get(item.cycle_id).push(item);
  }
  return [...groups].map(([cycle, rows], index) => {
    const failures = rows.filter((item) => item.phase === 'execute').flatMap((item) => item.context.summary?.results || [])
      .filter((result) => result.status === 'Fail').length;
    const label = `历史周期 ${index + 1}`;
    return h('details', { class: 'card workflow-cycle', dataset: { readingId: 'cycle:' + cycle, cycle } },
      h('summary', {}, `${label} · ${failures} 条失败结果 · ${fmtTime(rows[0].created_at)}`),
      h('div', { class: 'card-body stack' }, h('p', { class: 'sub' }, '周期：' + (cycle || '未记录')),
        stageTable(rows, label + '阶段与执行')));
  });
}

function publicationSummary(context) {
  const receipt = context.publication_receipt;
  if (!receipt) return context.publication && h('p', { class: 'sub' }, PUBLICATION[context.publication] || context.publication);
  const published = receipt.published_ids || [];
  const unpublished = Object.entries(receipt.unpublished || {});
  return h('div', { class: 'sub' },
    h('p', {}, `已入库 ${published.length} 条${unpublished.length ? `，未入库 ${unpublished.length} 条` : ''}`),
    published.length > 0 && h('p', {}, '已入库用例：' + published.join('、')),
    unpublished.map(([identity, reason]) => h('p', {}, `${identity} 未入库：${reason}`)),
    receipt.conflict && h('p', {}, '存在入库冲突，原用例保留。'));
}

function executionSummary(summary, identity) {
  return h('details', { class: 'more', dataset: { readingId: 'execution:' + identity } }, h('summary', {}, '历史回归结论：', CONCLUSION[summary.conclusion] || summary.conclusion),
    h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['回归用例', '历史执行结果', '业务验证', '证据'].map((label) => h('th', {}, label)))),
      h('tbody', {}, (summary.results || []).map((item) => h('tr', {}, h('td', {}, item.case_id), h('td', {}, VERDICT[item.status] || '未执行'),
        h('td', {}, VERDICT[item.business_status] || item.business_status), h('td', {}, item.evidence_valid ? '已核验' : (item.reason || '待补充')))))));
}

function workflowReport(flow) {
  const receipt = flow.test_report;
  if (!receipt) return null;
  return h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '工作流报告'),
    flow.state?.report === 'ready' ? h('div', { class: 'row wrap' },
      [['download_url', 'Excel'], ['markdown_download_url', '测试简报'], ['bugs_download_url', '问题清单']].map(([name, label]) =>
        receipt[name] && h('a', { class: 'btn', href: receipt[name], download: '' }, label)))
      : h('p', { class: 'sub' }, receipt.error || receipt.reason || '报告尚未生成完成'),
    flow.state?.report === 'failed' && retryWorkflowReport(receipt)));
}

function retryWorkflowReport(receipt) {
  const retry = h('button', { class: 'btn', onClick: async () => {
    const result = await attempt(() => api('/task-reports/' + encodeURIComponent(receipt.report_id) + '/retry', { method: 'POST' }), retry);
    if (result) { toast(result.status === 'ready' ? '报告已生成' : result.error || '报告尚未生成', { bad: result.status === 'failed' }); refresh(); }
  } }, icon('reset'), '重试导出报告');
  return retry;
}

function sourceCards(documents) {
  return h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '来源与设计'),
    [['sources', '需求来源'], ['requirements', '拆分需求'], ['scenarios', '测试场景'], ['oracles', '独立预期']].map(([name, label]) =>
      h('details', { class: 'more', open: name === 'sources', dataset: { readingId: 'source:' + name } }, h('summary', {}, `${label}（${(documents[name + '.json'] || []).length}）`),
        (documents[name + '.json'] || []).map((item) => h('div', { class: 'stack', style: 'margin:12px 0' },
          h('b', {}, item.id + (item.title ? ' · ' + item.title : '')),
          item.filename && h('p', { class: 'sub' }, '原始文件：' + item.filename),
          item.raw_ref && h('p', { class: 'sub mono', style: 'overflow-wrap:anywhere' }, '来源标识：' + item.raw_ref),
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
      : empty({ title: '项目还没有已有用例', text: '可复用用例保存后，这里显示其需求与执行关联。' }));
}

function candidateCard(documents) {
  const materialization = documents['materialization.json'];
  return h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '候选更新与回归范围'),
    materialization ? [h('p', {}, `回归目标：${materialization.case_ids.join('、')}`),
      h('p', { class: 'sub' }, '这里保留候选与历史证据；可复用用例以真实执行验证和实际发布记录为准。'),
      (materialization.cases || []).map((item) => h('details', { class: 'more', dataset: { readingId: 'candidate:' + item.id } }, h('summary', {}, item.id + ' · ' + item.title), h('pre', { class: 'code' }, text(item))))]
      : h('p', { class: 'sub' }, '尚无候选记录；请在 DSH 对话中继续当前测试。')));
}

function maintenanceSummary(scope) {
  return h('div', { class: 'banner info' }, icon('link'), h('div', {}, h('b', {}, `需求维护：已固定 ${scope.case_ids.length} 条用例`),
    h('p', {}, scope.case_ids.join('、')), h('a', { href: href('workflows/' + scope.baseline_workflow_id) }, '查看原需求工作流与历史证据')));
}
