// Configure concurrent runs of existing cases, then inspect each retained execution and its metrics.
import { h, icon, replace, api, scope, scopeQuery, go, href, route, attempt, toast,
  fmtTime, fmtNumber, refresh, live } from '../core.js';
import { drawer, closeOverlay, empty, errorState } from '../ui.js';
import { updateLiveView } from '../live-refresh.js';
import { returnToRuns } from './run-actions.js';
import { reportControls, reportVerdict } from './report-controls.js';
import { downloadRunHtml } from './run-export.js';
import { renderLoadDashboard, loadDashboardScript } from './load-dashboard.js';

const PAGE = 50;
const active = (run) => ['Queued', 'Running'].includes(run.status);
const keyOf = (item) => `${item.project_id}/${item.case_id}`;
const MODES = { performance: '性能测试', stress: '压力测试' };
const modeOf = (mode) => mode === 'stress' ? 'stress' : 'performance';

function modeTabs(context, initial, change) {
  let current = initial;
  const tabs = Object.keys(MODES).map((mode) => h('button', { type: 'button', role: 'tab', id: context + '-' + mode,
    'aria-controls': context + '-panel', 'aria-selected': String(mode === current), tabIndex: mode === current ? 0 : -1,
    onClick: () => {
      if (mode === current) return;
      current = mode;
      for (const tab of tabs) {
        const selected = tab.id === context + '-' + mode;
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
      }
      change(mode);
    },
    onKeyDown: (event) => {
      const index = tabs.indexOf(event.currentTarget);
      const next = { ArrowLeft: 1 - index, ArrowRight: 1 - index, Home: 0, End: 1 }[event.key];
      if (next == null) return;
      event.preventDefault();
      tabs[next].focus();
      tabs[next].click();
    },
  }, MODES[mode]));
  return h('div', { class: 'seg load-mode-tabs', role: 'tablist', 'aria-label': '测试模式' }, tabs);
}

export function loadTestButton(label, getItems, { small = false } = {}) {
  const button = h('button', { class: 'btn' + (small ? ' sm' : ''), onClick: (event) => {
    event.stopPropagation();
    openLoadTest(getItems());
  } }, icon('sliders'), label);
  return button;
}

export async function openLoadTest(seeds = [], config = {}) {
  const projects = seeds.length ? [...new Set(seeds.map((item) => item.project_id))] : scope().projects;
  let options;
  try { options = await api('/load-tests/options' + (projects.length ? '?projects=' + encodeURIComponent(projects.join(',')) : '')); }
  catch (error) { toast(error.message, { bad: true }); return; }
  options = { ...options, items: options.items.filter((item) => item.kind === 'api') };
  const picked = new Set();
  for (const seed of seeds) {
    for (const item of options.items) {
      if (item.project_id === seed.project_id && (!seed.case_ids || seed.case_ids.includes(item.case_id))) picked.add(keyOf(item));
    }
  }
  const missing = seeds.flatMap((seed) => (seed.case_ids || []).filter((id) => !options.items.some((item) => item.project_id === seed.project_id && item.case_id === id)));
  loadEditor(options, picked, missing, config);
}

const PARAMETERS = {
  performance: [
    ['concurrency', '并发数', 3],
    ['duration_seconds', '持续时间（秒）', ''],
    ['target_requests', '目标请求数', ''],
  ],
  stress: [
    ['initial_concurrency', '初始并发数', 1],
    ['step_concurrency', '每阶段增加并发数', 1],
    ['max_concurrency', '最大并发数', 3],
    ['step_duration_seconds', '每阶段持续时间（秒）', 30],
  ],
};

function parameterFields(mode, config) {
  const fields = [];
  const inputs = {};
  for (const [key, label, fallback] of PARAMETERS[mode]) {
    const seconds = key.endsWith('_seconds');
    const input = h('input', { class: 'input', id: 'load-' + key, name: key, type: 'number',
      min: seconds ? 0 : 1, step: seconds ? 'any' : 1, required: fallback !== '',
      value: config[key] ?? fallback });
    fields.push(h('div', { class: 'field' }, h('label', { for: input.id }, label), input));
    inputs[key] = input;
  }
  const panel = h('div', { class: 'stack' }, h('div', { class: 'load-fields' }, fields),
    mode === 'performance' && h('p', { class: 'hint' }, '持续时间和目标请求数至少填写一项。同时填写时，先达到的条件结束测试。'));
  function read() {
    const values = {};
    for (const [key, label] of PARAMETERS[mode]) {
      if (!inputs[key].value) continue;
      const value = inputs[key].valueAsNumber;
      if (!(value > 0) || (!key.endsWith('_seconds') && !Number.isSafeInteger(value))) throw new Error(label + '必须为正' + (key.endsWith('_seconds') ? '数' : '整数'));
      values[key] = value;
    }
    if (mode === 'performance' && !values.duration_seconds && !values.target_requests) throw new Error('请填写持续时间或目标请求数');
    if (mode === 'stress' && values.max_concurrency < values.initial_concurrency) throw new Error('最大并发数不能小于初始并发数');
    return values;
  }
  return { panel, read };
}

function loadEditor(options, picked, missing, config) {
  let mode = modeOf(config.mode);
  const fields = Object.fromEntries(Object.keys(MODES).map((key) => [key, parameterFields(key, key === mode ? config : {})]));
  const panel = h('div', { role: 'tabpanel', id: 'load-editor-panel', 'aria-labelledby': 'load-editor-' + mode }, fields[mode].panel);
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const summary = h('p', { class: 'sub', role: 'status', 'aria-live': 'polite' });
  const start = h('button', { class: 'btn primary', type: 'submit', form: 'load-test-form', disabled: !options.items.length }, icon('play'), '开始压测');
  const update = () => {
    const count = new Set(options.items.filter((item) => picked.has(keyOf(item))).map((item) => item.project_id)).size;
    summary.textContent = `已选 ${picked.size} 条用例` + (count > 1 ? ' · 每个项目单独应用此设置' : '');
  };
  const tabs = modeTabs('load-editor', mode, (next) => {
    mode = next;
    panel.setAttribute('aria-labelledby', 'load-editor-' + mode);
    replace(panel, fields[mode].panel);
    error.hidden = true;
  });
  const cases = casePicker(options, picked, update);
  const form = h('form', { id: 'load-test-form', class: 'stack', onSubmit: async (event) => {
    event.preventDefault();
    if (start.disabled) return;
    error.hidden = true;
    try {
      const parameters = fields[mode].read();
      const items = selectedProjects(options.items.filter((item) => picked.has(keyOf(item))));
      const submittedMode = mode;
      start.disabled = true;
      const result = await api('/load-tests', { method: 'POST', body: {
        items, mode: submittedMode, ...parameters,
      } });
      if (!result.runs.length) throw new Error(result.skipped?.[0]?.reason || '没有启动压测，请检查所选用例');
      closeOverlay();
      live.until = Date.now() + 10000;
      if (result.skipped?.length) toast(`${result.skipped.length} 个项目未启动：${result.skipped[0].reason}`, { bad: true });
      if (result.runs.length === 1) go('load-tests/' + result.runs[0].id);
      else { toast(`已启动 ${result.runs.length} 个项目的测试`); go('load-tests', { mode: submittedMode }); }
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { start.disabled = !options.items.length; }
  } }, tabs, panel, h('div', { class: 'field' }, h('h3', {}, '选择接口用例'), cases, summary),
  missing.length > 0 && h('div', { class: 'banner', role: 'alert' }, icon('warn'), `以下用例不在可选接口范围内：${missing.join('、')}`), error);
  update();
  drawer({ title: '新建压测', body: form, wide: true,
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), start] });
}

function selectedProjects(chosen) {
  if (!chosen.length) throw new Error('请至少选择一条接口用例');
  const unready = chosen.filter((item) => !item.replayable);
  if (unready.length) throw new Error('以下用例缺少可回放步骤：' + unready.map((item) => item.case_id).join('、'));
  const groups = new Map();
  for (const item of chosen) {
    if (!groups.has(item.project_id)) groups.set(item.project_id, []);
    groups.get(item.project_id).push(item.case_id);
  }
  return [...groups].map(([project_id, case_ids]) => ({ project_id, case_ids }));
}

function casePicker(options, picked, update) {
  const box = h('div', { class: 'case-pick load-case-pick' });
  if (!options.items.length) return h('p', { class: 'sub' }, '当前项目没有可选的接口用例。');
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
      h('span', { class: 'sub mono' }, item.case_id)));
    }
  }
  return box;
}

export async function render(view, { id }) {
  if (id) await report(view, id);
  else await history(view);
}

export function commit(view, fresh, { quiet }) {
  if (quiet) return updateLiveView(view, fresh);
  const focus = document.activeElement;
  const keepFocus = view.contains(focus) && focus.matches('[role="tab"], [data-load-stage]');
  view.replaceChildren(...fresh.childNodes);
  if (keepFocus) document.getElementById(focus.id)?.focus({ preventScroll: true });
}

function loadConfig(config = {}) {
  if (modeOf(config.mode) === 'stress') {
    return [`并发 ${config.initial_concurrency} → ${config.max_concurrency}`, `每阶段增加 ${config.step_concurrency}`, `每阶段 ${config.step_duration_seconds} 秒`].join(' · ');
  }
  return [config.concurrency && `${config.concurrency} 并发`, config.duration_seconds && `${config.duration_seconds} 秒`,
    config.target_requests && `目标 ${fmtNumber(config.target_requests)} 次`].filter(Boolean).join(' · ');
}

async function history(view) {
  const mode = modeOf(route().params.get('mode'));
  const offset = Math.max(0, Number(route().params.get('offset')) || 0);
  let data;
  try { data = await api('/load-tests' + scopeQuery({ mode, limit: PAGE, offset, include_metrics: 'false' })); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  const start = () => openLoadTest([], { mode });
  const tabs = modeTabs('load-history', mode, (next) => go('load-tests', { mode: next }));
  const name = MODES[mode] + '记录';
  const rows = data.items.map((run) => {
    const link = href('load-tests/' + run.id, { offset: offset || null });
    return h('tr', {}, h('td', {}, reportVerdict(run)),
      h('td', {}, h('a', { class: 'title', href: link }, run.project_name), h('div', { class: 'sub mono' }, run.id)),
      h('td', {}, loadConfig(run.load)), h('td', { class: 'num' }, fmtTime(run.created_at)),
      h('td', { class: 'actions' }, h('a', { class: 'btn sm', href: link }, '查看结果')));
  });
  const body = rows.length ? h('div', { class: 'load-table-wrap' }, h('table', { class: 'table', 'aria-label': name },
    h('thead', {}, h('tr', {}, ['状态', '项目', '配置', '创建时间', ''].map((text) => h('th', {}, text)))), h('tbody', {}, rows)))
    : empty({ glyph: 'sliders', title: '还没有' + name });
  const page = (target) => go('load-tests', { mode, offset: target || null });
  replace(view, h('div', { class: 'page-head' }, h('h1', { class: 'grow' }, '压测记录'),
    h('button', { class: 'btn primary', onClick: start }, icon('plus'), '新建压测')),
  tabs, h('section', { class: 'card', role: 'tabpanel', id: 'load-history-panel', 'aria-labelledby': 'load-history-' + mode }, body,
    data.total > PAGE && h('div', { class: 'row pager' }, h('span', { class: 'sub num' }, `${offset + 1}–${Math.min(offset + PAGE, data.total)} / ${data.total}`),
      h('button', { class: 'btn sm', disabled: offset === 0, onClick: () => page(Math.max(0, offset - PAGE)) }, '上一页'),
      h('button', { class: 'btn sm', disabled: offset + PAGE >= data.total, onClick: () => page(offset + PAGE) }, '下一页'))));
}

function stageControl(run, current, change) {
  const stages = run.load_report?.stages || [];
  const selected = stages.some((entry) => String(entry.index) === current) ? current : '';
  const select = h('select', { class: 'select', id: 'load-report-stage', 'data-load-stage': '', onChange: (event) => change(event.currentTarget.value) },
    h('option', { value: '' }, '全部阶段'), stages.map((stage) =>
      h('option', { value: String(stage.index), selected: String(stage.index) === selected }, `第 ${stage.index} 阶段`)));
  select.value = selected;
  select.selectedOptions[0].defaultSelected = true;
  return { selected, control: h('div', { class: 'field load-stage-field' }, h('label', { for: select.id }, '阶段'), select) };
}

function reportBody(run, { offline = false, cancel } = {}) {
  const config = run.load || {};
  const mode = modeOf(config.mode);
  const params = route().params;
  const stage = mode === 'stress' ? stageControl(run, params.get('stage'), (value) => {
    go('load-tests/' + run.id, { ...Object.fromEntries(params), stage: value || null });
  }) : null;
  const restart = () => openLoadTest([{ project_id: run.project_id, case_ids: run.case_ids }], config);
  const project = (run.project_name || run.project_id).replace(/ · 浏览器模拟$/, '');
  const title = `${project} · ${MODES[mode]}`;
  const driver = config.driver === 'locust' || run.engine === 'locust' ? 'Locust' : '';
  const source = run.simulated || run.load_report?.simulated ? '浏览器模拟' : driver;
  const charts = h('div', { 'data-load-dashboard': '' });
  if (!offline) charts.append(renderLoadDashboard(run, { stage: stage?.selected }));
  return h(offline ? 'main' : 'div', { class: 'stack report-document load-report' },
    h('div', { class: 'page-head' }, h('div', { class: 'grow report-heading' }, h('h1', {}, title),
      h('div', { class: 'row wrap' }, reportVerdict(run),
        h('p', { class: 'sub', title: run.load_report?.clock }, [loadConfig(config), fmtTime(run.created_at), source].filter(Boolean).join(' · ')))),
    !offline && (active(run) ? cancel : h('button', { class: 'btn', onClick: restart }, icon('reset'), '再次压测')),
    !offline && reportControls(() => downloadRunHtml(run, reportBody(run, { offline: true }), {
      loadDashboard: { script: loadDashboardScript(), data: { load: { mode }, load_report: run.load_report } },
    }))),
    run.reason && h('p', { class: 'banner', role: 'status' }, run.reason),
    run.done > 0 && !run.load_report?.samples?.length && h('p', { class: 'sub' },
      `已完成 ${fmtNumber(run.done)} 条执行记录，本次未留存可用于统计的目标样本。`),
    stage?.control, charts);
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
  const params = route().params;
  const fromHistory = /^#\/runs(?:\?|$)/.test(params.get('return') || '');
  const back = fromHistory ? returnToRuns() : href('load-tests', { mode: modeOf(run.load?.mode), offset: params.get('offset') || null });
  replace(view, h('a', { class: 'back', href: back }, icon('right'), fromHistory ? '返回执行记录' : '压测记录'), reportBody(run, { cancel }));
}
