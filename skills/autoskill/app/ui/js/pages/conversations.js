// Inspect captured dialogue sources and the assets and evidence saved by the host.
import { projectIndex } from '../shell.js';
import { h, icon, api, replace, route, scope, go, href, fmtTime, refresh } from '../core.js';
import { filterSelect, empty, errorState } from '../ui.js';

const path = (projectId, captureId = '') => '/conversations/' + encodeURIComponent(projectId) + (captureId ? '/' + encodeURIComponent(captureId) : '');
const json = (value) => JSON.stringify(value, null, 2);

export async function render(view, { id }) {
  let projects;
  try { projects = (await projectIndex()).filter((item) => !item.broken); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  const projectId = route().params.get('project') || scope().projects[0] || projects[0]?.id;
  const project = projects.find((item) => item.id === projectId);
  if (!project) {
    replace(view, h('div', { class: 'page-head' }, h('h1', {}, '多轮对话')), empty({ glyph: 'api', title: '还没有对话来源',
      text: '在 DSH 对话中交给 autoSkill 测试业务对话，已保存的请求来源与验证资产会显示在这里。' }));
    return;
  }
  if (id) await capturePage(view, project, id);
  else await overview(view, projects, project);
}

async function overview(view, projects, project) {
  let captures, assets;
  const offset = Math.max(0, Number(route().params.get('offset') || 0));
  try { [captures, assets] = await Promise.all([api(path(project.id) + `?limit=50&offset=${offset}`), api('/assets/' + encodeURIComponent(project.id) + '?all=1')]); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  const picker = filterSelect('项目', project.id, projects.map((item) => [item.id, item.name]),
    (value) => go('conversations', { project: value }), '对话所属项目');
  const existing = assets.assets.filter((item) => item.engine === 'ai-eval');
  replace(view, h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '多轮对话'),
    h('p', { class: 'sub' }, '查看真实请求来源与已保存的多轮验证资产；新增或调整测试请在 DSH 对话中交给 autoSkill。'))),
  h('div', { class: 'toolbar' }, picker),
  h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '捕获来源')),
    captures.items.length ? captureTable(project, captures.items) : empty({ glyph: 'api', title: '还没有对话来源',
      text: 'autoSkill 保存的业务对话请求会显示在这里。' }),
    captures.total > 50 && h('div', { class: 'card-body row', style: 'justify-content:flex-end' },
      h('span', { class: 'sub num' }, `${offset + 1}–${Math.min(offset + 50, captures.total)} / ${captures.total}`),
      h('button', { class: 'btn sm', disabled: offset === 0, onClick: () => go('conversations', { project: project.id, offset: Math.max(0, offset - 50) || null }) }, '上一页'),
      h('button', { class: 'btn sm', disabled: offset + 50 >= captures.total, onClick: () => go('conversations', { project: project.id, offset: offset + 50 }) }, '下一页'))),
  dialogueAssets(project, existing));
}

function captureTable(project, captures) {
  return h('div', { class: 'load-table-wrap' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['来源', '请求数', '候选轮次', '捕获时间', ''].map((text) => h('th', {}, text)))),
    h('tbody', {}, captures.map((item) => h('tr', {}, h('td', {}, h('div', { class: 'title' }, item.origin?.name || item.name || item.id), h('div', { class: 'sub mono' }, item.id)),
      h('td', { class: 'num' }, typeof item.requests === 'number' ? item.requests : item.request_count),
      h('td', { class: 'sub num' }, (item.conversation_candidates || []).join('、') || '尚无轮次记录'),
      h('td', { class: 'sub num' }, fmtTime(item.created_at)),
      h('td', { class: 'actions' }, h('a', { class: 'btn sm', href: href('conversations/' + item.id, { project: project.id }) }, '查看原始来源')))))));
}

function dialogueAssets(project, assets) {
  return h('section', { class: 'card', style: 'margin-top:16px' }, h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, '多轮验证资产'),
    h('p', { class: 'sub' }, '一次用例包含完整多轮对话；运行结果分别记录执行、答案质量与性能。'))),
  assets.length ? h('div', { class: 'list' }, assets.map((asset) => h('div', { class: 'list-row row' },
    h('div', { class: 'grow' }, h('div', { class: 'title' }, asset.name), h('div', { class: 'sub mono' }, asset.id)),
    h('a', { class: 'btn sm', href: href('native-assets/' + asset.id, { project: project.id }) }, '查看资产'),
    h('a', { class: 'btn sm', href: href('runs', { projects: project.id, range: 'all' }) }, '项目执行记录'))))
    : empty({ glyph: 'cases', title: '还没有多轮资产', text: '请在 DSH 对话中继续当前测试，保存的多轮验证资产会显示在这里。' }));
}

async function capturePage(view, project, captureId) {
  let capture;
  try { capture = await api(path(project.id, captureId)); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  replace(view, h('a', { class: 'back', href: href('conversations', { project: project.id }) }, icon('right'), '多轮对话'),
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, capture.origin?.name || '对话来源'),
      h('p', { class: 'sub' }, `${project.name} · ${capture.requests.length} 个业务请求 · ${fmtTime(capture.created_at)}`))),
    h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '真实请求与响应'),
      capture.requests.map((request) => h('details', { class: 'more' }, h('summary', {}, `请求 ${request.number} · ${request.request.method} ${request.request.url}`),
        h('p', { class: 'sub' }, `${request.protocol} · 响应状态 ${request.transport_status ?? '未捕获'} · 来源 ${request.capture_source}`),
        h('pre', { class: 'code' }, json({ request: request.request, body_json: request.body_json, response: request.response, response_json: request.response_json,
          body_complete: request.body_complete, capture_errors: request.capture_errors })))))),
    h('p', { class: 'sub' }, '来源与响应保留为证据。继续测试或调整多轮验证，请回到 DSH 对话。'),
    h('a', { class: 'btn', href: href('runs', { projects: project.id, range: 'all' }) }, '查看项目执行记录'));
}
