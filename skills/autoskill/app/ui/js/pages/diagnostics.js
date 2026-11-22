// Run actual local diagnostics and show unavailable dependencies without treating them as successful tests.
import { h, icon, replace, api, fmtTime } from '../core.js';
import { errorState } from '../ui.js';

let generation = 0;
const show = (value) => value == null || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
const flag = (ok, yes = '可用', no = '不可用') => h('span', { class: 'status ' + (ok ? 'pass' : 'blocked') }, icon(ok ? 'check' : 'warn'), ok ? yes : no);

function properties(values) {
  return h('dl', { class: 'stack', style: 'gap:8px' }, Object.entries(values).map(([name, value]) => h('div', { class: 'field', style: 'gap:4px' },
    h('dt', { class: 'sub' }, name), h('dd', { class: 'mono', style: 'margin:0;overflow-wrap:anywhere' }, show(value)))));
}

function doctorResult(report) {
  return h('div', { class: 'stack', role: 'status' },
    h('div', { class: 'row wrap' }, flag(report.ok, '运行环境完整', '运行环境缺少组件'), h('span', { class: 'sub' }, report.bundled_runtime ? '随包运行时' : '开发环境')),
    report.missing?.length > 0 && h('div', { class: 'banner', role: 'alert' }, icon('warn'), '缺少或检查未通过：' + report.missing.join('、')),
    properties({ Python: report.python, 平台: report.platform, 浏览器: report.chrome, 浏览器驱动: report.chromedriver,
      页面录制器: report.agent_browser, 数据目录: report.workspace }),
    h('table', { class: 'table', 'aria-label': 'Python 依赖状态' },
      h('thead', {}, h('tr', {}, h('th', {}, '依赖'), h('th', {}, '状态'))),
      h('tbody', {}, Object.entries(report.modules || {}).map(([name, ok]) => h('tr', {}, h('td', { class: 'mono' }, name), h('td', {}, flag(ok)))))),
    report.browser_launch && h('div', { class: 'stack' }, h('h3', {}, '真实浏览器启动结果'), properties(report.browser_launch)),
    report.browser_versions && h('details', { class: 'more' }, h('summary', {}, '浏览器版本匹配'), h('pre', { class: 'code' }, show(report.browser_versions))),
    report.hint && h('p', { class: 'hint' }, report.hint));
}

function verifyResult(report) {
  return h('div', { class: 'stack', role: 'status' }, flag(report.ok, '随包文件校验通过', '随包文件校验未通过'),
    h('p', { class: 'sub' }, `已按清单检查 ${report.files || 0} 个文件的 SHA-256。`),
    report.missing?.length > 0 && h('div', {}, h('h3', {}, '缺少文件'), h('pre', { class: 'code' }, report.missing.join('\n'))),
    report.changed?.length > 0 && h('div', {}, h('h3', {}, '内容发生变化'), h('pre', { class: 'code' }, report.changed.join('\n'))));
}

function engineResult(report, engines) {
  const names = new Map(engines.map((engine) => [engine.id, engine.name || engine.title || engine.id]));
  return h('div', { style: 'overflow:auto', role: 'status' }, h('table', { class: 'table', 'aria-label': '执行器检查结果' },
    h('thead', {}, h('tr', {}, ['执行器', '状态', '实际版本与路径', '诊断'].map((name) => h('th', {}, name)))),
    h('tbody', {}, (report.engines || []).map((engine) => h('tr', {},
      h('td', {}, names.get(engine.id) || engine.id, h('div', { class: 'sub mono' }, engine.id)),
      h('td', {}, flag(engine.available)),
      h('td', {}, h('pre', { class: 'code' }, show(engine.version)), engine.command && h('div', { class: 'sub mono' }, engine.command.join(' '))),
      h('td', { class: 'sub' }, engine.diagnosis || (!engine.available && '执行器未安装或未通过诊断')))))));
}

async function completedTask(initial, mine) {
  let task = initial;
  while (task.status === 'Running') {
    await new Promise((resolve) => setTimeout(resolve, 400));
    if (mine !== generation) return null;
    task = await api('/diagnostics/' + encodeURIComponent(task.id));
  }
  if (task.status !== 'Completed') throw new Error(task.error || '检查没有完成，请重试');
  return task;
}

function checkCard({ title, note, busy, fields = [], actions, renderResult, mine }) {
  const result = h('div', { class: 'stack', 'aria-live': 'polite' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const progress = h('p', { class: 'sub', role: 'status', hidden: true });
  const buttons = actions.map(({ label, specification }) => h('button', { class: 'btn', onClick: async () => {
    for (const button of buttons) button.disabled = true;
    error.hidden = true;
    progress.hidden = false;
    progress.textContent = busy;
    try {
      const started = await api('/diagnostics', { method: 'POST', body: specification() });
      const task = await completedTask(started, mine);
      if (!task) return;
      replace(result, h('p', { class: 'sub' }, '检查时间：' + fmtTime(task.created_at)), renderResult(task.result));
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally {
      progress.hidden = true;
      for (const button of buttons) button.disabled = false;
    }
  } }, icon('play'), label));
  return h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, title), h('p', { class: 'sub' }, note))),
    h('div', { class: 'card-body stack' }, fields, h('div', { class: 'row wrap' }, buttons), progress, error, result));
}

export async function render(view) {
  const mine = ++generation;
  let engines;
  try { engines = (await api('/engines')).engines; }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, () => render(view)))); return; }
  const choice = h('select', { class: 'select', id: 'diagnostic-engine' }, h('option', { value: '' }, '全部执行器'),
    engines.map((engine) => h('option', { value: engine.id }, engine.name || engine.title || engine.id)));
  replace(view, h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '运行诊断'))),
  checkCard({ title: '运行环境', busy: '正在检查运行环境…', note: '检查 Python 依赖、浏览器、驱动与录制器；启动检查会实际打开浏览器完成本机探测。', mine,
    actions: [{ label: '检查运行环境', specification: () => ({ kind: 'doctor' }) },
      { label: '检查并启动浏览器', specification: () => ({ kind: 'doctor', launch: true }) }], renderResult: doctorResult }),
  checkCard({ title: '原生执行器', busy: '正在探测执行器…', note: '探测实际安装和版本。导入资产之前，可先确认 Newman、Apifox 或 Python 执行器是否可用。', mine,
    fields: h('div', { class: 'field' }, h('label', { for: 'diagnostic-engine' }, '选择执行器'), choice),
    actions: [{ label: '检查执行器', specification: () => ({ kind: 'engines', ...(choice.value ? { engine: choice.value } : {}) }) }],
    renderResult: (report) => engineResult(report, engines) }),
  checkCard({ title: '离线包文件', busy: '正在校验离线包文件…', note: '按运行时清单校验文件内容。此项需要完整离线包，源码环境缺少清单时会显示原因。', mine,
    actions: [{ label: '校验离线包文件', specification: () => ({ kind: 'verify' }) }], renderResult: verifyResult }));
}
