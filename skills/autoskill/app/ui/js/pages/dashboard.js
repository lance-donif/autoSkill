// Dashboard: every figure, chart and list below is computed from the same scope (projects x time range).
import { h, icon, replace, api, scope, scopeQuery, bounds, go, href, setScope, statusPill, copyText, attempt,
  fmtNumber, fmtPercent, fmtTime, fmtUntil, fmtDuration, CATEGORY, ENGINE, TRIGGER, DEFAULT_RANGE, isDefaultScope, refresh } from '../core.js';
import { definition, empty, errorState, skeletonRows, runButton } from '../ui.js';
import { trendChart, trendTable, legend, fillBuckets, splitBar } from '../charts.js';
import { state, refreshShell } from '../shell.js';

const DEFINITIONS = {
  rate: '首轮业务通过率 = 首次尝试业务断言通过数 ÷ 本范围创建的运行内全部目标用例。阻塞、取消、未完成、无业务断言均在分母；最终通过率使用相同分母。原生目标数仍未知时不显示百分比。',
  cases: '截至所选时间范围结束时仍然有效（未归档）的用例数。',
  runs: '所选时间范围内开始的运行次数（手动 + 定时）。自动登录不计入。',
  failed: '所选时间范围内结果为「失败」的用例执行条次：页面或接口的实际结果与预期不一致。',
  blocked: '所选时间范围内结果为「阻塞」的用例执行条次：没有真正验证到结果，例如前置用例未通过、缺少凭据、环境不可达。',
  trend: '每个时间段内的用例执行条次，按结果堆叠。范围不超过一天时按小时，否则按天。',
};
let showTable = false;
let currentOnly = false;

function kpi({ label, value, note, def, to, hero, extra }) {
  return h('a', { class: 'kpi' + (hero ? ' hero' : ''), href: to },
    h('span', { class: 'kpi-label' }, label, definition(def)),
    hero ? h('div', { class: 'hero-row' }, h('span', { class: 'kpi-value' }, value), extra) : h('span', { class: 'kpi-value' }, value),
    hero && note ? note : h('span', { class: 'kpi-note' }, note),
    h('span', { class: 'kpi-go' }, icon('right')));
}

function firstUse() {
  const skill = state.status ? state.status.skill_path : '<Skill 目录>';
  const prompt = `请使用 autoSkill 技能（目录：${skill}）测试 <要测试的网址>：先阅读其中的 SKILL.md，按说明创建项目、录制页面操作并生成可回放的页面用例和接口用例，最后回放一遍确认通过。`;
  const demoButton = h('button', { class: 'btn primary' }, icon('spark'), '加载示例项目');
  demoButton.addEventListener('click', async () => {
    const done = await attempt(() => api('/demo', { method: 'POST' }), demoButton);
    if (done) { await refreshShell(); go('cases', { projects: done.id }, false); }
  });
  return h('div', { class: 'card' }, empty({
    glyph: 'spark', title: '还没有项目',
    text: '项目里的用例由 AI 在操作页面时录制得到，之后不需要 AI 就能反复回放。先用示例项目看看完整效果，或把下面这句话交给你的 AI 助手开始录制。',
    actions: [demoButton, h('button', { class: 'btn', onClick: () => copyText(prompt) }, icon('copy'), '复制给 AI 的指令')],
  }), h('div', { class: 'card-body', style: 'padding-top:0' }, h('pre', { class: 'prompt' }, prompt)));
}

export async function render(view) {
  if (!state.projects.length && state.online) { replace(view, pageHead(null), firstUse()); return; }
  let summary, failures, recent;
  try {
    [summary, failures, recent] = await Promise.all([
      api('/summary' + scopeQuery()),
      currentOnly ? api('/results' + scopeQuery({ limit: 6, status: 'Fail', current: true })) : null,
      api('/runs' + scopeQuery({ limit: 6, exclude_load: true })),
    ]);
    if (!currentOnly) {
      const [failed, blocked] = await Promise.all([
        api('/results' + scopeQuery({ limit: 6, status: 'Fail' })), api('/results' + scopeQuery({ limit: 6, status: 'Blocked' })),
      ]);
      failures = { total: failed.total + blocked.total,
        items: [...failed.items, ...blocked.items].sort((a, b) => (a.started_at < b.started_at ? 1 : -1)).slice(0, 6) };
    }
  } catch (error) {
    replace(view, pageHead(null), h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  const r = summary.results, c = summary.cases, u = summary.runs;
  const s = scope();
  const { start, end } = bounds(s);
  const points = fillBuckets(summary.trend.points, summary.trend.unit, start, end);
  const none = r.executions === 0;

  const kpis = summaryKpis(r, c, u, s, points, none);

  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': '趋势展示方式' },
    h('button', { 'aria-pressed': String(!showTable), onClick: () => { showTable = false; refresh(); } }, '图表'),
    h('button', { 'aria-pressed': String(showTable), onClick: () => { showTable = true; refresh(); } }, '表格'));
  const pickDay = (point) => {
    const day = point.bucket.slice(0, 10);
    go('runs', { view: 'results', range: 'custom', from: day, to: day });
  };
  const trend = h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', { class: 'row' }, '执行趋势', definition(DEFINITIONS.trend)),
        h('p', { class: 'sub' }, summary.trend.unit === 'hour' ? '每小时的用例执行条次' : '每天的用例执行条次')),
      !none && legend(), !none && toggle),
    none
      ? empty({ glyph: 'runs', title: '这个范围内还没有执行',
        text: isDefaultScope(s) ? '运行一次用例，这里就会出现趋势。' : '换一个时间范围或项目，或者现在运行一次。',
        actions: [runAll({ primary: true }), !isDefaultScope(s) && h('button', { class: 'btn', onClick: () => setScope({ projects: [], range: DEFAULT_RANGE, from: '', to: '' }) }, icon('reset'), '重置筛选')] })
      : h('div', { class: 'card-body' }, showTable ? trendTable(points, summary.trend.unit) : trendChart(points, summary.trend.unit, pickDay)));

  replace(view, pageHead(summary), kpis,
    h('div', { class: 'dash-grid' }, trend, tasksCard(summary.tasks)),
    h('div', { class: 'dash-grid even' }, failuresCard(failures, r, view), recentCard(recent)));
}

function summaryKpis(r, c, u, s, _points, none) {
  const reliability = r.reliability || {};
  const heroNote = h('div', { class: 'stack', style: 'gap:8px' }, splitBar(r),
    h('span', { class: 'kpi-note' }, none ? '这个范围内还没有执行'
      : [h('b', {}, `${fmtNumber(reliability.first_passed || 0)} 首轮业务通过 / ${fmtNumber(reliability.denominator || 0)} 已知目标`),
        ` · 最终 ${fmtPercent(reliability.final_pass_rate)} · ${fmtNumber(reliability.unverified || 0)} 业务未验证`,
        reliability.unknown_target_runs ? ` · ${reliability.unknown_target_runs} 次运行的目标数仍未知` : '']));

  return h('section', { class: 'kpis', 'aria-label': '汇总指标' },
    kpi({ hero: true, label: '首轮业务通过率', value: fmtPercent(reliability.first_pass_rate), def: DEFINITIONS.rate, note: heroNote,
      to: href('runs', { view: 'results' }) }),
    kpi({ label: '用例总数', value: fmtNumber(c.total), def: DEFINITIONS.cases,
      note: [`页面 ${c.ui} · 接口 ${c.api}`, s.range !== 'all' && c.added ? ` · 新增 ${c.added}` : ''], to: href('cases') }),
    kpi({ label: '执行次数', value: fmtNumber(u.total), def: DEFINITIONS.runs,
      note: [`共 ${fmtNumber(r.executions)} 条次`, u.scheduled ? ` · 定时 ${u.scheduled}` : '', u.active ? ` · ${u.active} 个进行中` : ''], to: href('runs') }),
    kpi({ label: '失败', value: fmtNumber(r.failed), def: DEFINITIONS.failed,
      note: r.failed ? [h('b', {}, `${r.still_failing} 条`), ' 当前仍失败'] : '没有失败', to: href('runs', { view: 'results', status: 'Fail' }) }),
    kpi({ label: '阻塞', value: fmtNumber(r.blocked), def: DEFINITIONS.blocked,
      note: r.blocked ? '没有验证到结果' : '没有阻塞', to: href('runs', { view: 'results', status: 'Blocked' }) }));
}

function runAll(options = {}) {
  const s = scope();
  const targets = (s.projects.length ? state.projects.filter((p) => s.projects.includes(p.id)) : state.projects).filter((p) => p.cases?.total);
  const label = targets.length > 1 ? `运行 ${targets.length} 个项目` : '运行全部用例';
  return runButton(label, () => targets.map((p) => ({ project_id: p.id })), options);
}

function pageHead() {
  return h('div', { class: 'page-head' },
    h('div', { class: 'grow' }, h('h1', {}, '仪表盘'), h('p', { class: 'sub' }, '下面所有数字、图表和列表都按上方的项目与时间范围统计。')),
    state.projects.some((p) => p.cases?.total) && runAll());
}

function tasksCard(tasks) {
  const body = tasks.total
    ? h('div', { class: 'list' }, tasks.tasks.map((task) => h('a', { class: 'list-row', href: href('schedules') },
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, task.title),
        h('div', { class: 'secondary-text ellipsis' }, `${task.project_name} · ${task.enabled ? '下次 ' + fmtUntil(task.next_due) : '已暂停'}`)),
      task.last_status ? statusPill(task.last_status) : h('span', { class: 'tag' }, '尚未执行'))))
    : empty({ glyph: 'clock', title: '还没有定时任务', text: '把稳定的回归安排在固定时间自动执行。',
      actions: h('a', { class: 'btn', href: href('schedules', { new: 1 }) }, icon('plus'), '新建定时任务') });
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', {}, '定时任务'),
        h('p', { class: 'sub' }, tasks.total ? `启用 ${tasks.enabled} · 暂停 ${tasks.paused} · 范围内触发 ${tasks.fired_in_range} 次` : '所选项目内')),
      tasks.total > 0 && h('a', { class: 'card-link', href: href('schedules') }, '全部', icon('right'))),
    body);
}

function failuresCard(failures, r, _view) {
  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': '失败列表范围' },
    h('button', { 'aria-pressed': String(!currentOnly), onClick: () => { currentOnly = false; refresh(); } }, '范围内全部'),
    h('button', { 'aria-pressed': String(currentOnly), onClick: () => { currentOnly = true; refresh(); } }, '当前仍失败'));
  const body = failures.items.length
    ? h('div', { class: 'list' }, failures.items.map((item) => h('a', { class: 'list-row', href: href('runs/' + item.run_id, { case: item.case_id }) },
      statusPill(item.status),
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, item.case_title),
        h('div', { class: 'secondary-text ellipsis' }, `${item.project_name} · ${CATEGORY[item.category] || '未分类'} · ${item.reason || item.actual}`)),
      h('span', { class: 'secondary-text num' }, fmtTime(item.started_at)))))
    : empty({ glyph: 'check', title: currentOnly ? '没有仍在失败的用例' : '没有失败或阻塞',
      text: r.executions ? '这个范围内没有失败或阻塞记录；业务验证度见上方汇总。' : '这个范围内还没有执行。' });
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', {}, '失败与阻塞'),
        h('p', { class: 'sub' }, currentOnly ? `最近一次执行仍为失败的用例 ${r.still_failing} 条` : `范围内共 ${r.failed + r.blocked} 条次`)),
      toggle),
    body,
    failures.total > failures.items.length && h('div', { class: 'card-body', style: 'padding-top:8px' },
      h('a', { class: 'card-link', href: href('runs', { view: 'results', status: 'Fail' }) }, '查看全部', icon('right'))));
}

function recentCard(recent) {
  const body = recent.items.length
    ? h('div', { class: 'list' }, recent.items.map((run) => h('a', { class: 'list-row', href: href('runs/' + run.id) },
      statusPill(run.status),
      h('span', { class: 'grow' },
        h('div', { class: 'primary-text ellipsis' }, run.project_name),
        h('div', { class: 'secondary-text ellipsis' }, ['Queued', 'Running'].includes(run.status)
          ? `${run.done}/${run.total} · ${run.current || '准备中'}`
          : `${run.passed}/${run.total} 通过 · ${TRIGGER[run.trigger] || run.trigger} · ${ENGINE[run.engine]}${run.finished_at && run.started_at ? ' · ' + fmtDuration(new Date(run.finished_at) - new Date(run.started_at)) : ''}`)),
      h('span', { class: 'secondary-text num' }, fmtTime(run.created_at)))))
    : empty({ glyph: 'runs', title: '还没有执行记录', text: '运行一次用例后，这里显示最近的执行。' });
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', { class: 'grow' }, h('h2', {}, '最近执行'), h('p', { class: 'sub' }, `范围内共 ${recent.total} 次`)),
      recent.total > 0 && h('a', { class: 'card-link', href: href('runs') }, '全部', icon('right'))),
    body);
}

export { skeletonRows };
