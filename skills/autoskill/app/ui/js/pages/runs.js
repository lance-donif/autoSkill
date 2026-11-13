// Run history and the run report: what ran, what each case checked, and why something failed.
import { h, icon, replace, api, scopeQuery, go, href, route, statusPill, attempt, toast,
  fmtTime, fmtDuration, CATEGORY, ENGINE, TRIGGER, refresh, live } from '../core.js';
import { empty, errorState, startRun, confirm, preferredHeaded } from '../ui.js';
import { splitBar } from '../charts.js';
import { refreshShell } from '../shell.js';

const PAGE = 50;
const kindTag = (value) => h('span', { class: 'tag' }, icon(value === 'ui' ? 'globe' : 'api'), value === 'ui' ? '页面' : '接口');
const active = (run) => ['Queued', 'Running'].includes(run.status);
const selected = new Map();
const pendingDeletions = new Map();
let selectionScope = '';
let deletionErrors = [];
let deletionTimer = null;
let checkingDeletions = false;
const deleting = (run) => !!run.delete_requested && !run.deletion_error;

// biome-ignore lint/suspicious/useAwait: Preserve the asynchronous render contract and its existing promise adoption.
export async function render(view, { id }) {
  if (id) return report(view, id);
  return history(view);
}

// ---- lists: runs, or individual case results (the drill-down behind the dashboard figures) ----
async function history(view) {
  const { params } = route();
  const mode = params.get('view') === 'results' ? 'results' : 'runs';
  const status = params.get('status') || '';
  const offset = Number(params.get('offset') || 0);
  let data;
  try {
    data = await api((mode === 'results' ? '/results' : '/runs') + scopeQuery({ status, limit: PAGE, offset }));
  } catch (error) {
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  if (offset > 0 && offset >= data.total) {
    go('runs', { ...Object.fromEntries(params), offset: Math.max(0, Math.floor((data.total - 1) / PAGE) * PAGE) || null });
    return;
  }
  updateSelection(data.items, params, mode);
  const tabs = h('div', { class: 'seg', role: 'group', 'aria-label': '查看方式' },
    h('button', { 'aria-pressed': String(mode === 'runs'), onClick: () => go('runs', { view: null, status: null, offset: null }) }, '按运行'),
    h('button', { 'aria-pressed': String(mode === 'results'), onClick: () => go('runs', { view: 'results', status: null, offset: null }) }, '按用例结果'));
  const options = mode === 'results'
    ? [['', '全部结果'], ['Pass', '通过'], ['Fail', '失败'], ['Blocked', '阻塞']]
    : [['', '全部状态'], ['Pass', '通过'], ['Fail', '失败'], ['Blocked', '阻塞'], ['Running', '执行中'], ['Cancelled', '已取消']];
  const filter = h('div', { class: 'seg', role: 'group', 'aria-label': '结果筛选' }, options.map(([value, label]) => h('button', {
    'aria-pressed': String(status === value), onClick: () => go('runs', { view: mode === 'results' ? 'results' : null, status: value || null, offset: null }),
  }, label)));

  let body;
  let selectionBar;
  if (!data.items.length) {
    body = empty({ glyph: 'runs', title: status ? '没有符合条件的记录' : '这个范围内还没有执行',
      text: status ? '换一个结果筛选，或调整上方的项目与时间范围。' : '到「项目与用例」运行一次，结果会出现在这里。',
      actions: h('a', { class: 'btn primary', href: href('cases') }, icon('play'), '去运行用例') });
  } else if (mode === 'runs') {
    const list = runList(data.items, params);
    body = list.table;
    selectionBar = list.bar;
  } else {
    body = h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['结果', '用例', '类型', '项目', '原因', '时间', '耗时'].map((t) => h('th', {}, t)))),
      h('tbody', {}, data.items.map((item) => h('tr', { class: 'click', onClick: () => go('runs/' + item.run_id, { ...Object.fromEntries(params), case: item.case_id }) },
        h('td', {}, statusPill(item.status)),
        h('td', {}, h('div', { class: 'title' }, item.case_title), h('div', { class: 'sub mono' }, item.case_id)),
        h('td', {}, kindTag(item.kind)),
        h('td', { class: 'sub' }, item.project_name),
        h('td', { class: 'sub', style: 'max-width:340px' }, item.status === 'Pass' ? '—'
          : h('div', { class: 'ellipsis', title: item.reason }, h('b', {}, CATEGORY[item.category] || '未分类'), item.reason ? '：' + item.reason : '')),
        h('td', { class: 'sub num' }, fmtTime(item.started_at)),
        h('td', { class: 'sub num' }, fmtDuration(item.duration_ms))))));
  }
  const pager = data.total > PAGE && h('div', { class: 'card-body row', style: 'justify-content:flex-end' },
    h('span', { class: 'sub num' }, `${offset + 1}–${Math.min(offset + PAGE, data.total)} / ${data.total}`),
    h('button', { class: 'btn sm', disabled: offset === 0, onClick: () => go('runs', { ...Object.fromEntries(params), offset: Math.max(0, offset - PAGE) || null }) }, '上一页'),
    h('button', { class: 'btn sm', disabled: offset + PAGE >= data.total, onClick: () => go('runs', { ...Object.fromEntries(params), offset: offset + PAGE }) }, '下一页'));

  replace(view,
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '执行记录'),
      h('p', { class: 'sub' }, mode === 'results' ? `范围内共 ${data.total} 条用例结果（不含自动执行的前置）` : `范围内共 ${data.total} 次运行`))),
    h('div', { class: 'toolbar' }, tabs, filter),
    deletionNotice(), h('section', { class: 'card' }, body, pager), selectionBar);
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

function historyRow(run, params, repaint) {
  const box = h('input', { class: 'check', type: 'checkbox', checked: selected.has(run.id), disabled: deleting(run),
    'aria-label': `选择运行 ${run.id}`, onClick: (event) => event.stopPropagation(), onChange: () => {
      if (box.checked) selected.set(run.id, run); else selected.delete(run.id);
      repaint();
    } });
  const note = run.deletion_error || (deleting(run) ? '正在取消并删除，等待执行器退出及清理完成' :
    active(run) ? (run.current || '准备中') : (run.reason || `${run.total} 条用例`));
  return h('tr', { class: 'click' + (selected.has(run.id) ? ' selected' : ''), onClick: () => go('runs/' + run.id, Object.fromEntries(params)) },
    h('td', {}, box), h('td', {}, statusPill(run.status, deleting(run) ? '正在取消并删除' : null)),
    h('td', {}, h('div', { class: 'title' }, run.project_name), h('div', { class: 'sub ellipsis', style: 'max-width:360px', title: note }, note)),
    h('td', { style: 'min-width:170px' }, active(run)
      ? h('div', { class: 'stack', style: 'gap:4px' }, h('div', { class: 'progress' }, h('i', { style: `width:${run.total ? (run.done / run.total) * 100 : 0}%` })), h('span', { class: 'sub num' }, `${run.done}/${run.total}`))
      : h('div', { class: 'stack', style: 'gap:4px' }, splitBar(run), h('span', { class: 'sub num' }, `${run.passed} 通过 · ${run.failed} 失败 · ${run.blocked} 阻塞`))),
    h('td', { class: 'sub' }, TRIGGER[run.trigger] || run.trigger), h('td', { class: 'sub' }, ENGINE[run.engine]),
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
    go('runs', { ...Object.fromEntries(current.params), case: null });
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
const openRows = new Set();
let reportFor = null;

async function report(view, runId) {
  let run;
  try { run = await api('/runs/' + runId); } catch (error) {
    if (pendingDeletions.has(runId)) {
      replace(view, deletionNotice());
      return;
    }
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  watchDeletion(run);
  const focus = route().params.get('case');
  prepareReportRows(run, runId, focus);
  const running = active(run);
  if (running) live.until = Date.now() + 8000;
  const counted = run.results.filter((row) => !row.prerequisite);
  const spent = run.results.reduce((sum, row) => sum + row.duration_ms, 0);
  const notPassed = counted.filter((row) => row.status !== 'Pass').length;

  const again = h('button', { class: 'btn' }, icon('reset'), '重新运行');
  again.addEventListener('click', async () => {
    const made = await attempt(() => api(`/runs/${runId}/rerun`, { method: 'POST', body: { headed: preferredHeaded() } }), again);
    if (made) go('runs/' + made.id, { case: null });
  });
  const retry = h('button', { class: 'btn primary' }, icon('play'), `只重跑未通过的 ${notPassed} 条`);
  retry.addEventListener('click', async () => {
    const made = await attempt(() => api(`/runs/${runId}/rerun`, { method: 'POST', body: { failed_only: true, headed: preferredHeaded() } }), retry);
    if (made) go('runs/' + made.id, { case: null });
  });
  const stop = h('button', { class: 'btn danger' }, icon('stop'), '取消运行');
  stop.addEventListener('click', async () => {
    if (!(await confirm({ title: '取消这次运行？', text: '正在执行的那条用例会跑完，之后的不再执行。已完成的结果会保留。', action: '取消运行', danger: true }))) return;
    await attempt(() => api(`/runs/${runId}/cancel`, { method: 'POST', body: {} }), stop);
    toast('已请求取消');
    refresh();
  });

  const summary = runSummary(run, running, stop, again, notPassed, retry, spent, runDeleteButton([run]));

  const rows = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('h2', {}, '用例结果'),
      h('p', { class: 'sub' }, run.results.some((row) => row.prerequisite) ? '带「前置」标记的用例是为了满足依赖自动执行的，不计入统计。' : '点开一条查看每一步和每条断言。'))),
    run.results.length
      ? h('div', { class: 'result-list' }, run.results.map((row) => resultRow(run, row)))
      : empty({ glyph: 'clock', title: running ? '正在准备' : '没有结果', text: running ? '浏览器启动后，结果会一条条出现在这里。' : (run.reason || '这次运行没有产生结果。') }));

  replace(view,
    h('a', { class: 'back', href: href('runs', { ...Object.fromEntries(route().params), case: null }) }, icon('right'), '执行记录'),
    deletionNotice(),
    run.deletion_error && h('div', { class: 'banner bad', role: 'alert' }, icon('warn'), `删除未完成：${run.deletion_error}。可以重试删除。`),
    summary, rows);
  if (focus) {
    const target = view.querySelector(`[data-case="${CSS.escape(focus)}"]`);
    if (target && !view.dataset.scrolled) { target.scrollIntoView({ block: 'center' }); view.dataset.scrolled = runId; }
  }
}

function prepareReportRows(run, runId, focus) {
  if (reportFor !== runId) {
    reportFor = runId;
    openRows.clear();
    if (focus) openRows.add(focus);
    else for (const row of run.results) if (!row.prerequisite && row.status !== 'Pass') openRows.add(row.case_id);  // failures open by default
  }
}

function testReportControl(run) {
  const receipt = run.test_report;
  if (receipt?.status === 'ready') {
    return h('a', { class: 'btn', href: receipt.download_url, download: '', title: '本次任务的用例、步骤、断言与实际结果' }, icon('table'), '下载本次用例 Excel');
  }
  if (receipt?.status !== 'failed') return null;
  const retry = h('button', { class: 'btn', onClick: async () => {
    const result = await attempt(() => api('/task-reports/' + encodeURIComponent(receipt.report_id) + '/retry', { method: 'POST' }), retry);
    if (result) {
      toast(result.status === 'ready' ? '测试用例 Excel 已生成' : result.error || result.reason || '报告尚未生成', { bad: result.status === 'failed' });
      refresh();
    }
  } }, icon('reset'), '重试导出 Excel');
  return [h('span', { class: 'sub', title: receipt.error }, 'Excel 导出未完成'), retry];
}

function runSummary(run, running, stop, again, notPassed, retry, spent, remove) {
  return h('section', { class: 'card run-summary' },
    h('div', { class: 'card-body' },
      h('div', { class: 'row wrap', style: 'gap:16px' },
        statusPill(run.status, deleting(run) ? '正在取消并删除' : null),
        h('div', { class: 'grow' }, h('h1', {}, run.project_name),
          h('p', { class: 'sub' }, `${TRIGGER[run.trigger] || run.trigger}触发 · 页面用例用 ${ENGINE[run.engine]} 回放 · ${fmtTime(run.created_at)}`)),
        !deleting(run) && (running ? stop : [testReportControl(run), again, notPassed > 0 && retry]), remove),
      running
        ? h('div', { class: 'stack', style: 'gap:8px;margin-top:16px' },
          h('div', { class: 'progress' }, h('i', { style: `width:${run.total ? (run.done / run.total) * 100 : 0}%` })),
          h('div', { class: 'row sub' }, h('i', { class: 'spin' }), run.status === 'Queued' ? '排队中，等待前一个运行结束' : `正在执行：${run.current || '准备中'}`,
            h('span', { class: 'grow' }), h('span', { class: 'num' }, `${run.done} / ${run.total}`)))
        : h('div', { class: 'stats' },
          stat('通过', run.passed, 'pass'), stat('失败', run.failed, 'fail'), stat('阻塞', run.blocked, 'blocked'),
          stat('用例', run.total), stat('总耗时', fmtDuration(spent))),
      run.reason && h('div', { class: 'banner bad', style: 'margin-top:16px' }, icon('warn'), run.reason)));
}

function stat(label, value, tone) {
  return h('div', { class: 'stat' + (tone ? ' ' + tone : '') }, h('span', { class: 'stat-value' }, String(value)), h('span', { class: 'stat-label' }, label));
}

function resultRow(run, row) {
  const open = openRows.has(row.case_id);
  const head = h('button', { class: 'result-head', 'aria-expanded': String(open), onClick: () => {
    if (open) openRows.delete(row.case_id); else openRows.add(row.case_id);
    refresh();
  } },
  icon('right', 'icon-svg caret'), statusPill(row.status),
  h('span', { class: 'grow' }, h('span', { class: 'title' }, row.case_title),
    h('span', { class: 'sub' }, `　${row.case_id}`),
    row.prerequisite ? h('span', { class: 'tag accent', style: 'margin-left:8px' }, '前置') : null,
    row.detail.reused ? h('span', { class: 'tag', style: 'margin-left:6px' }, '复用登录态') : null,
    row.detail.relogin ? h('span', { class: 'tag', style: 'margin-left:6px' }, '已自动重新登录') : null,
    row.detail.fallbacks ? h('span', { class: 'tag', style: 'margin-left:6px', title: '首选定位没有匹配，用了备选定位' }, '定位已降级') : null),
  kindTag(row.kind), h('span', { class: 'sub num', style: 'min-width:72px;text-align:right' }, fmtDuration(row.duration_ms)));
  return h('div', { class: 'result' + (open ? ' open' : ''), dataset: { case: row.case_id } }, head, open && resultBody(run, row));
}

function resultBody(run, row) {
  const d = row.detail;
  const parts = [];
  if (row.status !== 'Pass') {
    parts.push(h('div', { class: 'banner ' + (row.status === 'Fail' ? 'bad' : '') }, icon(row.status === 'Fail' ? 'x' : 'warn'),
      h('span', {}, h('b', {}, CATEGORY[row.category] || (row.status === 'Fail' ? '失败' : '阻塞')), row.reason ? '：' + row.reason : '')));
  } else if (row.actual) {
    parts.push(h('p', { class: 'sub' }, row.actual));
  }
  if (d.request) {
    const req = d.request, res = d.response;
    parts.push(h('div', { class: 'req-line' }, h('span', { class: 'method ' + req.method.toLowerCase() }, req.method),
      h('span', { class: 'mono grow ellipsis', title: req.url }, req.url),
      res && h('span', { class: 'tag' + (res.status < 400 ? ' accent' : '') }, 'HTTP ' + res.status), res && h('span', { class: 'sub num' }, `${fmtDuration(row.duration_ms)} · ${res.bytes} B`)));
  }
  appendAssertionResults(parts, d);
  if (d.steps?.length) {
    parts.push(h('ol', { class: 'steps timeline' }, d.steps.map((step) => h('li', { class: step.status },
      h('span', { class: 'step-text' }, step.check && h('span', { class: 'tag accent' }, '断言'), step.text,
        step.fallback ? h('span', { class: 'tag' }, '备选定位') : null),
      h('span', { class: 'step-sub num' }, step.error || fmtDuration(step.ms))))));
  }
  appendRequestDetails(parts, d, row);
  appendScreenshots(parts, d, run, row);
  if (row.status !== 'Pass' && !row.prerequisite) {
    const again = h('button', { class: 'btn sm' }, icon('play'), '只重跑这一条');
    again.addEventListener('click', () => startRun([{ project_id: run.project_id, case_ids: [row.case_id] }], again));
    parts.push(h('div', { class: 'row' }, again));
  }
  return h('div', { class: 'result-body' }, parts);
}

function appendAssertionResults(parts, d) {
  if (d.assertions?.length) {
    parts.push(h('table', { class: 'table checks' },
      h('thead', {}, h('tr', {}, ['', '断言', '期望', '实际'].map((t) => h('th', {}, t)))),
      h('tbody', {}, d.assertions.map((check) => h('tr', { class: check.passed ? '' : 'bad' },
        h('td', { style: 'width:28px' }, h('span', { class: 'mark ' + (check.passed ? 'ok' : 'no') }, icon(check.passed ? 'check' : 'x'))),
        h('td', {}, check.name, check.fallback ? h('span', { class: 'tag', style: 'margin-left:6px' }, '备选定位') : null),
        h('td', { class: 'mono' }, show(check.expected)), h('td', { class: 'mono' }, show(check.actual)))))));
  }
}

function appendRequestDetails(parts, d, row) {
  if (d.request) {
    const sections = [['请求头', d.request.headers], ['查询参数', d.request.params], ['请求体', d.request.json]]
      .filter(([, value]) => value && (typeof value !== 'object' || Object.keys(value).length));
    if (sections.length || d.response) {
      parts.push(h('details', { class: 'more', open: row.status === 'Fail' }, h('summary', {}, '请求与响应详情'),
        h('div', { class: 'io' },
          h('div', {}, sections.map(([label, value]) => [h('div', { class: 'sub io-label' }, label), h('pre', { class: 'code' }, JSON.stringify(value, null, 2))])),
          d.response && h('div', {}, h('div', { class: 'sub io-label' }, '响应体'), h('pre', { class: 'code' }, d.response.body || '（空）')))));
    }
  }
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
  return text.length > 160 ? text.slice(0, 160) + '…' : text;
}
