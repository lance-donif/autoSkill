// Dashboard: every figure, chart and list below is computed from the same scope (projects x time range).
import { h, icon, replace, api, scope, scopeQuery, bounds, go, href, setScope,
  fmtNumber, fmtPercent, fmtTime, fmtUntil, fmtDuration, CATEGORY, ENGINE, TRIGGER, DEFAULT_RANGE, isDefaultScope, refresh } from '../core.js';
import { definition, empty, errorState, skeletonRows } from '../ui.js';
import { trendChart, trendTable, legend, fillBuckets, splitBar } from '../charts.js';
import { primaryState, stateResults } from '../status-view.js';

const DEFINITIONS = {
  rate: '最近一批运行通过的目标用例数 ÷ 该批目标用例数。',
  cases: '截至所选时间范围结束时仍然有效（未归档）的用例数。',
  passed: '全部步骤成功且所有适用断言通过的用例条次。没有断言时按步骤执行结果判断。',
  failed: '已开始执行后步骤、断言或结果校验失败的用例条次。',
  trend: '每个时间段内的用例执行条次，按结果堆叠。范围不超过一天时按小时，否则按天。',
};
let showTable = false;
let currentOnly = false;

function kpi({ label, value, note, def, to, bar }) {
  return h('a', { class: 'kpi' + (bar ? ' hero' : ''), href: to },
    h('span', { class: 'kpi-label' }, label, definition(def)),
    h('span', { class: 'kpi-value' }, value),
    bar,
    note && h('span', { class: 'kpi-note' }, note),
    h('span', { class: 'kpi-go' }, icon('right')));
}

function firstUse() {
  return h('div', { class: 'card' }, empty({
    glyph: 'spark', title: '还没有测试记录',
    text: '在 DSH 对话中告诉 AI 测试地址与目标，执行后这里会自动显示进度和结果。',
  }));
}

export async function render(view) {
  let summary, failures, recent;
  try {
    ({ summary, failures, recent } = await api('/dashboard' + scopeQuery({ current: currentOnly, limit: 6 })));
  } catch (error) {
    replace(view, pageHead(), h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  const r = summary.results, c = summary.cases, u = summary.runs;
  if (isDefaultScope() && !c.total && !u.total && !r.executions) { replace(view, pageHead(), firstUse()); return; }
  const s = scope();
  const { start, end } = bounds(s);
  const points = fillBuckets(summary.trend.points, summary.trend.unit, start, end);
  const none = r.executions === 0;

  const kpis = summaryKpis(r, c, u.latest_batch, s);

  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': '趋势展示方式' },
    h('button', { 'aria-pressed': String(!showTable), onClick: () => changeTrend(false) }, '图表'),
    h('button', { 'aria-pressed': String(showTable), onClick: () => changeTrend(true) }, '表格'));
  const pickDay = (point) => {
    const day = point.bucket.slice(0, 10);
    go('runs', { view: 'results', range: 'custom', from: day, to: day });
  };
  const chartBody = h('div', { class: 'card-body' }, showTable ? trendTable(points, summary.trend.unit) : trendChart(points, summary.trend.unit, pickDay));
  function changeTrend(table) {
    showTable = table;
    for (const [index, button] of [...toggle.children].entries()) button.setAttribute('aria-pressed', String(table === (index === 1)));
    replace(chartBody, table ? trendTable(points, summary.trend.unit) : trendChart(points, summary.trend.unit, pickDay));
  }
  const trend = h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h2', { class: 'grow row' }, '执行趋势', definition(DEFINITIONS.trend)),
      !none && legend(), !none && toggle),
    none
      ? empty({ glyph: 'runs', title: '暂无执行记录',
        text: isDefaultScope(s) ? '运行一次用例，这里就会出现趋势。' : '换一个时间范围或项目，或者现在运行一次。',
        actions: [h('a', { class: 'btn primary', href: href('cases') }, icon('play'), '选择用例重新运行'),
          !isDefaultScope(s) && h('button', { class: 'btn', onClick: () => setScope({ projects: [], range: DEFAULT_RANGE, from: '', to: '' }) }, icon('reset'), '重置筛选')] })
      : chartBody);

  replace(view, pageHead(), kpis,
    h('div', { class: 'dash-grid' }, trend, tasksCard(summary.tasks)),
    h('div', { class: 'dash-grid even' }, failuresCard(failures), recentCard(recent)));
}

function summaryKpis(r, c, batch, s) {
  return h('section', { class: 'kpis', 'aria-label': '汇总指标' },
    kpi({ label: '最近一批通过率', value: fmtPercent(batch?.pass_rate), def: DEFINITIONS.rate,
      bar: splitBar(batch || {}), to: href('runs') }),
    kpi({ label: '用例总数', value: fmtNumber(c.total), def: DEFINITIONS.cases,
      note: [`页面 ${c.ui} · 接口 ${c.api}`, s.range !== 'all' && c.added ? ` · 新增 ${c.added}` : ''], to: href('cases') }),
    kpi({ label: '通过', value: fmtNumber(r.passed), def: DEFINITIONS.passed,
      to: href('runs', { view: 'results', status: 'Pass' }) }),
    kpi({ label: '失败', value: fmtNumber(r.failed), def: DEFINITIONS.failed,
      to: href('runs', { view: 'results', status: 'Fail' }) }));
}

function pageHead() {
  return h('div', { class: 'page-head' }, h('h1', {}, '总览'));
}

function tasksCard(tasks) {
  const body = tasks.total
    ? h('div', { class: 'list' }, tasks.tasks.map((task) => h('a', { class: 'list-row', href: href('schedules') },
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, task.title),
        h('div', { class: 'secondary-text ellipsis' }, `${task.project_name} · ${task.enabled ? '下次 ' + fmtUntil(task.next_due) : '已暂停'}`)),
      h('div', { class: 'stack' }, task.last_status && primaryState({ state: task.last_run_state }), stateResults({ state: task.last_run_state })))))
    : empty({ glyph: 'clock', title: '还没有定时任务',
      actions: h('a', { class: 'btn', href: href('schedules', { new: 1 }) }, icon('plus'), '新建定时任务') });
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', {}, '定时任务'),
        tasks.total > 0 && h('p', { class: 'sub' }, `启用 ${tasks.enabled} · 暂停 ${tasks.paused}`)),
      tasks.total > 0 && h('a', { class: 'card-link', href: href('schedules') }, '全部', icon('right'))),
    body);
}

async function loadFailures(current) {
  const snapshot = await api('/dashboard' + scopeQuery({ current, limit: 6 }));
  return snapshot.failures;
}

function failuresBody(failures, current) {
  return [failures.items.length
    ? h('div', { class: 'list' }, failures.items.map((item) => h('a', { class: 'list-row', href: href('runs/' + item.run_id, { case: item.case_id }) },
      h('div', { class: 'stack', style: 'gap:4px' }, stateResults(item)),
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, item.case_title),
        h('div', { class: 'secondary-text ellipsis' }, `${item.project_name} · ${CATEGORY[item.category] || '未分类'} · ${item.reason || item.actual}`)),
      h('span', { class: 'secondary-text num' }, fmtTime(item.started_at)))))
    : empty({ glyph: 'check', title: current ? '没有仍在失败的用例' : '没有失败' }),
  failures.total > failures.items.length && h('div', { class: 'card-body', style: 'padding-top:8px' },
    h('a', { class: 'card-link', href: href('runs', { view: 'results', status: 'Fail' }) }, '查看全部', icon('right')))];
}

function failuresCard(failures) {
  let request = 0;
  const initialScope = scopeQuery();
  const content = h('div', { dataset: { dashboardFailures: '' } }, failuresBody(failures, currentOnly));
  async function choose(current) {
    currentOnly = current;
    const mine = ++request;
    for (const [index, button] of [...toggle.children].entries()) button.setAttribute('aria-pressed', String(current === (index === 1)));
    content.setAttribute('aria-busy', 'true');
    try {
      const data = await loadFailures(current);
      if (mine !== request || !content.isConnected || initialScope !== scopeQuery()) return;
      replace(content, failuresBody(data, current));
    } catch (error) {
      if (mine === request && content.isConnected) replace(content, errorState(error, () => choose(current)));
    } finally {
      if (mine === request) content.removeAttribute('aria-busy');
    }
  }
  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': '失败列表范围' },
    h('button', { 'aria-pressed': String(!currentOnly), onClick: () => choose(false) }, '全部'),
    h('button', { 'aria-pressed': String(currentOnly), onClick: () => choose(true) }, '当前仍失败'));
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h2', { class: 'grow' }, '失败用例'),
      toggle),
    content);
}

function recentCard(recent) {
  const body = recent.items.length
    ? h('div', { class: 'list' }, recent.items.map((run) => h('a', { class: 'list-row', href: href('runs/' + run.id) },
      h('div', { class: 'stack', style: 'gap:4px' }, !['settled', 'completed', 'blocked'].includes(run.state?.phase || run.state?.lifecycle) && primaryState(run), stateResults(run)),
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, run.project_name),
        h('div', { class: 'secondary-text ellipsis' }, ['Queued', 'Running'].includes(run.status)
          ? `${run.done}/${run.total} · ${run.current || '准备中'}`
          : `${run.passed}/${run.total} 通过 · ${TRIGGER[run.trigger] || run.trigger} · ${ENGINE[run.engine] || run.engine || '未记录'}${run.finished_at && run.started_at ? ' · ' + fmtDuration(new Date(run.finished_at) - new Date(run.started_at)) : ''}`)),
      h('span', { class: 'secondary-text num' }, fmtTime(run.created_at)))))
    : empty({ glyph: 'runs', title: '还没有执行记录' });
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h2', { class: 'grow' }, '最近执行'),
      recent.total > 0 && h('a', { class: 'card-link', href: href('runs') }, '全部', icon('right'))),
    body);
}

export { skeletonRows };
