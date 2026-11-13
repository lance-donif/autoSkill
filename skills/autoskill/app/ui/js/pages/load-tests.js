// Configure concurrent runs of existing cases, then inspect each retained execution and its metrics.
import { h, icon, replace, api, scope, scopeQuery, go, href, route, statusPill, attempt, toast,
  fmtTime, fmtDuration, fmtNumber, fmtPercent, refresh, live } from '../core.js';
import { drawer, closeOverlay, empty, errorState, definition, preferredEngine, preferredHeaded } from '../ui.js';
import { downloadRunHtml } from './run-export.js';

const PAGE = 50;
const active = (run) => ['Queued', 'Running'].includes(run.status);
const keyOf = (item) => `${item.project_id}/${item.case_id}`;
const number = (value, digits = 2) => value == null ? '—' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits });

export function loadTestButton(label, getItems, { small = false } = {}) {
  const button = h('button', { class: 'btn' + (small ? ' sm' : ''), onClick: (event) => {
    event.stopPropagation();
    openLoadTest(getItems());
  } }, icon('sliders'), label);
  return button;
}

export async function openLoadTest(seeds = []) {
  const projects = seeds.length ? [...new Set(seeds.map((item) => item.project_id))] : scope().projects;
  let options;
  try { options = await api('/load-tests/options' + (projects.length ? '?projects=' + encodeURIComponent(projects.join(',')) : '')); }
  catch (error) { toast(error.message, { bad: true }); return; }
  const picked = new Set();
  for (const seed of seeds) {
    for (const item of options.items) {
      if (item.project_id === seed.project_id && (!seed.case_ids || seed.case_ids.includes(item.case_id))) picked.add(keyOf(item));
    }
  }
  const missing = seeds.flatMap((seed) => (seed.case_ids || []).filter((id) => !options.items.some((item) => item.project_id === seed.project_id && item.case_id === id)));
  loadEditor(options, picked, missing);
}

function numericField(id, label, value, min, step, note) {
  const input = h('input', { class: 'input', type: 'number', id, value, min, step, required: true });
  return { input, field: h('div', { class: 'field' }, h('label', { for: id }, label), input, h('span', { class: 'hint' }, note)) };
}

function loadEditor(options, picked, missing) {
  const concurrency = numericField('lt-concurrency', '并发用户数', 3, 1, 1, '最多同时执行的独立用户数。');
  const iterations = numericField('lt-iterations', '迭代次数', 10, 1, 1, '每次迭代完整执行所选用例及其前置；这是总迭代次数。');
  const ramp = numericField('lt-ramp', '渐增时间（秒）', 0, 0, 'any', '0 表示立即启动全部并发用户。');
  const summary = h('p', { class: 'sub', role: 'status', 'aria-live': 'polite' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const start = h('button', { class: 'btn primary', type: 'submit', form: 'load-test-form', disabled: !options.items.length }, icon('play'), '开始压测');
  const update = () => {
    summary.textContent = `已选 ${picked.size} 条用例 · 计划执行 ${fmtNumber(picked.size * Math.max(0, Number(iterations.input.value) || 0))} 条次（前置另行执行）`;
  };
  iterations.input.addEventListener('input', update);
  const cases = casePicker(options, picked, update);
  const form = h('form', { id: 'load-test-form', class: 'stack', onSubmit: async (event) => {
    event.preventDefault();
    error.hidden = true;
    const chosen = options.items.filter((item) => picked.has(keyOf(item)));
    if (!chosen.length) { error.textContent = '请至少选择一条用例'; error.hidden = false; return; }
    const unready = chosen.filter((item) => !item.replayable);
    if (unready.length) { error.textContent = '以下用例还没有可回放步骤，请先补全或取消选择：' + unready.map((item) => item.case_id).join('、'); error.hidden = false; return; }
    const groups = new Map();
    for (const item of chosen) groups.set(item.project_id, [...(groups.get(item.project_id) || []), item.case_id]);
    start.disabled = true;
    try {
      const result = await api('/load-tests', { method: 'POST', body: {
        items: [...groups].map(([project_id, case_ids]) => ({ project_id, case_ids })),
        concurrency: Number(concurrency.input.value), iterations: Number(iterations.input.value), ramp_up_seconds: Number(ramp.input.value),
        engine: preferredEngine(), headed: preferredHeaded(),
      } });
      if (!result.runs.length) throw new Error(result.skipped?.[0]?.reason || '没有启动压测，请检查所选用例');
      closeOverlay();
      live.until = Date.now() + 10000;
      if (result.skipped?.length) toast(`${result.skipped.length} 个项目未启动：${result.skipped[0].reason}`, { bad: true });
      if (result.runs.length === 1) go('load-tests/' + result.runs[0].id);
      else { toast(`已启动 ${result.runs.length} 个项目的压测`); go('load-tests'); }
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { start.disabled = !options.items.length; }
  } },
  h('div', { class: 'load-fields' }, concurrency.field, iterations.field, ramp.field),
  h('div', { class: 'field' }, h('h3', {}, '选择已有用例'),
    h('p', { class: 'hint' }, '页面与接口用例均可选择；每次迭代使用独立的浏览器、Cookie 和变量，按依赖顺序执行。'), cases, summary),
  h('p', { class: 'hint' }, '页面用例沿用顶部的回放设置。压测保留原断言和每次执行结果。'),
  missing.length > 0 && h('div', { class: 'banner', role: 'alert' }, icon('warn'), `以下用例已不存在：${missing.join('、')}。请核对当前选择。`), error);
  update();
  drawer({ title: '新建压测', subtitle: '复用已有用例及断言，设置并发负载。', body: form, wide: true,
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), start] });
}

function casePicker(options, picked, update) {
  const box = h('div', { class: 'case-pick load-case-pick' });
  if (!options.items.length) return h('p', { class: 'sub' }, '还没有用例，请先到「项目与用例」录制或导入。');
  for (const project of options.projects) {
    const items = options.items.filter((item) => item.project_id === project.id);
    if (!items.length) continue;
    box.append(h('h4', {}, project.name));
    for (const item of items) {
      const input = h('input', { class: 'check', type: 'checkbox', checked: picked.has(keyOf(item)), onChange: () => {
        if (input.checked) picked.add(keyOf(item)); else picked.delete(keyOf(item));
        update();
      } });
      box.append(h('label', { class: 'row' }, input, h('span', { class: 'grow' }, item.title,
        !item.replayable && h('span', { class: 'sub' }, ' · 缺少可回放步骤')),
      h('span', { class: 'tag' }, item.kind === 'ui' ? '页面' : '接口'), h('span', { class: 'sub mono' }, item.case_id)));
    }
  }
  return box;
}

export async function render(view, { id }) {
  if (id) await report(view, id);
  else await history(view);
}

async function history(view) {
  const offset = Number(route().params.get('offset') || 0);
  let data;
  try { data = await api('/load-tests' + scopeQuery({ limit: PAGE, offset })); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  const start = () => openLoadTest();
  const body = data.items.length ? h('div', { class: 'load-table-wrap' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['状态', '项目', '负载', '进度', '结果', '创建时间', ''].map((text) => h('th', {}, text)))),
    h('tbody', {}, data.items.map((run) => h('tr', {},
      h('td', {}, statusPill(run.status)),
      h('td', {}, h('a', { class: 'title', href: href('load-tests/' + run.id) }, run.project_name), h('div', { class: 'sub mono' }, run.id)),
      h('td', { class: 'sub' }, `${run.load.concurrency} 并发 · ${run.load.iterations} 次迭代`),
      h('td', { class: 'num' }, `${run.done} / ${run.total}`),
      h('td', { class: 'sub' }, `${run.passed} 通过 · ${run.failed} 失败 · ${run.blocked} 阻塞`),
      h('td', { class: 'sub num' }, fmtTime(run.created_at)),
      h('td', { class: 'actions' }, h('a', { class: 'btn sm', href: href('load-tests/' + run.id) }, '查看结果')))))))
    : empty({ glyph: 'sliders', title: '还没有压测记录', text: '选择已有的页面或接口用例，配置并发数和迭代次数后开始压测。',
      actions: h('button', { class: 'btn primary', onClick: start }, icon('plus'), '新建压测') });
  replace(view, h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '压测'),
    h('p', { class: 'sub' }, '复用已有用例，观察并发负载下的业务结果和执行耗时。')),
  h('button', { class: 'btn primary', onClick: start }, icon('plus'), '新建压测')),
  h('section', { class: 'card' }, body, historyPager(data.total, offset)));
}

function historyPager(total, offset) {
  if (total <= PAGE) return null;
  const change = (next) => go('load-tests', { offset: next || null });
  return h('div', { class: 'card-body row', style: 'justify-content:flex-end' },
    h('span', { class: 'sub num' }, `${offset + 1}–${Math.min(offset + PAGE, total)} / ${total}`),
    h('button', { class: 'btn sm', disabled: offset === 0, onClick: () => change(Math.max(0, offset - PAGE)) }, '上一页'),
    h('button', { class: 'btn sm', disabled: offset + PAGE >= total, onClick: () => change(offset + PAGE) }, '下一页'));
}

async function report(view, id) {
  let run;
  try { run = await api('/load-tests/' + encodeURIComponent(id)); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  if (active(run)) live.until = Date.now() + 8000;
  const cancel = h('button', { class: 'btn danger', disabled: !!run.cancel, onClick: async () => {
    if (await attempt(() => api(`/load-tests/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {} }), cancel)) {
      live.until = Date.now() + 8000;
      refresh();
    }
  } }, icon('stop'), run.cancel ? '正在停止' : '停止压测');
  const download = h('button', { class: 'btn', onClick: () => attempt(() => downloadRunHtml(run,
    h('main', { class: 'stack' }, reportSummary(run), metricCards(run), resultTable(run))), download) }, icon('table'), '导出报告');
  replace(view, h('a', { class: 'back', href: href('load-tests') }, icon('right'), '压测记录'),
    reportSummary(run, active(run) ? cancel : loadTestButton('再次压测', () => [{ project_id: run.project_id, case_ids: run.case_ids }]), download),
    taskReportLinks(run), metricCards(run), resultTable(run));
}

function taskReportLinks(run) {
  const receipt = run.test_report;
  if (receipt?.status === 'ready') return h('div', { class: 'row wrap' },
    h('a', { class: 'btn', href: receipt.download_url, download: '' }, icon('table'), '下载本次用例 Excel'),
    h('a', { class: 'btn', href: receipt.markdown_download_url, download: '' }, '下载 Markdown 汇总'),
    h('a', { class: 'btn', href: receipt.bugs_download_url, download: '' }, '下载 Bug 清单'));
  if (receipt?.status !== 'failed') return null;
  const retry = h('button', { class: 'btn', onClick: async () => {
    const result = await attempt(() => api(receipt.retry_url.replace(/^\/api/, ''), { method: 'POST' }), retry);
    if (result) { toast(result.status === 'ready' ? '报告已生成' : result.error || '报告尚未生成', { bad: result.status === 'failed' }); refresh(); }
  } }, icon('reset'), '重试导出报告');
  return h('div', { class: 'banner', role: 'status' }, icon('warn'), h('span', { class: 'grow' }, receipt.error || '报告导出未完成'), retry);
}

function reportSummary(run, action, download) {
  const config = run.load;
  return h('section', { class: 'card' }, h('div', { class: 'card-body' },
    h('div', { class: 'row wrap' }, statusPill(run.status), h('div', { class: 'grow report-heading' }, h('h1', {}, `${run.project_name} · 压测`),
      h('p', { class: 'sub' }, `${config.concurrency} 并发 · ${config.iterations} 次迭代 · 渐增 ${config.ramp_up_seconds} 秒 · ${fmtTime(run.created_at)}`)), download, action),
    h('div', { class: 'stack', style: 'gap:8px;margin-top:16px' },
      h('div', { class: 'progress', role: 'progressbar', 'aria-label': '压测进度', 'aria-valuemin': 0, 'aria-valuemax': run.total, 'aria-valuenow': run.done },
        h('i', { style: `width:${run.total ? Math.min(100, run.done / run.total * 100) : 0}%` })),
      h('div', { class: 'row sub' }, h('span', { class: 'grow' }, active(run) ? (run.current || '准备执行') : '执行结束'),
        h('span', { class: 'num' }, `${run.done} / ${run.total} 条次`))),
    h('div', { class: 'stats' }, stat('执行通过', run.passed, 'pass'), stat('失败', run.failed, 'fail'), stat('阻塞', run.blocked, 'blocked'),
      stat('尚未执行', Math.max(0, run.total - run.done))),
    run.reason && h('div', { class: 'banner bad', style: 'margin-top:16px', role: 'alert' }, icon('warn'), run.reason)));
}

function stat(label, value, tone) {
  return h('div', { class: 'stat' + (tone ? ' ' + tone : '') }, h('span', { class: 'stat-value num' }, String(value)), h('span', { class: 'stat-label' }, label));
}

function metricCards(run) {
  const m = run.metrics;
  const timing = (key) => m.latency_samples ? number(m[key]) + ' ms' : '—';
  const metric = (label, value, note) => h('div', { class: 'card load-metric' }, h('div', { class: 'kpi-label' }, label, definition(note)), h('div', { class: 'load-metric-value num' }, value));
  return h('section', { class: 'stack', 'aria-label': '压测指标' }, h('div', { class: 'load-metrics' },
    metric('用例吞吐量', run.done ? number(m.throughput_per_second) + ' 用例/秒' : '—', '已完成目标用例条次 ÷ 实际墙钟耗时，包含失败与阻塞，不含前置；这是用例吞吐量，不是 HTTP QPS。'),
    metric('失败与阻塞率', run.done ? fmtPercent(m.error_rate) : '—', '（失败条次 + 阻塞条次）÷ 已完成目标用例条次；尚未执行的数量单独展示。'),
    metric('P50', timing('p50_ms'), '真正执行的目标用例耗时第 50 百分位；前置阻塞等未发起的目标不计入延迟样本。'),
    metric('P95', timing('p95_ms'), '真正执行的目标用例耗时第 95 百分位；页面用例包含整个页面场景耗时。'),
    metric('P99', timing('p99_ms'), '真正执行的目标用例耗时第 99 百分位，单位为毫秒。')),
  h('p', { class: 'sub' }, `延迟样本 ${fmtNumber(m.latency_samples || 0)} 条 · 接口请求发送 ${fmtNumber(m.request_count || 0)} 次（含前置，不统计页面内部请求）。执行通过仍需结合业务断言判断。`));
}

function resultTable(run) {
  const rows = run.results;
  return h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, '逐次执行结果'),
    h('p', { class: 'sub' }, '每个用户、每次迭代的结果分别保存；前置不计目标条次、用例吞吐与延迟，接口请求数包含前置。'))),
  rows.length ? h('div', { class: 'load-table-wrap' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['结果', '用例', '用户 / 迭代', '耗时', '详情'].map((text) => h('th', {}, text)))),
    h('tbody', {}, rows.map((row) => h('tr', {},
      h('td', {}, statusPill(row.status)),
      h('td', {}, h('div', { class: 'title' }, row.case_title, row.prerequisite && h('span', { class: 'tag' }, '前置')),
        h('div', { class: 'sub mono' }, row.case_id)),
      h('td', { class: 'num' }, `${row.detail.load_user} / ${row.detail.load_iteration}`),
      h('td', { class: 'num' }, fmtDuration(row.duration_ms)),
      h('td', {}, resultDetails(row)))))))
    : empty({ glyph: 'clock', title: active(run) ? '等待执行结果' : '没有执行结果', text: run.reason || '用例执行完成后，结果会显示在这里。' }));
}

function resultDetails(row) {
  const assertions = row.detail.assertions || [];
  const show = (value) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  return h('details', { class: 'more' }, h('summary', {}, row.reason || '查看断言与业务结论'),
    h('p', { class: 'sub' }, '业务结论：' + ({ Pass: '通过', Fail: '失败', Unverified: '尚未验证' }[row.detail.business_status] || '尚未验证')),
    assertions.length ? h('ul', {}, assertions.map((check) => h('li', {},
      `${check.passed ? '通过' : '失败'} · ${check.name || check.label} · 期望 ${show(check.expected)} · 实际 ${show(check.actual)}`)))
      : h('p', { class: 'sub' }, '本次没有业务断言结果。'));
}
