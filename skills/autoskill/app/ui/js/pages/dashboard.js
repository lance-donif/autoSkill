// Dashboard: every figure, chart and list below is computed from the same scope (projects x time range).
import { h, icon, replace, api, scope, scopeQuery, bounds, go, href, setScope,
  fmtNumber, fmtPercent, fmtTime, fmtUntil, fmtDuration, CATEGORY, ENGINE, TRIGGER, DEFAULT_RANGE, isDefaultScope, refresh } from '../core.js';
import { definition, empty, errorState, skeletonRows } from '../ui.js';
import { trendChart, trendTable, legend, fillBuckets, splitBar } from '../charts.js';
import { primaryState, stateResults } from '../status-view.js';

const DEFINITIONS = {
  rate: '首次尝试通过的用例数 ÷ 范围内的目标用例数。重试后通过率使用同一分母。目标数未知时不显示百分比。',
  cases: '截至所选时间范围结束时仍然有效（未归档）的用例数。',
  runs: '所选时间范围内开始的运行次数（手动 + 定时）。自动登录不计入。',
  passed: '全部步骤成功且所有适用断言通过的用例条次。没有断言时按步骤执行结果判断。',
  failed: '已开始执行后步骤、断言或结果校验失败的用例条次。',
  not_run: '本范围内尚未开始执行的目标用例条次。',
  trend: '每个时间段内的用例执行条次，按结果堆叠。范围不超过一天时按小时，否则按天。',
};
let showTable = false;
let currentOnly = false;

function kpi({ label, value, note, def, to, hero, extra }) {
  return h('a', { class: 'kpi' + (hero ? ' hero' : ''), href: to },
    h('span', { class: 'kpi-label' }, label, definition(def)),
    hero ? h('div', { class: 'hero-row' }, h('span', { class: 'kpi-value' }, value), extra) : h('span', { class: 'kpi-value' }, value),
    note && (hero ? note : h('span', { class: 'kpi-note' }, note)),
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
    replace(view, pageHead(null), h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  const r = summary.results, c = summary.cases, u = summary.runs;
  if (isDefaultScope() && !c.total && !u.total && !r.executions) { replace(view, pageHead(null), firstUse()); return; }
  const s = scope();
  const { start, end } = bounds(s);
  const points = fillBuckets(summary.trend.points, summary.trend.unit, start, end);
  const none = r.executions === 0;

  const kpis = summaryKpis(r, c, u, s, points, none);

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
      h('div', { class: 'grow' }, h('h2', { class: 'row' }, '执行趋势', definition(DEFINITIONS.trend)),
        h('p', { class: 'sub' }, summary.trend.unit === 'hour' ? '每小时的用例执行条次' : '每天的用例执行条次')),
      !none && legend(), !none && toggle),
    none
      ? empty({ glyph: 'runs', title: '这个范围内还没有执行',
        text: isDefaultScope(s) ? '运行一次用例，这里就会出现趋势。' : '换一个时间范围或项目，或者现在运行一次。',
        actions: [runAll({ primary: true }), !isDefaultScope(s) && h('button', { class: 'btn', onClick: () => setScope({ projects: [], range: DEFAULT_RANGE, from: '', to: '' }) }, icon('reset'), '重置筛选')] })
      : chartBody);

  replace(view, pageHead(summary), kpis,
    h('div', { class: 'dash-grid' }, trend, tasksCard(summary.tasks)),
    h('div', { class: 'dash-grid even' }, failuresCard(failures, r, view), recentCard(recent)));
}

function summaryKpis(r, c, u, s, _points, none) {
  const reliability = r.reliability || {};
  const heroNote = h('div', { class: 'stack', style: 'gap:8px' }, splitBar(r),
    h('span', { class: 'kpi-note' }, none ? '这个范围内还没有执行'
      : [h('b', {}, `${fmtNumber(reliability.first_passed || 0)} 历史首轮通过 / ${fmtNumber(reliability.denominator || 0)} 已知目标`),
        ` · 重试后通过率 ${fmtPercent(reliability.final_pass_rate)}`,
        reliability.unknown_target_runs ? ` · ${reliability.unknown_target_runs} 次运行的目标数仍未知` : '']));

  return h('section', { class: 'kpis', 'aria-label': '汇总指标' },
    kpi({ hero: true, label: '历史首轮通过率', value: fmtPercent(reliability.first_pass_rate), def: DEFINITIONS.rate, note: heroNote,
      to: href('runs', { view: 'results' }) }),
    kpi({ label: '用例总数', value: fmtNumber(c.total), def: DEFINITIONS.cases,
      note: [`页面 ${c.ui} · 接口 ${c.api}`, s.range !== 'all' && c.added ? ` · 新增 ${c.added}` : ''], to: href('cases') }),
    kpi({ label: '执行次数', value: fmtNumber(u.total), def: DEFINITIONS.runs,
      note: [`共 ${fmtNumber(r.executions)} 条次`, u.scheduled ? ` · 定时 ${u.scheduled}` : '', u.active ? ` · ${u.active} 个进行中` : ''], to: href('runs') }),
    kpi({ label: '未执行', value: fmtNumber(r.not_run || 0), def: DEFINITIONS.not_run,
      to: href('runs', { view: 'results', status: 'NotRun' }) }),
    kpi({ label: '通过', value: fmtNumber(r.passed), def: DEFINITIONS.passed,
      to: href('runs', { view: 'results', status: 'Pass' }) }),
    kpi({ label: '失败', value: fmtNumber(r.failed), def: DEFINITIONS.failed,
      note: r.failed > 0 && [h('b', {}, `${r.still_failing} 条`), ' 当前仍失败'], to: href('runs', { view: 'results', status: 'Fail' }) }));
}

function runAll(options = {}) {
  return h('a', { class: 'btn' + (options.primary === false ? '' : ' primary'), href: href('cases') }, icon('play'), '选择用例重新运行');
}

function pageHead(summary) {
  return h('div', { class: 'page-head' },
    h('div', { class: 'grow' }, h('h1', {}, '总览')),
    summary?.cases.total > 0 && runAll());
}

function tasksCard(tasks) {
  const body = tasks.total
    ? h('div', { class: 'list' }, tasks.tasks.map((task) => h('a', { class: 'list-row', href: href('schedules') },
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, task.title),
        h('div', { class: 'secondary-text ellipsis' }, `${task.project_name} · ${task.enabled ? '下次 ' + fmtUntil(task.next_due) : '已暂停'}`)),
      task.last_status ? h('div', { class: 'stack' }, primaryState({ state: task.last_run_state }), stateResults({ state: task.last_run_state })) : h('span', { class: 'tag' }, '未执行'))))
    : empty({ glyph: 'clock', title: '还没有定时任务',
      actions: h('a', { class: 'btn', href: href('schedules', { new: 1 }) }, icon('plus'), '新建定时任务') });
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', {}, '定时任务'),
        tasks.total > 0 && h('p', { class: 'sub' }, `启用 ${tasks.enabled} · 暂停 ${tasks.paused} · 范围内触发 ${tasks.fired_in_range} 次`)),
      tasks.total > 0 && h('a', { class: 'card-link', href: href('schedules') }, '全部', icon('right'))),
    body);
}

async function loadFailures(current) {
  const snapshot = await api('/dashboard' + scopeQuery({ current, limit: 6 }));
  return snapshot.failures;
}

function failuresBody(failures, r, current) {
  return [failures.items.length
    ? h('div', { class: 'list' }, failures.items.map((item) => h('a', { class: 'list-row', href: href('runs/' + item.run_id, { case: item.case_id }) },
      h('div', { class: 'stack', style: 'gap:4px' }, stateResults(item)),
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, item.case_title),
        h('div', { class: 'secondary-text ellipsis' }, `${item.project_name} · ${CATEGORY[item.category] || '未分类'} · ${item.reason || item.actual}`)),
      h('span', { class: 'secondary-text num' }, fmtTime(item.started_at)))))
    : empty({ glyph: 'check', title: current ? '没有仍在失败的用例' : '没有失败',
      text: r.executions ? '这个范围内没有失败记录。' : '这个范围内还没有执行。' }),
  failures.total > failures.items.length && h('div', { class: 'card-body', style: 'padding-top:8px' },
    h('a', { class: 'card-link', href: href('runs', { view: 'results', status: 'Fail' }) }, '查看全部', icon('right')))];
}

function failuresCard(failures, r) {
  let request = 0;
  const initialScope = scopeQuery();
  const subtitle = h('p', { class: 'sub' });
  const content = h('div', { dataset: { dashboardFailures: '' } }, failuresBody(failures, r, currentOnly));
  const note = () => { subtitle.textContent = currentOnly ? `最近一次执行仍为失败的用例 ${r.still_failing} 条` : `范围内共 ${r.failed} 条次`; };
  async function choose(current) {
    currentOnly = current;
    const mine = ++request;
    for (const [index, button] of [...toggle.children].entries()) button.setAttribute('aria-pressed', String(current === (index === 1)));
    content.setAttribute('aria-busy', 'true');
    try {
      const data = await loadFailures(current);
      if (mine !== request || !content.isConnected || initialScope !== scopeQuery()) return;
      replace(content, failuresBody(data, r, current));
      note();
    } catch (error) {
      if (mine === request && content.isConnected) replace(content, errorState(error, () => choose(current)));
    } finally {
      if (mine === request) content.removeAttribute('aria-busy');
    }
  }
  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': '失败列表范围' },
    h('button', { 'aria-pressed': String(!currentOnly), onClick: () => choose(false) }, '范围内全部'),
    h('button', { 'aria-pressed': String(currentOnly), onClick: () => choose(true) }, '当前仍失败'));
  note();
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', {}, '失败用例'),
        subtitle),
      toggle),
    content);
}

function recentCard(recent) {
  const body = recent.items.length
    ? h('div', { class: 'list' }, recent.items.map((run) => h('a', { class: 'list-row', href: href('runs/' + run.id) },
      h('div', { class: 'stack', style: 'gap:4px' }, primaryState(run), stateResults(run)),
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, run.project_name),
        h('div', { class: 'secondary-text ellipsis' }, ['Queued', 'Running'].includes(run.status)
          ? `${run.done}/${run.total} · ${run.current || '准备中'}`
          : `${run.passed}/${run.total} 通过 · ${TRIGGER[run.trigger] || run.trigger} · ${ENGINE[run.engine] || run.engine || '未记录'}${run.finished_at && run.started_at ? ' · ' + fmtDuration(new Date(run.finished_at) - new Date(run.started_at)) : ''}`)),
      h('span', { class: 'secondary-text num' }, fmtTime(run.created_at)))))
    : empty({ glyph: 'runs', title: '还没有执行记录' });
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', {}, '最近执行'), h('p', { class: 'sub' }, `范围内共 ${recent.total} 次`)),
      recent.total > 0 && h('a', { class: 'card-link', href: href('runs') }, '全部', icon('right'))),
    body);
}

export { skeletonRows };
