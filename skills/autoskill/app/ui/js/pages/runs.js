// Run history and the run report: what ran, what each case checked, and why something failed.
import { h, icon, replace, api, scopeQuery, go, href, route, statusPill, attempt, toast,
  fmtTime, fmtDuration, CATEGORY, ENGINE, TRIGGER, refresh, live } from '../core.js';
import { empty, errorState, startRun, confirm, preferredHeaded } from '../ui.js';
import { splitBar } from '../charts.js';

const PAGE = 50;
const kindTag = (value) => h('span', { class: 'tag' }, icon(value === 'ui' ? 'globe' : 'api'), value === 'ui' ? '页面' : '接口');
const active = (run) => ['Queued', 'Running'].includes(run.status);

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
  if (!data.items.length) {
    body = empty({ glyph: 'runs', title: status ? '没有符合条件的记录' : '这个范围内还没有执行',
      text: status ? '换一个结果筛选，或调整上方的项目与时间范围。' : '到「项目与用例」运行一次，结果会出现在这里。',
      actions: h('a', { class: 'btn primary', href: href('cases') }, icon('play'), '去运行用例') });
  } else if (mode === 'runs') {
    body = h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['状态', '项目', '结果', '触发', '引擎', '开始时间', '耗时'].map((t) => h('th', {}, t)))),
      h('tbody', {}, data.items.map((run) => h('tr', { class: 'click', onClick: () => go('runs/' + run.id) },
        h('td', {}, statusPill(run.status)),
        h('td', {}, h('div', { class: 'title' }, run.project_name),
          h('div', { class: 'sub ellipsis', style: 'max-width:360px' }, active(run) ? (run.current || '准备中') : (run.reason || `${run.total} 条用例`))),
        h('td', { style: 'min-width:170px' }, active(run)
          ? h('div', { class: 'stack', style: 'gap:4px' }, h('div', { class: 'progress' }, h('i', { style: `width:${run.total ? (run.done / run.total) * 100 : 0}%` })), h('span', { class: 'sub num' }, `${run.done}/${run.total}`))
          : h('div', { class: 'stack', style: 'gap:4px' }, splitBar(run), h('span', { class: 'sub num' }, `${run.passed} 通过 · ${run.failed} 失败 · ${run.blocked} 阻塞`))),
        h('td', { class: 'sub' }, TRIGGER[run.trigger] || run.trigger),
        h('td', { class: 'sub' }, ENGINE[run.engine]),
        h('td', { class: 'sub num' }, fmtTime(run.created_at)),
        h('td', { class: 'sub num' }, run.finished_at && run.started_at ? fmtDuration(new Date(run.finished_at) - new Date(run.started_at)) : '—')))));
  } else {
    body = h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['结果', '用例', '类型', '项目', '原因', '时间', '耗时'].map((t) => h('th', {}, t)))),
      h('tbody', {}, data.items.map((item) => h('tr', { class: 'click', onClick: () => go('runs/' + item.run_id, { case: item.case_id, view: null, status: null, offset: null }) },
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
    h('section', { class: 'card' }, body, pager));
}

// ---- one run ----
const openRows = new Set();
let reportFor = null;

async function report(view, runId) {
  let run;
  try { run = await api('/runs/' + runId); } catch (error) {
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  const focus = route().params.get('case');
  if (reportFor !== runId) {
    reportFor = runId;
    openRows.clear();
    if (focus) openRows.add(focus);
    else for (const row of run.results) if (!row.prerequisite && row.status !== 'Pass') openRows.add(row.case_id);  // failures open by default
  }
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

  const summary = h('section', { class: 'card run-summary' },
    h('div', { class: 'card-body' },
      h('div', { class: 'row wrap', style: 'gap:16px' },
        statusPill(run.status),
        h('div', { class: 'grow' }, h('h1', {}, run.project_name),
          h('p', { class: 'sub' }, `${TRIGGER[run.trigger] || run.trigger}触发 · 页面用例用 ${ENGINE[run.engine]} 回放 · ${fmtTime(run.created_at)}`)),
        running ? stop : [h('a', { class: 'btn', href: `/api/projects/${run.project_id}/report`, download: '', title: '用例、步骤、断言和每条用例的最近结果' }, icon('table'), '下载报告'), again, notPassed > 0 && retry]),
      running
        ? h('div', { class: 'stack', style: 'gap:8px;margin-top:16px' },
          h('div', { class: 'progress' }, h('i', { style: `width:${run.total ? (run.done / run.total) * 100 : 0}%` })),
          h('div', { class: 'row sub' }, h('i', { class: 'spin' }), run.status === 'Queued' ? '排队中，等待前一个运行结束' : `正在执行：${run.current || '准备中'}`,
            h('span', { class: 'grow' }), h('span', { class: 'num' }, `${run.done} / ${run.total}`)))
        : h('div', { class: 'stats' },
          stat('通过', run.passed, 'pass'), stat('失败', run.failed, 'fail'), stat('阻塞', run.blocked, 'blocked'),
          stat('用例', run.total), stat('总耗时', fmtDuration(spent))),
      run.reason && h('div', { class: 'banner bad', style: 'margin-top:16px' }, icon('warn'), run.reason)));

  const rows = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('h2', {}, '用例结果'),
      h('p', { class: 'sub' }, run.results.some((row) => row.prerequisite) ? '带「前置」标记的用例是为了满足依赖自动执行的，不计入统计。' : '点开一条查看每一步和每条断言。'))),
    run.results.length
      ? h('div', { class: 'result-list' }, run.results.map((row) => resultRow(run, row)))
      : empty({ glyph: 'clock', title: running ? '正在准备' : '没有结果', text: running ? '浏览器启动后，结果会一条条出现在这里。' : (run.reason || '这次运行没有产生结果。') }));

  replace(view,
    h('a', { class: 'back', href: href('runs') }, icon('right'), '执行记录'),
    summary, rows);
  if (focus) {
    const target = view.querySelector(`[data-case="${CSS.escape(focus)}"]`);
    if (target && !view.dataset.scrolled) { target.scrollIntoView({ block: 'center' }); view.dataset.scrolled = runId; }
  }
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
  if (d.assertions?.length) {
    parts.push(h('table', { class: 'table checks' },
      h('thead', {}, h('tr', {}, ['', '断言', '期望', '实际'].map((t) => h('th', {}, t)))),
      h('tbody', {}, d.assertions.map((check) => h('tr', { class: check.passed ? '' : 'bad' },
        h('td', { style: 'width:28px' }, h('span', { class: 'mark ' + (check.passed ? 'ok' : 'no') }, icon(check.passed ? 'check' : 'x'))),
        h('td', {}, check.name, check.fallback ? h('span', { class: 'tag', style: 'margin-left:6px' }, '备选定位') : null),
        h('td', { class: 'mono' }, show(check.expected)), h('td', { class: 'mono' }, show(check.actual)))))));
  }
  if (d.steps?.length) {
    parts.push(h('ol', { class: 'steps timeline' }, d.steps.map((step) => h('li', { class: step.status },
      h('span', { class: 'step-text' }, step.check && h('span', { class: 'tag accent' }, '断言'), step.text,
        step.fallback ? h('span', { class: 'tag' }, '备选定位') : null),
      h('span', { class: 'step-sub num' }, step.error || fmtDuration(step.ms))))));
  }
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
  for (const name of d.screenshots || []) {
    const url = `/api/runs/${run.id}/files/${encodeURIComponent(name)}`;
    parts.push(h('a', { class: 'shot', href: url, target: '_blank', rel: 'noreferrer', title: '在新标签页查看原图' },
      h('img', { src: url, alt: `${row.case_title} 结束时的页面截图`, loading: 'lazy' }),
      h('span', { class: 'sub' }, icon('image'), '结束时的页面截图（密码输入框已遮盖）')));
  }
  if (row.status !== 'Pass' && !row.prerequisite) {
    const again = h('button', { class: 'btn sm' }, icon('play'), '只重跑这一条');
    again.addEventListener('click', () => startRun([{ project_id: run.project_id, case_ids: [row.case_id] }], again));
    parts.push(h('div', { class: 'row' }, again));
  }
  return h('div', { class: 'result-body' }, parts);
}

function show(value) {
  if (value === true) return '是';
  if (value === false) return '否';
  if (value == null) return '—';
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return text.length > 160 ? text.slice(0, 160) + '…' : text;
}
