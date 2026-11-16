// Inspect preserved native assets and follow their project execution history.
import { h, replace, api, route, scope, go, href, refresh } from '../core.js';
import { empty, errorState } from '../ui.js';

const projectOptions = (projects, value) => projects.map((project) => h('option', { value: project.id, selected: project.id === value }, project.name));

export async function render(view, { id }) {
  let projects;
  try { projects = await api('/projects'); }
  catch (error) { replace(view, errorState(error, refresh)); return; }
  const projectId = route().params.get('project') || scope().projects[0] || projects[0]?.id;
  const selected = projects.find((project) => project.id === projectId);
  const select = h('select', { class: 'select', 'aria-label': '原生资产项目', onChange: () => go('native-assets', { project: select.value, cursor: null }) }, projectOptions(projects, projectId));
  replace(view, h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '原生资产'),
    h('p', { class: 'sub' }, '查看已保存脚本、集合、负载计划和多轮对话。新增或调整测试请在 DSH 对话中交给 autoSkill。'))),
  projects.length ? h('div', { class: 'toolbar' }, h('label', {}, '项目'), select,
    h('a', { class: 'btn', href: href('diagnostics') }, '检查执行器'), h('a', { class: 'btn', href: href('runs', { projects: projectId, range: 'all' }) }, '执行记录')) : null);
  if (!selected) { view.append(h('section', { class: 'card' }, empty({ title: '还没有原生资产', text: '在 DSH 对话中交给 autoSkill 使用已有测试项目；保存后可在这里查看。' }))); return; }
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
      h('td', {}, h('a', { class: 'btn sm', href: href('runs', { projects: projectId, range: 'all' }) }, '项目执行记录'))))))
    : empty({ glyph: 'folder', title: '这个项目还没有原生资产', text: '在 DSH 对话中说明要使用的已有脚本、集合或负载计划，交给 autoSkill 处理。' }),
  data.page?.has_more && h('div', { class: 'card-body' }, h('button', { class: 'btn', onClick: () => go('native-assets', { project: projectId, cursor: data.page.next_cursor }) }, '下一页')),
  cursor && h('div', { class: 'card-body' }, h('button', { class: 'btn', onClick: () => go('native-assets', { project: projectId, cursor: null }) }, '回到第一页'))));
}

async function detail(view, projectId, assetId) {
  let asset;
  try { asset = await api(`/assets/${encodeURIComponent(projectId)}/${encodeURIComponent(assetId)}`); }
  catch (error) { view.append(errorState(error, refresh)); return; }
  view.append(h('section', { class: 'card card-body stack' },
    h('div', { class: 'row wrap' }, h('h2', { class: 'grow' }, asset.name), h('a', { class: 'btn', href: href('runs', { projects: projectId, range: 'all' }) }, '查看项目执行记录')),
    h('p', { class: 'sub' }, `执行器 ${asset.engine} · 工作目录 ${asset.cwd}`),
    h('p', { class: 'sub mono' }, `资产 ${asset.id} · 版本 ${asset.asset_version}`),
    h('div', { class: 'field' }, h('h3', {}, '入口参数'), h('pre', { class: 'code' }, JSON.stringify(asset.argv, null, 2))),
    h('p', { class: 'sub' }, '执行使用导入时保存的文件，不直接读取之后被修改的来源目录。第三方执行器和资产依赖需在本机可用。'),
    h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['文件', '字节', 'SHA-256'].map((label) => h('th', {}, label)))),
      h('tbody', {}, asset.files.map((file) => h('tr', {}, h('td', { class: 'mono' }, file.path), h('td', { class: 'num' }, file.size), h('td', { class: 'mono' }, file.sha256))))),
    h('a', { class: 'btn', href: href('native-assets', { project: projectId, cursor: null }) }, '返回资产列表')));
}
