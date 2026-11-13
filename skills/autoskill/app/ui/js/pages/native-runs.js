// Native reports keep business quality, performance and each conversation turn visible.
import { h, icon, replace, api, go, href, route, live, refresh, statusPill, attempt, fmtTime, fmtDuration, fmtPercent, fmtNumber } from '../core.js';
import { empty, errorState } from '../ui.js';

const expanded = new Map();
const show = (value) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
const quality = (value) => ({ Pass: '业务通过', Fail: '业务失败', Unverified: '业务未验证' })[value] || '业务未验证';
const names = {
  planned_cases: '计划对话数', completed_cases: '完成对话数', quality_passed: '质量通过数', quality_pass_rate: '质量通过率',
  failed_cases: '失败对话数', blocked_cases: '阻塞对话数', cancelled_cases: '取消对话数', sent_turns: '已发送轮次',
  concurrency: '并发会话数', elapsed_ms: '总耗时（ms）', throughput_turns_per_second: '吞吐（轮/秒）',
  p50_ms: 'P50 耗时（ms）', p95_ms: 'P95 耗时（ms）', p99_ms: 'P99 耗时（ms）', samples: '采样数', failed_samples: '失败采样数',
  elapsed_seconds: '总耗时（秒）', throughput_per_second: '吞吐（采样/秒）', max_threads: '最大线程数', error_rate: '错误率',
};
const fileUrl = (id, path) => `/api/native-runs/${encodeURIComponent(id)}/files/${String(path).split('/').map(encodeURIComponent).join('/')}`;

function runIdentity(run) {
  return `${run.project_name || run.project_id} · ${run.engine} · ${fmtTime(run.created_at)}`;
}

function outcomeReason(run) {
  const reason = run.reason || run.outcome?.reason;
  return reason && h('div', { class: 'banner bad' }, icon('warn'), reason);
}

export async function render(view, { id }) {
  if (!id) { go('runs'); return; }
  const cursor = route().params.get('cursor');
  let run;
  try { run = await api(`/native-runs/${encodeURIComponent(id)}?limit=50${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`); }
  catch (error) { replace(view, errorState(error, refresh)); return; }
  const running = ['Queued', 'Running'].includes(run.status);
  if (running) live.until = Date.now() + 8000;
  const outcome = run.outcome || {};
  const stop = h('button', { class: 'btn danger', onClick: async () => {
    if (await attempt(() => api(`/native-runs/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {} }), stop)) refresh();
  } }, icon('stop'), '停止运行');
  const again = h('button', { class: 'btn primary', onClick: async () => {
    const next = await attempt(() => api(`/native-runs/${encodeURIComponent(id)}/rerun`, { method: 'POST', body: {} }), again);
    if (next) go('native-runs/' + next.id, { cursor: null });
  } }, icon('play'), '按原版本再次运行');
  const rows = run.results || [];
  const focus = route().params.get('case');
  for (const row of rows) if (focus === row.case_id) expanded.set(id + '/' + row.id, true);
  replace(view,
    h('a', { class: 'back', href: href('runs') }, icon('right'), '执行记录'),
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '原生执行报告'),
      h('p', { class: 'sub' }, runIdentity(run))),
    running ? stop : again),
    h('section', { class: 'card card-body stack' },
      h('div', { class: 'row wrap' }, statusPill(run.status), h('span', { class: 'tag' }, quality(outcome.business_status)),
        h('span', { class: 'sub' }, `${run.done || 0} / ${run.planned_unknown ? '目标数待执行器发现' : run.total} 已记录`)),
      h('p', { class: 'sub mono' }, `${run.id} · 资产 ${run.asset_id} · 版本 ${run.asset_version}`),
      outcomeReason(run),
      h('p', { class: 'sub' }, '执行状态、业务质量和性能目标分别判定。后续通过不会改写已有失败；尚未完成的目标不计为通过。'),
      metricsTable(outcome.metrics || {}), reportLinks(run)),
    h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('h2', {}, '用例与多轮结果'),
      h('p', { class: 'sub' }, '展开用例核对每轮问题、实际回答、独立断言、宿主判定和耗时。'))),
    rows.length ? h('div', { class: 'result-list' }, rows.map((row) => caseRow(run, row)))
      : empty({ glyph: running ? 'clock' : 'runs', title: running ? '等待原生执行器产生结果' : '没有用例结果', text: outcome.reason }),
    run.page?.has_more && h('div', { class: 'card-body row' }, h('button', { class: 'btn', onClick: () => go('native-runs/' + id, { cursor: run.page.next_cursor }) }, '下一页')),
    cursor && h('div', { class: 'card-body' }, h('button', { class: 'btn', onClick: () => go('native-runs/' + id, { cursor: null }) }, '回到第一页'))),
    evidencePanel(run));
}

function metricsTable(metrics) {
  const entries = Object.entries(metrics).filter(([, value]) => value == null || typeof value !== 'object');
  if (!entries.length) return h('p', { class: 'sub' }, '当前执行器尚未提供性能统计。');
  return h('table', { class: 'table' }, h('thead', {}, h('tr', {}, h('th', {}, '指标'), h('th', {}, '实际值'))),
    h('tbody', {}, entries.map(([key, value]) => h('tr', {}, h('td', {}, names[key] || key),
      h('td', { class: 'num' }, key.endsWith('_rate') ? fmtPercent(value) : typeof value === 'number' ? fmtNumber(Math.round(value * 1000) / 1000) : show(value))))));
}

function reportLinks(run) {
  const report = run.test_report;
  return h('div', { class: 'row wrap' },
    run.report?.available && h('a', { class: 'btn', href: fileUrl(run.id, 'outcome.json'), download: '' }, icon('table'), '下载原生汇总 JSON'),
    report?.status === 'ready' && [h('a', { class: 'btn', href: report.download_url, download: '' }, 'Excel 用例报告'),
      h('a', { class: 'btn', href: report.markdown_download_url, download: '' }, '测试简报'),
      h('a', { class: 'btn', href: report.bugs_download_url, download: '' }, 'Bug 清单')],
    report?.status === 'failed' && h('span', { class: 'error' }, report.error || report.reason || '用例报告生成失败'));
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
    h('div', { class: 'row wrap' }, h('h3', {}, `第 ${turn.turn} 轮`), statusPill(turn.status), h('span', { class: 'tag' }, quality(turn.business_status))),
    h('div', {}, h('div', { class: 'sub' }, '问题'), h('pre', { class: 'code' }, show(turn.question))),
    h('div', {}, h('div', { class: 'sub' }, '实际回答'), h('pre', { class: 'code' }, show(turn.answer))),
    turn.reason && h('p', { class: 'error' }, turn.reason),
    checksTable(turn.assertions, '业务断言'), checksTable(turn.performance_assertions, '性能目标'),
    turn.timings && h('details', { class: 'more' }, h('summary', {}, '响应与流式耗时'), metricsTable(turn.timings)),
    turn.judgement && h('details', { class: 'more', open: turn.business_status !== 'Pass' }, h('summary', {}, '宿主判定与事实依据'),
      h('pre', { class: 'code' }, show(turn.judgement))));
}

function caseRow(run, row) {
  const key = run.id + '/' + row.id;
  const body = h('div', { class: 'result-body', hidden: !expanded.get(key) });
  let loaded = false;
  async function load() {
    replace(body, h('p', { class: 'sub' }, '正在读取完整结果…'));
    try {
      const data = await api(`/native-runs/${encodeURIComponent(run.id)}?case=${encodeURIComponent('result:' + row.id)}`);
      const detail = data.case;
      replace(body, h('div', { class: 'row wrap' }, h('span', { class: 'tag' }, quality(detail.business_status)), h('span', { class: 'sub' }, fmtDuration(row.duration_ms))),
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
  } }, icon('right', 'icon-svg caret'), statusPill(row.status),
  h('span', { class: 'grow' }, h('b', {}, row.case_title), h('span', { class: 'sub mono' }, '　' + row.case_id)),
  h('span', { class: 'sub' }, `${row.turn_count || 0} 轮 · ${fmtDuration(row.duration_ms)}`));
  if (expanded.get(key)) load();
  return h('div', { class: 'result' }, button, body);
}

function evidencePanel(run) {
  const content = h('div', { class: 'stack', style: 'gap:8px' });
  const load = h('button', { class: 'btn', disabled: !run.report?.available, onClick: async () => {
    const outcome = await attempt(() => api(`/native-runs/${encodeURIComponent(run.id)}/files/outcome.json`), load);
    if (!outcome) return;
    const entries = (outcome.evidence || []).filter((entry) => typeof entry.path === 'string');
    replace(content, entries.length ? entries.map((entry) => h('div', { class: 'stack', style: 'gap:2px' },
      h('a', { class: 'card-link', href: fileUrl(run.id, entry.path), target: '_blank', rel: 'noreferrer' }, entry.path),
      h('span', { class: 'sub mono' }, `${entry.size ?? '—'} 字节 · SHA-256 ${entry.sha256 || '未提供'}`))) : h('p', { class: 'sub' }, '尚无可下载的执行证据。'));
  } }, '列出报告与证据附件');
  return h('section', { class: 'card card-body stack' }, h('h2', {}, '原生报告与证据'),
    h('p', { class: 'sub' }, 'JTL、HTML、日志和对话证据按本次运行绑定；文件缺失或校验失败会明确报错。'), load, content);
}
