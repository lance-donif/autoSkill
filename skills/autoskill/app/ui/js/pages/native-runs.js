// Native reports keep business quality, performance and each conversation turn visible.
import { h, icon, replace, api, go, href, route, live, refresh, statusPill, fmtTime, fmtDuration, fmtPercent, fmtNumber } from '../core.js';
import { empty, errorState } from '../ui.js';
import { primaryState, stateDetails, stateResults } from '../status-view.js';
import { updateLiveView } from '../live-refresh.js';

import { cancelButton, rerunButton, runActionFeedback, resultConclusion, recoveryActions, returnToRuns } from './run-actions.js';
import { reportControls, reportDownloadLink } from './report-controls.js';

const expanded = new Map();
const issueResults = new Map();
const pendingDetails = new WeakMap();
const evidenceFiles = new Map();
const show = (value) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
const names = {
  planned_cases: '计划对话数', completed_cases: '完成对话数', quality_passed: '质量通过数', quality_pass_rate: '质量通过率',
  failed_cases: '失败对话数', not_run_cases: '未执行对话数', sent_turns: '已发送轮次',
  concurrency: '并发会话数', elapsed_ms: '总耗时（ms）', throughput_turns_per_second: '吞吐（轮/秒）',
  p50_ms: 'P50 耗时（ms）', p95_ms: 'P95 耗时（ms）', p99_ms: 'P99 耗时（ms）', samples: '采样数', failed_samples: '失败采样数',
  elapsed_seconds: '总耗时（秒）', throughput_per_second: '吞吐（采样/秒）', max_threads: '最大线程数', error_rate: '错误率',
};
const fileUrl = (id, path) => `/api/native-runs/${encodeURIComponent(id)}/files/${String(path).split('/').map(encodeURIComponent).join('/')}`;

function runIdentity(run) {
  return `${run.project_name || run.project_id} · ${fmtTime(run.created_at)}`;
}

function outcomeReason(run) {
  const reason = run.reason || run.outcome?.reason;
  return reason && h('div', { class: 'banner bad' }, icon('warn'), reason);
}

async function allSummaries(id) {
  const rows = [];
  let cursor = null;
  do {
    const page = await api(`/native-runs/${encodeURIComponent(id)}?limit=100${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`);
    rows.push(...page.results);
    cursor = page.page?.has_more ? page.page.next_cursor : null;
  } while (cursor);
  return rows;
}

async function focusedResult(id, focus) {
  try { return { data: await api(`/native-runs/${encodeURIComponent(id)}?case=${encodeURIComponent(focus)}`) }; }
  catch (error) {
    if (!focus.startsWith('result:') && error.message.includes('相同用例编号')) {
      try { return { error, matches: (await allSummaries(id)).filter((row) => row.case_id === focus) }; }
      catch (problem) { return { error: problem }; }
    }
    return { error };
  }
}

function focusedPanel(run, focus, target) {
  if (!target) return null;
  if (target.error) return h('section', { class: 'card card-body stack', role: 'status' },
    h('p', { class: 'error' }, target.error.message),
    target.matches?.map((row) => h('a', { class: 'btn', href: href('native-runs/' + run.id, { ...Object.fromEntries(route().params), case: 'result:' + row.id }) },
      `查看结果 ${row.id} · ${row.case_title}`, stateResults(row))));
  const row = target.data.results[0];
  const key = run.id + '/' + row.id;
  if (!expanded.has(key)) expanded.set(key, true);
  return h('section', { class: 'card', 'aria-label': '定位的执行结果' },
    h('div', { class: 'card-head' }, h('h2', {}, '定位的执行结果'),
      h('a', { class: 'btn sm', href: href('native-runs/' + run.id, Object.fromEntries([...route().params].filter(([key]) => key !== 'case'))) }, '返回本次结果列表')),
    caseRow(run, row, target.data.case), h('p', { class: 'sub mono' }, '结果标识：' + focus));
}

function issuesPanel(run) {
  const entry = issueResults.get(run.id);
  const button = h('button', { class: 'btn', disabled: entry?.loading, onClick: async () => {
    issueResults.set(run.id, { loading: true }); refresh();
    try {
      const rows = (await allSummaries(run.id)).filter((row) => row.state?.outcome === 'fail');
      issueResults.set(run.id, { rows });
    } catch (error) { issueResults.set(run.id, { error: error.message }); }
    refresh();
  } }, entry?.loading ? '正在查找本次问题结果…' : '查看本次所有失败结果');
  return h('div', { class: 'stack' }, button,
    entry?.error && h('p', { class: 'error', role: 'status' }, entry.error),
    entry?.rows && h('div', { class: 'stack', 'aria-label': '本次失败结果' },
      h('p', { class: 'sub' }, `共 ${entry.rows.length} 条失败结果。`),
      entry.rows.map((row) => h('a', { class: 'card-link', href: href('native-runs/' + run.id, { ...Object.fromEntries(route().params), case: 'result:' + row.id }) },
        row.case_title, ' · ', stateResults(row), row.state?.reason || ''))));
}

export async function render(view, { id }) {
  if (!id) { go('runs'); return; }
  const params = route().params;
  const cursor = params.get('cursor');
  const focus = params.get('case');
  let run;
  let target;
  try {
    [run, target] = await Promise.all([api(`/native-runs/${encodeURIComponent(id)}?limit=50${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`),
      focus ? focusedResult(id, focus) : null]);
  } catch (error) { replace(view, errorState(error, refresh)); return; }
  const running = ['Queued', 'Running'].includes(run.status);
  if (running) live.until = Date.now() + 8000;
  const outcome = run.outcome || {};
  const rows = run.results || [];
  replace(view,
    h('a', { class: 'back', href: returnToRuns() }, icon('right'), '返回执行记录'),
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '执行报告'),
      h('p', { class: 'sub' }, runIdentity(run)))),
    h('section', { class: 'card card-body stack report-summary' }, resultConclusion(run),
      h('div', { class: 'row wrap' }, primaryState(run),
        h('span', { class: 'sub' }, `${run.done || 0} / ${run.planned_unknown ? '目标数待执行器发现' : run.total} 已记录`)),
      outcomeReason(run),
      reportControls(run, run.report?.available && reportDownloadLink({ key: (run.test_report?.report_id || run.id) + ':native',
        url: fileUrl(run.id, 'outcome.json'), label: '下载原生汇总 JSON', filename: 'outcome.json' })),
      h('div', { class: 'row wrap' }, running ? cancelButton(run) : rerunButton(run)),
      recoveryActions(run), runActionFeedback(run), issuesPanel(run),
      h('details', { class: 'more report-technical' }, h('summary', {}, '执行与技术详情'),
        h('p', { class: 'sub mono' }, `${run.id} · ${run.engine} · 资产 ${run.asset_id} · 版本 ${run.asset_version}`),
        stateDetails(run), metricsTable(outcome.metrics || {}),
        h('p', { class: 'sub' }, '重新运行沿用本次资产版本和参数，并保留本次结果。'))),
    focusedPanel(run, focus, target),
    h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('h2', {}, '用例与多轮结果'),
      h('p', { class: 'sub' }, '展开用例核对问题、实际回答、断言和耗时。'))),
    rows.length ? h('div', { class: 'result-list' }, rows.map((row) => caseRow(run, row)))
      : empty({ glyph: running ? 'clock' : 'runs', title: running ? '等待执行器产生结果' : '没有用例结果', text: outcome.reason }),
    run.page?.has_more && h('div', { class: 'card-body row' }, h('a', { class: 'btn', href: href('native-runs/' + id, { ...Object.fromEntries(params), case: '', cursor: run.page.next_cursor }) }, '下一页')),
    cursor && h('div', { class: 'card-body' }, h('a', { class: 'btn', href: href('native-runs/' + id, Object.fromEntries([...params].filter(([key]) => !['cursor', 'case'].includes(key)))) }, '回到第一页'))),
    evidencePanel(run));
  // The refresh must see the complete expanded bodies before restoring their reading state.
  await Promise.all([...view.querySelectorAll('.result-body')].map((body) => pendingDetails.get(body)));
}

export function commit(view, next) { updateLiveView(view, next); }

function metricsTable(metrics) {
  const entries = Object.entries(metrics).filter(([, value]) => value == null || typeof value !== 'object');
  if (!entries.length) return h('p', { class: 'sub' }, '当前执行器尚未提供性能统计。');
  return h('table', { class: 'table' }, h('thead', {}, h('tr', {}, h('th', {}, '指标'), h('th', {}, '实际值'))),
    h('tbody', {}, entries.map(([key, value]) => h('tr', {}, h('td', {}, names[key] || key),
      h('td', { class: 'num' }, key.endsWith('_rate') ? fmtPercent(value) : typeof value === 'number' ? fmtNumber(Math.round(value * 1000) / 1000) : show(value))))));
}

function checksTable(checks, title) {
  if (!checks?.length) return h('p', { class: 'sub' }, `${title}：没有独立断言`);
  return h('div', { class: 'stack', style: 'gap:8px' }, h('h3', {}, title), h('table', { class: 'table checks' },
    h('thead', {}, h('tr', {}, ['', '断言', '期望', '实际'].map((label) => h('th', {}, label)))),
    h('tbody', {}, checks.map((check) => h('tr', { class: check.passed ? '' : 'bad' },
      h('td', {}, statusPill(check.passed ? 'Pass' : 'Fail')),
      h('td', {}, check.name || check.label || check.kind || '原生断言'),
      h('td', { class: 'mono' }, show(check.expected)), h('td', { class: 'mono' }, show(check.actual)))))));
}

function turnBody(turn) {
  return h('section', { class: 'stack', style: 'gap:12px' },
    h('div', { class: 'row wrap' }, h('h3', {}, `第 ${turn.turn} 轮`), ['Pass', 'Fail', 'NotRun'].includes(turn.status) && statusPill(turn.status)),
    h('div', {}, h('div', { class: 'sub' }, '问题'), h('pre', { class: 'code' }, show(turn.question))),
    h('div', {}, h('div', { class: 'sub' }, '实际回答'), h('pre', { class: 'code' }, show(turn.answer))),
    turn.reason && h('p', { class: 'error' }, turn.reason),
    checksTable(turn.assertions, '业务断言'), checksTable(turn.performance_assertions, '性能目标'),
    turn.timings && h('details', { class: 'more' }, h('summary', {}, '响应与流式耗时'), metricsTable(turn.timings)),
    turn.judgement && h('details', { class: 'more', open: turn.business_status !== 'Pass' }, h('summary', {}, '宿主判定与事实依据'),
      h('pre', { class: 'code' }, show(turn.judgement))));
}

function caseRow(run, row, initial = null) {
  const key = run.id + '/' + row.id;
  const body = h('div', { class: 'result-body', hidden: !expanded.get(key) });
  let loaded = false;
  async function load() {
    replace(body, h('p', { class: 'sub' }, '正在读取完整结果…'));
    try {
      const detail = initial || (await api(`/native-runs/${encodeURIComponent(run.id)}?case=${encodeURIComponent('result:' + row.id)}`)).case;
      replace(body, stateDetails({ state: detail.state || row.state }), h('span', { class: 'sub' }, fmtDuration(row.duration_ms)),
        detail.reason && h('p', { class: 'error' }, detail.reason),
        detail.turns?.length ? detail.turns.map(turnBody) : checksTable(detail.assertions, '原生断言'),
        h('details', { class: 'more' }, h('summary', {}, '完整用例结果'), h('pre', { class: 'code' }, show(detail))));
      loaded = true;
    } catch (error) { replace(body, errorState(error, load)); }
  }
  const button = h('button', { class: 'result-head', 'aria-expanded': String(!!expanded.get(key)), onClick: () => {
    const open = body.hidden;
    expanded.set(key, open);
    body.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open && !loaded) load();
  } }, icon('right', 'icon-svg caret'),
  h('span', { class: 'grow' }, h('b', {}, row.case_title), h('span', { class: 'sub mono' }, '　' + row.case_id),
    stateResults(row), row.state?.outcome !== 'pass' && h('span', { class: 'sub' }, row.state?.reason || '展开查看实际结果与未通过原因')),
  h('span', { class: 'sub' }, `${row.turn_count || 0} 轮 · ${fmtDuration(row.duration_ms)}`));
  if (expanded.get(key)) pendingDetails.set(body, load());
  return h('div', { class: 'result', dataset: { result: String(row.id) } }, button, body);
}

function evidencePanel(run) {
  const current = evidenceFiles.get(run.id);
  const entries = current?.entries;
  const content = h('div', { class: 'stack', style: 'gap:8px' },
    current?.error && h('p', { class: 'error', role: 'status' }, current.error + '；可以再次读取附件。'),
    entries && (entries.length ? entries.map((entry) => h('div', { class: 'stack', style: 'gap:2px' },
      h('a', { class: 'card-link', href: fileUrl(run.id, entry.path), target: '_blank', rel: 'noreferrer' }, entry.path),
      h('span', { class: 'sub mono' }, `${entry.size ?? '—'} 字节 · SHA-256 ${entry.sha256 || '未提供'}`))) : h('p', { class: 'sub' }, '尚无可下载的执行证据。')));
  const load = h('button', { class: 'btn', 'aria-label': '列出报告与证据附件', disabled: !run.report?.available || current?.loading, onClick: async () => {
    if (evidenceFiles.get(run.id)?.loading) return;
    const reading = { entries: evidenceFiles.get(run.id)?.entries, loading: true };
    evidenceFiles.set(run.id, reading);
    refresh();
    try {
      const outcome = await api(`/native-runs/${encodeURIComponent(run.id)}/files/outcome.json`);
      reading.entries = (outcome.evidence || []).filter((entry) => typeof entry.path === 'string');
    } catch (error) { reading.error = error.message; }
    finally { reading.loading = false; refresh(); }
  } }, current?.loading ? '正在读取附件…' : '列出报告与证据附件');
  return h('section', { class: 'card card-body stack' }, h('h2', {}, '原生报告与证据'),
    h('p', { class: 'sub' }, 'JTL、HTML、日志和对话证据按本次运行绑定；文件缺失或校验失败会明确报错。'), load, content);
}
