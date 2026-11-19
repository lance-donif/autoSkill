// Inspect the saved case sheet, execution evidence and prior Excel versions.
import { h, icon, replace, api, scope, route, go, href, refresh, attempt, fmtTime, statusPill, beforeRouteChange } from '../core.js';
import { drawer, closeOverlay, empty } from '../ui.js';
import { updateLiveView } from '../live-refresh.js';
import { projectIndex, runSettings } from '../shell.js';

const url = (project, session, suffix = '') => `/case-plans/${encodeURIComponent(project)}${suffix}?session=${encodeURIComponent(session)}`;
const readingPositions = new Map();
const sheetContexts = new WeakMap();
let changedTask = null;

export function commit(view, next) {
  const current = sheetContexts.get(view.querySelector('.case-plan-groups'));
  const incoming = sheetContexts.get(next.querySelector('.case-plan-groups'));
  const options = view.querySelector('.case-plan-options');
  if (current && incoming && current.key === incoming.key && options?.querySelector('.menu')) {
    updateLiveView(view.querySelector('.page-head'), next.querySelector('.page-head'));
    updateLiveView(view.querySelector('.case-plan-groups'), next.querySelector('.case-plan-groups'));
    return;
  }
  if (!current || !incoming || current.key === incoming.key) { updateLiveView(view, next); return; }
  if (current.address === incoming.address && current.taskId && incoming.taskId && current.taskId !== incoming.taskId) {
    changedTask = incoming.taskId;
    next.querySelector('.page-head .grow').append(taskChangeNotice());
  }
  const selection = current.address !== incoming.address && options?.querySelector('select:focus');
  const nextOptions = next.querySelector('.case-plan-options');
  if (selection && nextOptions) nextOptions.open = true;
  view.replaceChildren(...next.childNodes);
  const title = view.querySelector('h1');
  const target = selection && nextOptions?.querySelector(`[id="${CSS.escape(selection.id)}"]`);
  (target || title).focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

beforeRouteChange(() => {
  const sheet = document.querySelector('#view .case-plan-groups');
  const context = sheetContexts.get(sheet);
  if (!context) return;
  const focused = document.activeElement;
  const row = sheet.contains(focused) && focused.closest('[data-case]');
  readingPositions.set(context.address, { key: context.key, y: window.scrollY,
    optionsOpen: document.querySelector('#view .case-plan-options')?.open,
    closedBranches: [...sheet.querySelectorAll('.case-plan-tree:not([open])')].map((node) => node.dataset.branch),
    expanded: [...sheet.querySelectorAll('.case-plan-details[open]')].map((node) => node.closest('[data-case]').dataset.case),
    focus: row ? { case: row.dataset.case, identity: focused.dataset.readingId, tag: focused.tagName, href: focused.getAttribute('href') } : null });
});

function preserveReadingPosition(view, data) {
  const sheet = view.querySelector('.case-plan-groups');
  if (!sheet) return;
  const address = location.hash;
  // Results may change while away; only the original design controls whether reading state can return.
  const fields = ['id', 'module', 'title', 'type', 'priority', 'description', 'preconditions', 'steps', 'expected', 'source', 'business_path'];
  const key = JSON.stringify([data.project.id, data.session, data.task_id, data.version,
    data.rows.map((row) => fields.map((field) => row[field]))]);
  sheetContexts.set(sheet, { address, key, taskId: data.task_id });
  requestAnimationFrame(() => {
    if (!sheet.isConnected || location.hash !== address) return;
    const saved = readingPositions.get(address);
    readingPositions.delete(address);
    if (!saved || saved.key !== key) return;
    const options = sheet.closest('#view').querySelector('.case-plan-options');
    if (options) options.open = !!saved.optionsOpen;
    for (const branch of sheet.querySelectorAll('.case-plan-tree')) branch.open = !saved.closedBranches.includes(branch.dataset.branch);
    for (const details of sheet.querySelectorAll('.case-plan-details')) {
      details.open = saved.expanded.includes(details.closest('[data-case]').dataset.case);
    }
    if (saved.focus) {
      const row = sheet.querySelector(`[data-case="${CSS.escape(saved.focus.case)}"]`);
      const target = saved.focus.identity ? row?.querySelector(`[data-reading-id="${CSS.escape(saved.focus.identity)}"]`)
        : [...(row?.querySelectorAll(saved.focus.tag) || [])].find((node) => node.getAttribute('href') === saved.focus.href);
      target?.focus({ preventScroll: true });
    }
    window.scrollTo(0, saved.y);
  });
}

export async function render(view) {
  const projects = await projectIndex();
  const scoped = scope().projects;
  const params = route().params;
  const projectId = projects.find((item) => item.id === params.get('project'))?.id
    || projects.find((item) => scoped.includes(item.id))?.id || projects[0]?.id;
  const session = params.get('session') || '';
  const project = h('select', { class: 'select', id: 'cp-project', 'aria-label': '用例表项目' },
    projects.map((item) => h('option', { value: item.id, selected: item.id === projectId }, item.name)));
  project.addEventListener('change', () => go('case-plans', { project: project.value, projects: project.value }));
  const sheet = h('span', { class: 'sub' }, '当前用例表：' + (session || '默认'));
  const selectors = h('div', { class: 'toolbar wrap case-plan-selectors' }, h('label', { htmlFor: 'cp-project' }, '项目'), project,
    sheet);
  if (!projectId) {
    replace(view, heading(), h('section', { class: 'card' }, empty({ title: '还没有用例 Excel',
      text: '在 DSH 对话中让 autoSkill 先生成用例 Excel，再按用例执行并更新结果。保存后可在这里查看。' })));
    return;
  }
  const data = await api(url(projectId, session));
  const activeSession = data.session ?? session;
  sheet.textContent = '当前用例表：' + (activeSession || '默认');
  if (data.sessions?.length) {
    const choice = h('select', { class: 'select', id: 'cp-session', 'aria-label': '已有用例表',
      onChange: (event) => go('case-plans', { project: projectId, session: event.currentTarget.value }) },
    data.sessions.map((item) => h('option', { value: item.session, selected: item.session === activeSession },
      item.label || item.title || item.session || '默认用例表')));
    sheet.replaceWith(choice);
  }
  replace(view, heading(data),
    h('div', { class: 'toolbar wrap case-plan-tools' },
      data.exists && h('a', { class: 'btn primary', href: '/api' + url(projectId, activeSession, '/excel'), download: '' }, icon('table'), '下载 14 列 Excel'),
      h('details', { class: 'more case-plan-options' }, h('summary', {}, '用例表选项'),
        h('div', { class: 'stack case-plan-options-panel' }, selectors,
          h('div', { class: 'row wrap' },
            h('button', { class: 'btn', disabled: !data.history.length && !data.task_id, onClick: () => history(data) }, icon('clock'), '历史版本'),
            h('button', { class: 'btn', onClick: refresh }, icon('reset'), '刷新结果'), runSettings()),
          h('p', { class: 'sub' }, `版本 ${data.version} · 当前用例表：${activeSession || '默认'}`),
          h('p', { class: 'sub' }, '在 DSH 对话中生成、执行和调整用例。结果按真实证据回填；测试内容改变后需重新执行。')))),
    data.rows.length ? caseGroups(data)
      : h('section', { class: 'card' }, empty({ glyph: 'table', title: '这份用例表还没有设计用例', text: '请在原 DSH 对话中继续生成用例 Excel；已有链接会保留对应的用例表。' })));
  preserveReadingPosition(view, data);
}

function heading(data) {
  const retained = data?.rows.some((row) => row.status === 'Fail' && row.evidence?.has_historical_failure && row.evidence.latest_execution?.status === 'Pass');
  const other = [['Blocked', '阻塞'], ['Cancelled', '已取消'], ['Unverified', '业务未验证']]
    .filter(([status]) => data?.summary?.[status]).map(([status, label]) => `${data.summary[status]} ${label}`).join(' · ');
  return h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', { tabIndex: -1 }, '测试用例表'),
    h('p', { class: 'sub' }, data ? `${data.project.name} · ${data.rows.length} 条用例 · ${data.summary?.Pass || 0} 通过 · ${data.summary?.Fail || 0} ${retained ? '曾失败（含后续复测）' : '失败'} · ${data.summary?.NotRun || 0} 未执行${other ? ' · ' + other : ''}`
      : '先查看用例 Excel，再核对执行结果和问题证据。'), data?.task_id && changedTask === data.task_id && taskChangeNotice()));
}

function taskChangeNotice() {
  return h('p', { class: 'sub', role: 'status' }, '已切换到新的用例表任务，以下显示本次设计与结果。');
}

function caseGroups(data) {
  const groups = new Map();
  for (const row of data.rows) {
    const path = Array.isArray(row.business_path) ? row.business_path.filter(Boolean) : [];
    if (path.length > 1 && path[0] === data.project.name) path.shift();
    if (!path.length) path.push(row.module || '未分组');
    let level = groups;
    let branch;
    for (const name of path) {
      if (!level.has(name)) level.set(name, { name, rows: [], children: new Map(), total: 0 });
      branch = level.get(name);
      branch.total += 1;
      level = branch.children;
    }
    branch.rows.push(row);
  }
  return h('div', { class: 'stack case-plan-groups' }, [...groups.values()].map((branch) => caseBranch(data, branch, [])));
}

function caseBranch(data, branch, ancestors) {
  const root = ancestors.length === 0;
  const path = [...ancestors, branch.name];
  const address = path.join(' / ');
  return h('section', { class: root ? 'card' : 'case-plan-branch', 'aria-label': (root ? '业务模块：' : '业务目录：') + address },
    h('details', { class: 'case-plan-tree', open: true, 'aria-label': '目录 ' + address, dataset: { branch: JSON.stringify(path) } },
      h('summary', { class: 'card-head case-plan-folder' }, icon('right'), h(root ? 'h2' : 'h3', { class: 'grow' }, branch.name),
        h('span', { class: 'sub' }, `${branch.total} 条用例`)),
      branch.rows.map((row) => caseRow(data, row)),
      branch.children.size > 0 && h('div', { class: 'case-plan-branches' },
        [...branch.children.values()].map((child) => caseBranch(data, child, path)))));
}

function sourceText(source) {
  if (!source) return '尚未记录来源';
  if (typeof source === 'string') return source;
  if (Array.isArray(source)) return source.map(sourceText).join('\n');
  return [source.text, source.url, source.file].filter(Boolean).join('\n') || JSON.stringify(source, null, 2);
}

function caseRow(data, row) {
  const evidence = row.evidence || {};
  const latest = evidence.latest_execution;
  const executions = evidence.execution_history || [];
  const retested = latest && (executions.length > 1 || (evidence.has_historical_failure && latest.status === 'Pass'));
  const problem = row.status === 'Pass' ? '未发现问题' : evidence.reason || row.actual || row.conclusion || '尚未执行';
  const facts = [['preconditions', '前置条件'], ['steps', '测试步骤'], ['expected', '预期结果'], ['actual', '实际结果'], ['remarks', '备注']];
  const authors = [['author', '设计人'], ['executor', '执行人'], ['environment', '执行环境']]
    .filter(([key]) => row[key]).map(([key, label]) => `${label}：${row[key]}`).join(' · ');
  return h('article', { class: 'case-plan-row', dataset: { case: row.id } },
    caseOverview(data, row, evidence, problem, retested),
    h('details', { class: 'more case-plan-details' }, h('summary', {}, '查看步骤、预期与实际'),
      h('div', { class: 'case-plan-facts' }, facts.filter(([key]) => (key !== 'actual' || !retested) && (row[key] || ['steps', 'expected'].includes(key))).map(([key, label]) =>
        h('div', {}, h('h3', { class: 'sub' }, label), h('p', { class: 'case-plan-text' }, row[key] || '尚未记录'))),
      row.source && h('div', {}, h('h3', { class: 'sub' }, '需求来源'), h('p', { class: 'case-plan-text' }, sourceText(row.source)))),
      retested && h('section', { class: 'stack', 'aria-label': '实际结果与复测记录' }, h('h3', {}, '实际结果与复测记录'),
        executions.map((entry, index) => h('div', {},
          h('p', { class: 'row wrap' }, `第 ${index + 1} 次执行`, statusPill(entry.status), executionLink(entry, '查看本次执行', 'history')),
          h('p', { class: 'sub case-plan-text' }, entry.actual || entry.reason || '详见本次执行证据'),
          entry.reason && entry.actual && entry.reason !== entry.actual && h('p', { class: 'sub case-plan-text' }, entry.reason)))),
      h('p', { class: 'sub case-plan-text' }, '问题说明：' + problem),
      h('p', { class: 'sub' }, '业务路径：' + ((row.business_path || []).join(' / ') || row.module || '未分组')),
      (row.requirement_ids || []).map((identity) => {
        const requirement = data.requirements?.find((item) => item.id === identity);
        return h('p', { class: 'sub' }, '关联需求：', requirement?.workflow_id
          ? h('a', { href: href('workflows/' + requirement.workflow_id) }, requirement.requirement_id) : identity);
      }),
      authors && h('p', { class: 'sub' }, authors),
      evidence.materialized_case_id && h('p', { class: 'sub' }, '执行用例编号：' + evidence.materialized_case_id),
      (row.screenshots || []).map((source) => h('a', { class: 'card-link', href: source, target: '_blank', rel: 'noreferrer' }, '查看截图'))));
}

function caseOverview(data, row, evidence, problem, retested) {
  const latest = evidence.latest_execution;
  const stale = latest && row.status === 'NotRun' && evidence.current === false;
  const originalFailure = retested && evidence.execution_history.find((entry) => entry.status === 'Fail');
  const latestLabel = stale ? '查看历史执行' : retested ? '查看最新复测' : '查看真实执行证据';
  return h('div', { class: 'case-plan-overview' },
    h('div', {}, h('h3', {}, row.description || row.title || row.id), h('p', { class: 'sub mono' }, row.id)),
    caseOutcome(data, row, evidence, problem, retested, stale),
    h('div', { class: 'stack' }, originalFailure && executionLink(originalFailure, '查看原失败', 'original'),
      latest ? executionLink(latest, latestLabel)
        : evidence.run_id ? executionLink(evidence, '查看真实执行证据') : h('span', { class: 'sub' }, '尚无执行证据')));
}

function caseOutcome(data, row, evidence, problem, retested, stale) {
  const latest = evidence.latest_execution;
  const conclusion = stale ? '当前定义尚未验证，需重新执行'
    : retested && evidence.has_historical_failure ? '本任务曾失败' : row.conclusion;
  return h('div', {}, statusPill(row.status, conclusion),
    (stale || !retested) && h('p', { class: 'sub case-plan-problem' }, problem),
    (stale || retested) && h('p', { class: 'sub' }, stale ? '历史执行：' : '最新复测：', statusPill(latest.status)),
    latest && publicationResult(latest, data.project.id, stale));
}

function executionLink(entry, label, identity = 'latest') {
  return entry.run_id && h('a', { class: 'card-link', dataset: { readingId: `${identity}:${entry.run_id}:${entry.result_id ?? ''}` }, href: href('runs/' + entry.run_id,
    entry.result_id == null ? {} : { case: 'result:' + entry.result_id }) }, label);
}

function publicationResult(entry, projectId, historical = false) {
  const receipt = entry.publication;
  if (!receipt) return null;
  if (receipt.published_ids?.includes(entry.case_id)) return h('p', { class: 'sub' }, historical ? '该历史版本曾入库 · ' : '本次版本已入库 · ',
    h('a', { href: href('cases', { projects: projectId }) }, '查看可复用用例'));
  if (!receipt.complete) return h('p', { class: 'sub' }, '入库结果待确认');
  const reason = receipt.unpublished?.[entry.case_id];
  return reason && h('p', { class: 'sub case-plan-problem' }, '未入库：' + reason);
}

function history(data) {
  const versions = h('section', { class: 'stack', 'aria-label': '设计历史版本' }, h('h3', {}, '设计历史版本'),
    !data.history.length && h('p', { class: 'sub' }, '尚无设计修改前的版本。'), data.history.map((item) => {
    const view = h('button', { class: 'btn sm' }, `查看版本 ${item.version}`);
    view.addEventListener('click', async () => {
      const snapshot = await attempt(() => api(url(data.project.id, data.session, '/history/' + item.version)), view);
      if (snapshot) replace(versions, h('p', {}, `历史版本 ${snapshot.version} · 保存于 ${fmtTime(snapshot.saved_at)}。这里保留当时的设计与证据，不代表当前设计验证。`),
        caseGroups(snapshot));
    });
    return h('div', { class: 'row wrap' }, h('span', { class: 'grow' }, `版本 ${item.version} · ${item.rows} 条 · ${fmtTime(item.saved_at)}`), view,
      h('a', { class: 'btn sm', href: '/api' + url(data.project.id, data.session, `/history/${item.version}/excel`), download: '' }, '下载历史 Excel'));
  }));
  const deliveries = h('section', { class: 'stack', 'aria-label': '以前任务交付' });
  const load = async () => {
    replace(deliveries, h('h3', {}, '以前任务交付'), h('p', { class: 'sub', role: 'status' }, '正在读取原任务报告…'));
    try {
      const result = await api(url(data.project.id, data.session, '/deliveries'));
      replace(deliveries, h('h3', {}, '以前任务交付'),
        !result.deliveries.length && h('p', { class: 'sub' }, '本会话尚无以前任务的报告。'),
        result.deliveries.map(previousDelivery));
    } catch (error) {
      replace(deliveries, h('h3', {}, '以前任务交付'), h('p', { class: 'error', role: 'status' }, '读取失败：' + error.message),
        h('button', { class: 'btn sm', onClick: load }, '重新读取'));
    }
  };
  const content = h('div', { class: 'stack' }, deliveries, versions);
  drawer({ title: '设计历史版本', subtitle: '修改前保存原设计与当时的真实证据；旧失败和旧预期不会被改写。', wide: true, body: content,
    foot: h('button', { class: 'btn', onClick: closeOverlay }, '关闭') });
  load();
}

function previousDelivery(item) {
  const report = item.report;
  const labels = [['Pass', '通过'], ['Fail', '失败'], ['Blocked', '阻塞'], ['Cancelled', '已取消'], ['Unverified', '业务未验证'], ['NotRun', '未执行'], ['Unknown', '未知']];
  const counts = labels.filter(([key]) => report.summary_counts?.[key]).map(([key, label]) => `${report.summary_counts[key]} ${label}`).join(' · ');
  const links = [['download_url', '下载原任务 Excel'], ['markdown_download_url', '下载原任务简报'], ['bugs_download_url', '下载原任务 Bug 清单']];
  return h('article', { class: 'card stack', dataset: { task: item.task_id } },
    item.title && h('h3', {}, item.title),
    h('p', { class: 'sub case-plan-text' }, item.task_id),
    report.generated_at && h('p', { class: 'sub' }, '报告生成于 ' + fmtTime(report.generated_at)),
    counts && h('p', {}, counts),
    report.status === 'ready' ? h('div', { class: 'row wrap' }, links.filter(([key]) => report[key]).map(([key, label]) =>
      h('a', { class: 'btn sm', href: report[key], download: '' }, label)))
      : h('p', { class: 'error', role: 'status' }, report.error || '原任务报告尚不可下载；文件状态不代表业务通过。'));
}
