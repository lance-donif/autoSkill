// Import and run preserved Python, collection, JMeter and conversation assets.
import { h, icon, replace, api, route, scope, go, href, refresh, toast } from '../core.js';
import { empty, errorState, drawer, closeOverlay } from '../ui.js';

const field = (label, control, note) => h('div', { class: 'field' }, h('label', { for: control.id }, label), control, note && h('span', { class: 'hint' }, note));
const projectOptions = (projects, value) => projects.map((project) => h('option', { value: project.id, selected: project.id === value }, project.name));

export async function render(view, { id }) {
  let projects, engines;
  try { [projects, engines] = await Promise.all([api('/projects'), api('/engines')]); }
  catch (error) { replace(view, errorState(error, refresh)); return; }
  const projectId = route().params.get('project') || scope().projects[0] || projects[0]?.id;
  const selected = projects.find((project) => project.id === projectId);
  const create = h('button', { class: 'btn primary', disabled: !projects.length, onClick: () => importer(projects, engines.engines, projectId) }, icon('plus'), '导入原生资产');
  const select = h('select', { class: 'select', 'aria-label': '原生资产项目', onChange: () => go('native-assets', { project: select.value, cursor: null }) }, projectOptions(projects, projectId));
  replace(view, h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '原生资产'),
    h('p', { class: 'sub' }, '保留已有脚本、集合、负载计划和多轮对话，使用原执行器运行。')), create),
  projects.length ? h('div', { class: 'toolbar' }, h('label', {}, '项目'), select,
    h('a', { class: 'btn', href: href('diagnostics') }, '检查执行器'), h('a', { class: 'btn', href: href('runs') }, '执行记录')) : null);
  if (!selected) { view.append(h('section', { class: 'card' }, empty({ title: '先创建项目', text: '在项目与用例中创建站点项目，再导入原生资产。', actions: h('a', { class: 'btn', href: href('cases', { new: 1 }) }, '创建项目') }))); return; }
  if (id) { await detail(view, projectId, id); return; }
  let data;
  const cursor = route().params.get('cursor');
  try { data = await api(`/assets/${encodeURIComponent(projectId)}?limit=50${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`); }
  catch (error) { view.append(errorState(error, refresh)); return; }
  view.append(h('section', { class: 'card' }, data.assets.length ? h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['资产', '执行器', '文件', '操作'].map((label) => h('th', {}, label)))),
    h('tbody', {}, data.assets.map((asset) => h('tr', {},
      h('td', {}, h('a', { class: 'title card-link', href: href('native-assets/' + asset.id, { project: projectId }) }, asset.name), h('div', { class: 'sub mono' }, asset.id)),
      h('td', {}, asset.engine), h('td', { class: 'sub num' }, `${asset.files.length} 个文件`),
      h('td', {}, h('button', { class: 'btn sm', onClick: () => runAsset(projectId, asset) }, icon('play'), '运行'))))))
    : empty({ glyph: 'folder', title: '这个项目还没有原生资产', text: '可以导入 Python Selenium 项目、Postman/Apifox 集合或 JMeter 计划。',
      actions: h('button', { class: 'btn primary', onClick: () => importer(projects, engines.engines, projectId) }, '导入原生资产') }),
  data.page?.has_more && h('div', { class: 'card-body' }, h('button', { class: 'btn', onClick: () => go('native-assets', { project: projectId, cursor: data.page.next_cursor }) }, '下一页')),
  cursor && h('div', { class: 'card-body' }, h('button', { class: 'btn', onClick: () => go('native-assets', { project: projectId, cursor: null }) }, '回到第一页'))));
}

function importer(projects, engines, projectId) {
  const project = h('select', { class: 'select', id: 'asset-project' }, projectOptions(projects, projectId));
  const name = h('input', { class: 'input', id: 'asset-name', placeholder: '可选，默认使用来源文件名' });
  const source = h('input', { class: 'input', id: 'asset-source', placeholder: '本机测试目录或文件路径', autofocus: true });
  const engine = h('select', { class: 'select', id: 'asset-engine' }, engines.filter((item) => item.id !== 'http').map((item) => h('option', { value: item.id }, item.name || item.title || item.id)));
  const argv = h('textarea', { class: 'input mono', id: 'asset-argv', rows: 3, placeholder: '["python", "-m", "pytest"]' });
  const cwd = h('input', { class: 'input mono', id: 'asset-cwd', value: '.' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const save = h('button', { class: 'btn primary', onClick: async () => {
    error.hidden = true;
    try {
      if (!source.value.trim()) throw new Error('请填写来源目录或文件');
      const body = { source: source.value.trim(), engine: engine.value, name: name.value, cwd: cwd.value || '.' };
      if (argv.value.trim()) {
        body.argv = JSON.parse(argv.value);
        if (!Array.isArray(body.argv) || !body.argv.length || body.argv.some((arg) => typeof arg !== 'string' || !arg)) throw new Error('入口参数需要是非空字符串数组');
      }
      save.disabled = true;
      const asset = await api('/assets/' + encodeURIComponent(project.value), { method: 'POST', body });
      closeOverlay();
      toast('原生资产已导入，原文件和入口已保存');
      go('native-assets/' + asset.id, { project: project.value, cursor: null });
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { save.disabled = false; }
  } }, '导入');
  drawer({ title: '导入原生资产', subtitle: '来源路径位于运行 autoSkill 的电脑；目录及内部文件会一起保存。', body: [
    field('项目', project), field('名称', name), field('来源目录或文件', source), field('执行器', engine),
    field('入口参数（JSON 数组，可选）', argv, '留空时按资产推断入口。保留参数边界，不填写整段 shell 命令。'),
    field('工作目录', cwd, '相对于导入资产根目录。'), error,
  ], foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), save] });
}

async function detail(view, projectId, assetId) {
  let asset;
  try { asset = await api(`/assets/${encodeURIComponent(projectId)}/${encodeURIComponent(assetId)}`); }
  catch (error) { view.append(errorState(error, refresh)); return; }
  view.append(h('section', { class: 'card card-body stack' },
    h('div', { class: 'row wrap' }, h('h2', { class: 'grow' }, asset.name), h('button', { class: 'btn primary', onClick: () => runAsset(projectId, asset) }, icon('play'), '运行资产')),
    h('p', { class: 'sub' }, `执行器 ${asset.engine} · 工作目录 ${asset.cwd}`),
    h('p', { class: 'sub mono' }, `资产 ${asset.id} · 版本 ${asset.asset_version}`),
    h('div', { class: 'field' }, h('h3', {}, '入口参数'), h('pre', { class: 'code' }, JSON.stringify(asset.argv, null, 2))),
    h('p', { class: 'sub' }, '执行使用导入时保存的文件，不直接读取之后被修改的来源目录。第三方执行器和资产依赖需在本机可用。'),
    h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['文件', '字节', 'SHA-256'].map((label) => h('th', {}, label)))),
      h('tbody', {}, asset.files.map((file) => h('tr', {}, h('td', { class: 'mono' }, file.path), h('td', { class: 'num' }, file.size), h('td', { class: 'mono' }, file.sha256))))),
    h('a', { class: 'btn', href: href('native-assets', { project: projectId, cursor: null }) }, '返回资产列表')));
}

export function runAsset(projectId, asset) {
  const parameters = h('textarea', { class: 'input mono', id: 'asset-run-parameters', rows: 8, value: '{}' });
  const host = h('select', { class: 'select', id: 'asset-run-host' }, h('option', { value: 'generic' }, '通用宿主'), h('option', { value: 'dsh' }, 'DeepSeek Harness'));
  const session = h('input', { class: 'input', id: 'asset-run-session', placeholder: '可选，宿主会话标识' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const start = h('button', { class: 'btn primary', onClick: async () => {
    error.hidden = true;
    try {
      const values = JSON.parse(parameters.value);
      if (!values || Array.isArray(values) || typeof values !== 'object') throw new Error('运行参数需要是 JSON 对象');
      start.disabled = true;
      const run = await api('/native-runs', { method: 'POST', body: { project_id: projectId, asset_id: asset.id, head: asset.head,
        parameters: values, host: host.value, host_session_id: session.value } });
      closeOverlay();
      go('native-runs/' + run.id);
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { start.disabled = false; }
  } }, icon('play'), '开始运行');
  drawer({ title: '运行原生资产', subtitle: `${asset.name} · ${asset.engine}`, body: [
    field('运行参数（JSON）', parameters, '留空对象使用资产自身配置。JMeter 可提供 args 和 thresholds；多轮对话可提供 load。'),
    field('执行宿主', host, '需要语义判定时，宿主必须在线继续处理判定队列。'), field('宿主会话', session), error,
  ], foot: [h('button', { class: 'btn', onClick: closeOverlay }, '取消'), start] });
}
