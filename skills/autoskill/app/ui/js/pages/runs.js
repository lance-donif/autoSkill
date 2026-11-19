// Run history and the run report: what ran, what each case checked, and why something failed.
import { h, icon, replace, api, scopeQuery, go, href, route, attempt, toast,
  fmtTime, fmtDuration, CATEGORY, ENGINE, TRIGGER, refresh, live } from '../core.js';
import { empty, errorState, confirm } from '../ui.js';
import { splitBar } from '../charts.js';
import { refreshShell } from '../shell.js';
import { downloadRunHtml } from './run-export.js';
import { primaryState, stateResults, statePill, stateLabel } from '../status-view.js';
import { updateLiveView } from '../live-refresh.js';
import { rerunCaseButton, runActionFeedback, recoveryActions, returnToRuns } from './run-actions.js';
import { reportControls, reportStats, reportVerdict } from './report-controls.js';

const PAGE = 50;
const HISTORY_STATUSES = { NotRun: 'not_run', Pass: 'pass', Fail: 'fail' };
const isHistoryStatus = (value) => Object.hasOwn(HISTORY_STATUSES, value);
const kindTag = (value) => value && h('span', { class: 'tag' }, icon(value === 'ui' ? 'globe' : 'api'), value === 'native' ? '原生' : value === 'ui' ? '页面' : '接口');
const active = (run) => ['Queued', 'Running'].includes(run.status);
const selected = new Map();
const pendingDeletions = new Map();
let selectionScope = '';
let deletionErrors = [];
let deletionTimer = null;
let checkingDeletions = false;
const deleting = (run) => !!run.delete_requested && !run.deletion_error;
const changeHistory = (patch) => go('runs', { ...Object.fromEntries(route().params), ...patch, offset: null });
const stateAxes = { phase: '执行阶段', outcome: '综合结果', business: '业务验证', performance: '性能目标',
  quality: '质量', evidence: '证据', report: '报告交付', delivery: '投递' };
const historyPositions = new Map();
let focusedResult = '';

function historyLink(label, destination, identity) {
  return h('a', { class: 'title card-link', href: destination, dataset: { historyId: identity }, onClick: (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    historyPositions.set(location.hash, { y: window.scrollY, identity });
  } }, label);
}

function restoreHistoryPosition() {
  const saved = historyPositions.get(location.hash);
  if (!saved) return;
  historyPositions.delete(location.hash);
  requestAnimationFrame(() => {
    document.querySelector(`[data-history-id="${CSS.escape(saved.identity)}"]`)?.focus({ preventScroll: true });
    window.scrollTo(0, saved.y);
  });
}

// biome-ignore lint/suspicious/useAwait: Preserve the asynchronous render contract and its existing promise adoption.
export async function render(view, { id, isCurrent }) {
  if (id) return report(view, id, isCurrent);
  return history(view, isCurrent);
}

/** Keep the history controls mounted when only their result list changes. */
export function commit(view, next, { id }) {
  const current = view.querySelector('[data-history-region="content"]');
  const incoming = next.querySelector('[data-history-region="content"]');
  if (id || !current || !incoming) { updateLiveView(view, next); return; }
  const filters = view.querySelector('[data-history-region="filters"]');
  const removingFilter = filters.contains(document.activeElement);
  const oldToolbar = view.querySelector('[data-history-region="toolbar"]');
  const newToolbar = next.querySelector('[data-history-region="toolbar"]');
  if (oldToolbar.dataset.historyMode === newToolbar.dataset.historyMode) {
    for (const button of oldToolbar.querySelectorAll('[data-history-status]')) {
      button.setAttribute('aria-pressed', String(button.dataset.historyStatus === newToolbar.dataset.selectedStatus));
    }
    oldToolbar.dataset.selectedStatus = newToolbar.dataset.selectedStatus;
  } else {
    const focused = oldToolbar.contains(document.activeElement) ? document.activeElement : null;
    oldToolbar.replaceWith(newToolbar);
    if (focused?.dataset.historyStatus) newToolbar.querySelector(`[data-history-status="${focused.dataset.historyStatus}"]`).focus({ preventScroll: true });
    else if (focused) newToolbar.querySelector('[data-history-view][aria-pressed="true"]').focus({ preventScroll: true });
  }
  view.querySelector('.page-head p').replaceChildren(...next.querySelector('.page-head p').childNodes);
  for (const name of ['filters', 'notices', 'content', 'selection']) {
    updateLiveView(view.querySelector(`[data-history-region="${name}"]`), next.querySelector(`[data-history-region="${name}"]`));
  }
  if (removingFilter && !filters.contains(document.activeElement)) {
    (filters.querySelector('button') || view.querySelector('[data-history-status]')).focus({ preventScroll: true });
  }
}

// ---- lists: runs, or individual case results (the drill-down behind the dashboard figures) ----
async function history(view, isCurrent) {
  const { params } = route();
  const mode = params.get('view') === 'results' ? 'results' : 'runs';
  const bookmarked = params.get('status');
  const status = bookmarked === 'NotPassed' ? 'Fail' : bookmarked || '';
  const stateFilters = Object.fromEntries(Object.keys(stateAxes).filter((key) => params.get(key)).map((key) => [key, params.get(key)]));
  const offset = Number(params.get('offset') || 0);
  let data;
  try {
    data = await api((mode === 'results' ? '/results' : '/runs') + scopeQuery({ status, cycle: params.get('cycle'), ...stateFilters, limit: PAGE, offset }));
  } catch (error) {
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  if (!isCurrent()) return;
  if (offset > 0 && offset >= data.total) {
    go('runs', { ...Object.fromEntries(params), offset: Math.max(0, Math.floor((data.total - 1) / PAGE) * PAGE) || null });
    return;
  }
  updateSelection(data.items, params, mode);
  const chips = appliedFilters(stateFilters);
  if (status && !isHistoryStatus(status)) {
    const label = '状态：' + stateLabel(status.toLowerCase());
    chips.push(h('button', { class: 'btn sm', 'aria-label': '清除' + label, onClick: () => changeHistory({ status: null }) }, label, icon('x')));
  }
  let body;
  let selectionBar;
  if (!data.items.length) {
    body = emptyHistory(status || chips.length);
  } else if (mode === 'runs') {
    const list = runList(data.items, params);
    body = list.table;
    selectionBar = list.bar;
  } else {
    body = resultTable(data.items, params);
  }
  const pager = data.total > PAGE && h('div', { class: 'card-body row', style: 'justify-content:flex-end' },
    h('span', { class: 'sub num' }, `${offset + 1}–${Math.min(offset + PAGE, data.total)} / ${data.total}`),
    h('button', { class: 'btn sm', disabled: offset === 0, onClick: () => go('runs', { ...Object.fromEntries(params), offset: Math.max(0, offset - PAGE) || null }) }, '上一页'),
    h('button', { class: 'btn sm', disabled: offset + PAGE >= data.total, onClick: () => go('runs', { ...Object.fromEntries(params), offset: offset + PAGE }) }, '下一页'));

  replace(view,
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '执行记录'),
      h('p', { class: 'sub', 'aria-live': 'polite' }, '共 ', h('span', { dataset: { historyCount: '' } }, data.total),
        mode === 'results' ? ' 条用例结果（不含自动执行的前置）' : ' 次运行'))),
    historyToolbar(mode, status),
    h('div', { dataset: { historyRegion: 'filters' } }, chips.length > 0 && h('div', { class: 'toolbar', role: 'group', 'aria-label': '已应用的筛选' }, chips)),
    h('div', { dataset: { historyRegion: 'notices' } }, deletionNotice()),
    h('section', { class: 'card', dataset: { historyRegion: 'content' } },
      h('div', { class: 'table-scroll', role: 'region', tabindex: '0', 'aria-label': '执行记录表格' }, body), pager),
    h('div', { dataset: { historyRegion: 'selection' } }, selectionBar));
  restoreHistoryPosition();
}

function emptyHistory(filtered) {
  return empty({ glyph: 'runs', title: filtered ? '没有符合条件的记录' : '暂无执行记录',
      text: filtered ? '调整筛选条件、项目或时间范围后再试。' : '首次测试请在 DSH 对话中让 autoSkill 生成用例 Excel 并执行；已有用例可在「测试用例」重新运行，也可调整上方范围查找历史记录。',
      actions: h('a', { class: 'btn', href: href('cases') }, '查看测试用例') });
}

function historyVerdict(item) {
  const value = HISTORY_STATUSES[item.history_status] || item.state?.outcome || 'not_run';
  const pill = statePill(value);
  if (item.state?.scope?.historical) pill.title = '历史执行 · ' + pill.title;
  return pill;
}

function resultDestination(item, params) {
  const page = item.kind === 'native' ? 'native-runs/' : 'runs/';
  // A planned target can acquire a result ID between the list and report reads.
  const focus = String(item.id).startsWith('planned:') ? item.case_id : 'result:' + item.id;
  return href(page + item.run_id, { ...Object.fromEntries(params), case: focus, return: location.hash });
}

function resultTable(items, params) {
  return h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['结果', '用例', '类型', '项目', '原因', '时间', '耗时'].map((t) => h('th', {}, t)))),
      h('tbody', {}, items.map((item) => h('tr', { dataset: { historyVerdict: item.history_status || '', historyRawStatus: item.status } },
        h('td', {}, historyVerdict(item)),
        h('td', {}, historyLink(item.case_title, resultDestination(item, params), 'result:' + item.id), h('div', { class: 'sub mono' }, item.case_id)),
        h('td', {}, kindTag(item.kind)),
        h('td', { class: 'sub' }, item.project_name),
        h('td', { class: 'sub', style: 'max-width:340px' }, item.state?.outcome === 'pass' || (!item.category && !item.reason) ? '—'
          : h('div', { class: 'ellipsis', title: item.reason }, h('b', {}, CATEGORY[item.category] || '未分类'), item.reason ? '：' + item.reason : '')),
        h('td', { class: 'sub num' }, fmtTime(item.started_at)),
        h('td', { class: 'sub num' }, fmtDuration(item.duration_ms))))));
}

function historyToolbar(mode, status) {
  const tabs = h('div', { class: 'seg', role: 'group', 'aria-label': '查看方式' },
    [['runs', '按运行'], ['results', '按用例结果']].map(([value, label]) => h('button', {
      dataset: { historyView: value }, 'aria-pressed': String(mode === value),
      onClick: () => changeHistory({ view: value === 'runs' ? null : value }),
    }, label)));
  const label = mode === 'runs' ? '运行状态' : '用例结果';
  const choices = h('div', { class: 'seg', role: 'group', 'aria-label': label }, Object.entries(HISTORY_STATUSES).map(([value, outcome]) => h('button', {
    dataset: { historyStatus: value }, 'aria-pressed': String(status === value), title: '再次点击可清除筛选',
    onClick: (event) => changeHistory({ status: event.currentTarget.getAttribute('aria-pressed') === 'true' ? null : value }),
  }, stateLabel(outcome))));
  return h('div', { class: 'toolbar', dataset: { historyRegion: 'toolbar', historyMode: mode, selectedStatus: status } },
    tabs, h('div', { class: 'filter-field' }, h('span', { class: 'sub' }, label), choices));
}

function appliedFilters(stateFilters) {
  return Object.entries(stateFilters).map(([key, value]) => {
    const label = stateAxes[key] + '：' + stateLabel(value);
    return h('button', { class: 'btn sm', 'aria-label': '清除' + label, onClick: () => changeHistory({ [key]: null }) }, label, icon('x'));
  });
}

function updateSelection(items, params, mode) {
  const currentScope = params.toString();
  if (selectionScope !== currentScope || mode !== 'runs') selected.clear();
  selectionScope = currentScope;
  const available = new Map(items.filter((run) => !deleting(run)).map((run) => [run.id, run]));
  for (const id of selected.keys()) {
    if (available.has(id)) selected.set(id, available.get(id));
    else selected.delete(id);
  }
  if (mode === 'runs') for (const run of items) watchDeletion(run);
}

function runList(items, params) {
  const table = h('div');
  const bar = h('div', { class: 'select-bar', hidden: true });
  const eligible = items.filter((run) => !deleting(run));
  function paint() {
    const all = eligible.length > 0 && eligible.every((run) => selected.has(run.id));
    const master = h('input', { class: 'check', type: 'checkbox', checked: all, disabled: !eligible.length,
      'aria-label': '选择本页全部运行', onChange: () => {
        for (const run of eligible) { if (master.checked) selected.set(run.id, run); else selected.delete(run.id); }
        paint();
      } });
    master.indeterminate = !all && eligible.some((run) => selected.has(run.id));
    replace(table, h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, h('th', { style: 'width:36px' }, master),
        ['状态', '项目', '结果', '触发', '引擎', '开始时间', '耗时'].map((text) => h('th', {}, text)), h('th', { class: 'actions' }, ''))),
      h('tbody', {}, items.map((run) => historyRow(run, params, paint)))));
    const chosen = [...selected.values()];
    bar.hidden = !chosen.length;
    replace(bar, h('b', { class: 'grow' }, `已选 ${chosen.length} 次运行`),
      h('button', { class: 'btn ghost', onClick: () => { selected.clear(); paint(); } }, '清除'),
      runDeleteButton(chosen, { bulk: true }));
  }
  paint();
  return { table, bar };
}

function runProgress(run) {
  if (active(run)) {
    const width = run.total ? (run.done / run.total) * 100 : 0;
    return h('div', { class: 'stack', style: 'gap:4px' }, h('div', { class: 'progress' }, h('i', { style: `width:${width}%` })),
      h('span', { class: 'sub num' }, `${run.done}/${run.total}`));
  }
  return h('div', { class: 'stack', style: 'gap:4px' }, splitBar(run),
    h('span', { class: 'sub num' }, `${run.not_run || 0} 未执行 · ${run.passed || 0} 通过 · ${run.failed || 0} 失败`));
}

function historyRow(run, params, repaint) {
  const box = h('input', { class: 'check', type: 'checkbox', checked: selected.has(run.id), disabled: deleting(run),
    'aria-label': `选择运行 ${run.id}`, onClick: (event) => event.stopPropagation(), onChange: () => {
      if (box.checked) selected.set(run.id, run); else selected.delete(run.id);
      repaint();
    } });
  const note = run.deletion_error || (deleting(run) ? '正在取消并删除，等待执行器退出及清理完成' :
    active(run) ? (run.current || '准备中') : (run.reason || `${run.total} 条用例`));
  const destination = ({ load: 'load-tests', native: 'native-runs' })[run.family] || 'runs';
  return h('tr', { class: selected.has(run.id) ? 'selected' : '', dataset: { historyVerdict: run.history_status || '', historyRawStatus: run.status } },
    h('td', {}, box), h('td', {}, historyVerdict(run), active(run) && primaryState(run)),
    h('td', {}, historyLink([run.project_name, run.family === 'load' && h('span', { class: 'tag' }, '压测')], href(destination + '/' + run.id, { ...Object.fromEntries(params), return: location.hash }), 'run:' + run.id), h('div', { class: 'sub ellipsis', style: 'max-width:360px', title: note }, note)),
    h('td', { style: 'min-width:170px' }, runProgress(run)),
    h('td', { class: 'sub' }, TRIGGER[run.trigger] || run.trigger), h('td', { class: 'sub' }, ENGINE[run.engine] || run.engine || '未记录'),
    h('td', { class: 'sub num' }, fmtTime(run.created_at)),
    h('td', { class: 'sub num' }, run.finished_at && run.started_at ? fmtDuration(new Date(run.finished_at) - new Date(run.started_at)) : '—'),
    h('td', { class: 'actions' }, runDeleteButton([run], { small: true })));
}

function runDeleteButton(runs, { bulk = false, small = false } = {}) {
  const first = runs[0];
  let label = bulk ? '删除所选' : '删除运行';
  if (runs.some(active)) label = bulk ? '取消并删除所选' : '取消并删除';
  if (!bulk && first?.deletion_error) label = '重试删除';
  if (!bulk && first && deleting(first)) label = '正在取消并删除';
  const button = h('button', { class: 'btn danger' + (small ? ' sm' : ''),
    disabled: !runs.length || (!bulk && deleting(first)), title: label,
    'aria-label': small ? `${label} ${first.id}` : label,
    onClick: (event) => { event.stopPropagation(); deleteRuns(runs, button, bulk); },
  }, icon('trash'), label);
  return button;
}

async function deleteRuns(runs, button, bulk) {
  const cancelling = runs.some(active);
  const action = cancelling ? '取消并删除' : '删除运行';
  if (!(await confirm({ title: bulk ? `删除所选的 ${runs.length} 次运行？` : '删除这次运行？',
    text: `将删除${bulk ? `所选的 ${runs.length} 次` : '这次'}运行记录、结果和附件。`
      + (cancelling ? '正在执行的运行会先取消，待执行器退出并完成清理后删除。' : ''), action, danger: true }))) return;
  const result = await attempt(() => bulk
    ? api('/runs/delete', { method: 'POST', body: { run_ids: runs.map((run) => run.id) } })
    : api('/runs/' + encodeURIComponent(runs[0].id), { method: 'DELETE' }), button);
  if (!result) return;
  selected.clear();
  deletionErrors = result.errors || [];
  const deleted = result.deleted || [];
  const pending = result.pending || [];
  for (const item of deleted) pendingDeletions.delete(item.run_id);
  for (const item of pending) pendingDeletions.set(item.run_id, { message: item.message || '等待执行器退出及清理完成' });
  const messages = [];
  if (deleted.length) messages.push(`已删除 ${deleted.length} 次运行`);
  if (pending.length) messages.push(`${pending.length} 次正在取消并删除`);
  if (deletionErrors.length) messages.push(`${deletionErrors.length} 次未删除`);
  toast(messages.join('，') || '没有运行被删除', { bad: deletionErrors.length > 0 });
  scheduleDeletionCheck();
  returnToHistory(deleted.map((item) => item.run_id));
  await refreshShell();
  refresh();
}

function returnToHistory(deletedIds) {
  const current = route();
  if (current.page === 'runs' && deletedIds.includes(current.id)) {
    location.hash = returnToRuns();
  }
}

function watchDeletion(run) {
  if (!deleting(run)) return;
  if (!pendingDeletions.has(run.id)) pendingDeletions.set(run.id, { message: '等待执行器退出及清理完成' });
  live.until = Date.now() + 8000;
  scheduleDeletionCheck();
}

function scheduleDeletionCheck() {
  if (pendingDeletions.size && !deletionTimer && !checkingDeletions) deletionTimer = setTimeout(checkDeletions, 1200);
}

async function deletionState(id) {
  try {
    // Only an explicit API 404 proves removal; connection failures remain pending.
    const response = await fetch('/api/runs/' + encodeURIComponent(id), { cache: 'no-store' });
    if (response.status === 404) return { id, deleted: true };
    if (response.ok) return { id, run: await response.json() };
  } catch { /* Keep the request pending until the service answers. */ }
  return { id, connectionError: '暂时无法确认删除结果，将自动重试连接。' };
}

async function checkDeletions() {
  deletionTimer = null;
  checkingDeletions = true;
  const results = await Promise.all([...pendingDeletions.keys()].map(deletionState));
  const deleted = [];
  let changed = false;
  for (const result of results) {
    if (!pendingDeletions.has(result.id)) continue;
    if (result.deleted) {
      deleted.push(result.id);
      pendingDeletions.delete(result.id);
      changed = true;
    } else if (result.run?.deletion_error) {
      deletionErrors = [...deletionErrors.filter((error) => error.run_id !== result.id), { run_id: result.id, message: result.run.deletion_error }];
      pendingDeletions.delete(result.id);
      changed = true;
    } else {
      const item = pendingDeletions.get(result.id);
      changed ||= item.connectionError !== result.connectionError;
      item.connectionError = result.connectionError;
    }
  }
  if (deleted.length) {
    toast(`已删除 ${deleted.length} 次运行`);
    returnToHistory(deleted);
  }
  if (changed) { await refreshShell(); refresh(); }
  checkingDeletions = false;
  scheduleDeletionCheck();
}

function deletionNotice() {
  return [pendingDeletions.size > 0 && h('div', { class: 'banner info', role: 'status' }, h('i', { class: 'spin' }),
    h('div', {}, h('b', {}, `${pendingDeletions.size} 次运行正在取消并删除`),
      [...pendingDeletions].map(([id, item]) => h('p', {}, `${id}：${item.connectionError || item.message}`)))),
  deletionErrors.length > 0 && h('div', { class: 'banner bad', role: 'alert' }, icon('warn'),
    h('div', { class: 'grow' }, h('b', {}, '部分运行未删除'),
      deletionErrors.map((error) => h('p', {}, `${error.run_id || ''}：${error.message}`))),
    h('button', { class: 'btn sm icon', 'aria-label': '关闭删除提示', onClick: () => { deletionErrors = []; refresh(); } }, icon('x')))];
}

// ---- one run ----
const rowVisibility = new Map();
let reportFor = null;

async function report(view, runId, isCurrent) {
  let run;
  try { run = await api('/runs/' + runId); } catch (error) {
    if (pendingDeletions.has(runId)) {
      replace(view, deletionNotice());
      return;
    }
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  if (!isCurrent()) return;
  if (run.family === 'load') { go('load-tests/' + runId, Object.fromEntries(route().params)); return; }
  if (run.family === 'native') { go('native-runs/' + runId, Object.fromEntries(route().params)); return; }
  watchDeletion(run);
  const focus = route().params.get('case');
  const reportKey = runId + '/' + focus;
  if (reportFor !== reportKey) {
    reportFor = reportKey;
    rowVisibility.clear();
  }
  const running = active(run);
  if (running) live.until = Date.now() + 8000;
  const rows = reportRows(run, false, focus);

  replace(view,
    h('a', { class: 'back', href: returnToRuns() }, icon('right'), '返回执行记录'),
    deletionNotice(),
    run.deletion_error && h('div', { class: 'banner bad', role: 'alert' }, icon('warn'), `删除未完成：${run.deletion_error}。可以重试删除。`),
    runSummary(run), runActionFeedback(run), focusNotice(run, focus), rows);
  if (focus && focusedResult !== runId + '/' + focus && (focus.startsWith('result:') || run.results.filter((row) => row.case_id === focus).length === 1)) {
    focusedResult = runId + '/' + focus;
    requestAnimationFrame(() => {
      const target = document.querySelector(`#view [data-${focus.startsWith('result:') ? 'result' : 'case'}="${CSS.escape(focus.replace(/^result:/, ''))}"]`);
      target?.querySelector('.result-head')?.focus({ preventScroll: true });
      target?.scrollIntoView({ block: 'center' });
    });
  }
}

function reportRows(run, offline = false, focus = null) {
  const running = active(run);
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('h2', {}, '用例结果'),
      run.results.some((row) => row.prerequisite) && h('p', { class: 'sub' }, '自动执行的前置用例不计入统计。'))),
    run.results.length
      ? h('div', { class: 'result-list' }, run.results.map((row) => resultRow(run, row, offline, focus)))
      : empty({ glyph: 'clock', title: running ? '正在准备' : '没有结果', text: running ? '浏览器启动后，结果会一条条出现在这里。' : (run.reason || '这次运行没有产生结果。') }));
}

const resultKey = (row) => String(row.id ?? row.case_id);

function focusNotice(run, focus) {
  if (!focus) return null;
  if (focus.startsWith('result:')) return run.results.some((row) => String(row.id) === focus.slice(7)) ? null
    : h('div', { class: 'banner', role: 'status' }, '本次运行没有该结果记录，请在下方选择实际结果。');
  const matches = run.results.filter((row) => row.case_id === focus);
  if (matches.length < 2) return null;
  return h('div', { class: 'banner', role: 'status' }, '此用例编号有多条执行结果，请选择要查看的记录：',
    matches.map((row) => h('a', { class: 'btn sm', href: href('runs/' + run.id, { ...Object.fromEntries(route().params), case: 'result:' + row.id }) },
      `结果 ${row.id} · ${stateLabel(row.state?.outcome)}`)));
}

function offlineReport(run) {
  return h('main', { class: 'view report-document', id: 'view' }, runSummary(run, true), reportRows(run, true));
}

function runSummary(run, offline = false) {
  const spent = run.results.reduce((sum, row) => sum + (row.duration_ms || 0), 0);
  return h('section', { class: 'card run-summary' },
    h('div', { class: 'card-body stack report-summary' },
      h('div', { class: 'row wrap', style: 'gap:16px' },
        h('div', { class: 'grow report-heading' }, h('h1', {}, run.project_name),
          h('p', { class: 'sub' }, `${fmtTime(run.created_at)} · ${run.total} 条用例`)), reportVerdict(run)),
      publicationSummary(run, offline),
      active(run) && h('div', { class: 'stack', style: 'gap:8px' },
          h('div', { class: 'progress' }, h('i', { style: `width:${run.total ? (run.done / run.total) * 100 : 0}%` })),
          h('div', { class: 'row sub' }, h('i', { class: 'spin' }), run.status === 'Queued' ? '排队中，等待前一个运行结束' : `正在执行：${run.current || '准备中'}`,
            h('span', { class: 'grow' }), h('span', { class: 'num' }, `${run.done} / ${run.total}`))),
      run.reason && h('div', { class: 'banner bad' }, icon('warn'), run.reason),
      reportStats(run, spent),
      !offline && reportControls(() => downloadRunHtml(run, offlineReport(run)))));
}

function publicationSummary(run, offline) {
  const receipt = run.publication;
  if (!receipt) return null;
  const published = receipt.published_ids || [];
  const unpublished = Object.entries(receipt.unpublished || {});
  return h('div', { class: 'stack', 'aria-label': '本批候选入库结果' },
    h('p', { class: 'sub' }, receipt.complete ? `本批候选已入库 ${published.length} 条` : '候选入库结果待确认',
      published.length > 0 && '：' + published.join('、'),
      unpublished.length > 0 && `；${unpublished.length} 条未入库`,
      !offline && published.length > 0 && [' · ', h('a', { href: href('cases', { projects: run.project_id }) }, '查看本项目测试用例')]),
    unpublished.length > 0 && h('details', { class: 'more' }, h('summary', {}, '查看未入库原因'),
      unpublished.map(([id, reason]) => h('p', { class: 'sub' }, `${id}：${reason}`))));
}

function resultRow(run, row, offline, focus) {
  const key = resultKey(row);
  const selected = focus === (focus?.startsWith('result:') ? 'result:' + key : row.case_id);
  const open = offline || (rowVisibility.get(key) ?? (focus ? selected : !row.prerequisite && row.state?.outcome !== 'pass'));
  const head = h('button', { class: 'result-head', 'aria-expanded': String(open), onClick: () => {
    rowVisibility.set(key, !open);
    refresh();
  } },
  icon('right', 'icon-svg caret'),
  h('span', { class: 'grow' }, h('span', { class: 'title' }, row.case_title),
    h('span', { class: 'sub' }, `　${row.case_id}`),
    row.prerequisite ? h('span', { class: 'tag accent', style: 'margin-left:8px' }, '前置') : null,
    row.detail.reused ? h('span', { class: 'tag', style: 'margin-left:6px' }, '复用登录态') : null,
    row.detail.relogin ? h('span', { class: 'tag', style: 'margin-left:6px' }, '已自动重新登录') : null,
    row.detail.fallbacks ? h('span', { class: 'tag', style: 'margin-left:6px', title: '首选定位没有匹配，用了备选定位' }, '定位已降级') : null,
    stateResults(row)),
  kindTag(row.kind), h('span', { class: 'sub num', style: 'min-width:72px;text-align:right' }, fmtDuration(row.duration_ms)));
  return h('div', { class: 'result' + (open ? ' open' : ''), dataset: { case: row.case_id, result: key } }, head, open && resultBody(run, row, offline));
}

function resultBody(run, row, offline = false) {
  const d = row.detail;
  const parts = [attemptHistory(d)];
  if (!offline) parts.push(recoveryActions(run, row));
  if (row.state?.outcome !== 'pass') {
    parts.push(h('div', { class: 'banner ' + (row.state?.outcome === 'fail' ? 'bad' : '') }, icon(row.state?.outcome === 'fail' ? 'x' : 'warn'),
      h('span', {}, h('b', {}, CATEGORY[row.category] || stateLabel(row.state?.outcome)), row.reason ? '：' + row.reason : '')));
  } else if (row.actual) {
    parts.push(h('p', { class: 'sub' }, row.actual));
  }
  const exchanges = httpExchanges(d, row.duration_ms);
  parts.push(exchanges.map(({ label, line }) => line.length > 0 && h('div', { class: 'req-line' },
    label && h('b', {}, label), line)));
  appendAssertionResults(parts, d);
  parts.push(stepTimelines(d), httpDetails(exchanges, row));
  appendScreenshots(parts, d, run, row);
  if (!offline) parts.push(caseRetryControl(run, row));
  return h('div', { class: 'result-body' }, parts);
}

function caseRetryControl(run, row) {
  if (run.family === 'native' || row.state?.outcome === 'pass' || row.prerequisite) return null;
  if (run.workflow_id) return h('a', { class: 'btn sm', href: href('workflows/' + run.workflow_id) }, '返回原测试任务');
  if (run.publication?.candidate_id) return null;
  return h('div', { class: 'row' }, rerunCaseButton(run, row.case_id));
}

function attemptHistory(detail) {
  const attempts = detail.attempts || [];
  if (!attempts.length) return null;
  return h('ol', { class: 'steps timeline', 'aria-label': '尝试历史' }, attempts.map((entry, index) =>
    h('li', {}, h('span', { class: 'step-text' }, `第 ${index + 1} 次`, entry.outcome && statePill(entry.outcome)))));
}

const stepStates = { done: '已完成', pass: '已完成', failed: '失败', fail: '失败', running: '执行中', not_run: '未执行' };

function stepTimeline(steps, definition = false) {
  if (!steps.length) return null;
  return h('ol', { class: 'steps timeline', 'aria-label': definition ? '用例定义步骤' : '操作步骤' }, steps.map((step) => {
    const state = definition ? null : step.status;
    const observed = !definition && state !== 'not_run';
    const elapsed = observed && step.ms != null && !(step.error && step.ms === 0) ? fmtDuration(step.ms) : '';
    const facts = [stepStates[state], elapsed, observed && step.error].filter(Boolean);
    return h('li', { class: state || '' },
      h('span', { class: 'step-text' }, step.check && h('span', { class: 'tag accent' }, '断言'), step.text,
        observed && step.fallback && h('span', { class: 'tag' }, '备选定位')),
      facts.length > 0 && h('span', { class: 'step-sub' }, facts.join(' · ')));
  }));
}

function stepTimelines(detail) {
  const steps = detail.steps || [];
  const definitions = [...steps.filter((step) => step.status === 'definition_only'), ...(detail.definition_steps || [])];
  const actual = steps.filter((step) => step.status !== 'definition_only');
  const pending = (detail.pending_steps || []).map((step) => ({ ...step, status: 'not_run' }));
  return [stepTimeline([...actual, ...pending]), definitions.length > 0 && h('section', { class: 'stack' },
    h('h3', {}, '用例定义步骤'), stepTimeline(definitions, true))];
}

function assertionMark(check) {
  if (typeof check.passed !== 'boolean') return check.status === 'not_run' ? '未执行' : '—';
  return h('span', { class: 'mark ' + (check.passed ? 'ok' : 'no'), 'aria-label': check.passed ? '通过' : '失败' },
    icon(check.passed ? 'check' : 'x'));
}

function appendAssertionResults(parts, d) {
  if (d.assertions?.length) {
    parts.push(h('table', { class: 'table checks' },
      h('thead', {}, h('tr', {}, ['', '断言', '期望', '实际'].map((t) => h('th', {}, t)))),
      h('tbody', {}, d.assertions.map((check) => h('tr', { class: check.passed === false ? 'bad' : '' },
        h('td', { style: 'width:28px' }, assertionMark(check)),
        h('td', {}, check.name, check.fallback && h('span', { class: 'tag' }, '备选定位'),
          check.elapsed_ms != null && h('div', { class: 'sub num' }, fmtDuration(check.elapsed_ms))),
        h('td', { class: 'mono' }, show(check.expected)),
        h('td', { class: 'mono' }, check.actual_excerpt && h('span', { class: 'tag' }, '摘录'), show(check.actual)))))));
  }
}

function httpFields(fields) {
  const items = fields.filter(([, value]) => value != null && value !== '');
  if (!items.length) return null;
  return h('div', {}, items.map(([label, value]) => [
    h('div', { class: 'sub io-label' }, label),
    h('pre', { class: 'code' }, typeof value === 'string' ? value : JSON.stringify(value, null, 2)),
  ]));
}

function httpExchange(entry) {
  const { request, response } = entry;
  const method = request?.method;
  const nonempty = (value) => value && Object.keys(value).length ? value : null;
  const line = [
    method && h('span', { class: 'method ' + method.toLowerCase() }, method),
    request?.url && h('span', { class: 'mono request-url' }, request.url),
    response?.status != null && h('span', { class: 'tag' + (response.status < 400 ? ' accent' : '') }, 'HTTP ' + response.status),
  ].filter(Boolean);
  const body = response?.body === '' || (response?.body == null && response?.bytes === 0) ? '（空）' : response?.body;
  const fields = [
    httpFields([['请求头', nonempty(request?.headers)], ['查询参数', nonempty(request?.params)],
      ['请求体', request?.json ?? request?.body], ['表单', request?.form]]),
    httpFields([['响应头', nonempty(response?.headers)], ['响应体', body]]),
  ].filter(Boolean);
  if (!fields.length && response?.status != null) fields.push(httpFields([['响应状态', 'HTTP ' + response.status]]));
  if (!line.length && !fields.length && response?.bytes == null) return null;
  if (entry.duration_ms != null) line.push(h('span', { class: 'sub num' }, fmtDuration(entry.duration_ms)));
  if (response?.bytes != null) line.push(h('span', { class: 'sub num' }, `${response.bytes} B`));
  return { line, fields };
}

function httpExchanges(detail, duration) {
  const attempts = (detail.attempts || []).map((entry, index) => ({ entry, number: index + 1 }))
    .filter(({ entry }) => entry.kind !== 'postcondition');
  const records = attempts.map(({ entry, number }) => ({ number,
    exchange: httpExchange({ ...entry, duration_ms: entry.duration_ms ?? (attempts.length === 1 ? duration : null) }) }));
  if (!records.at(-1)?.exchange) {
    records.push({ exchange: httpExchange({ ...detail, duration_ms: attempts.length <= 1 ? duration : null }) });
  }
  const captured = records.filter(({ exchange }) => exchange);
  return captured.map(({ number, exchange }) => ({ ...exchange,
    label: attempts.length > 1 ? number ? `第 ${number} 次` : '用例结果' : '' }));
}

function httpDetails(exchanges, row) {
  const records = exchanges.filter(({ fields }) => fields.length);
  if (!records.length) return null;
  return h('details', { class: 'more', open: row.state?.outcome === 'fail' || row.status === 'Fail' },
    h('summary', {}, '请求与响应详情'), records.map(({ label, fields }) => h('div', { class: 'stack' },
      label && h('b', {}, label), h('div', { class: 'io' }, fields))));
}

function appendScreenshots(parts, d, run, row) {
  for (const name of d.screenshots || []) {
    const url = `/api/runs/${run.id}/files/${encodeURIComponent(name)}`;
    parts.push(h('a', { class: 'shot', href: url, target: '_blank', rel: 'noreferrer', title: '在新标签页查看原图' },
      h('img', { src: url, alt: `${row.case_title} 结束时的页面截图`, loading: 'lazy' }),
      h('span', { class: 'sub' }, icon('image'), '结束时的页面截图（密码输入框已遮盖）')));
  }
}

function show(value) {
  if (value === true) return '是';
  if (value === false) return '否';
  if (value == null) return '—';
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return text.length > 160 ? h('details', { class: 'more' }, h('summary', {}, text.slice(0, 160) + '…'), h('pre', { class: 'code' }, text)) : text;
}
