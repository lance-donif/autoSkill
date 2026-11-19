// The Pages demo runs the dashboard against a document-local sample workspace.
import { setRunScreenshotResolver } from '../skills/autoskill/app/ui/js/pages/runs.js';
import { casesApi } from './cases-api.js';
import { advanceRuns, runsApi } from './runs-api.js';
import { installLoadPages } from './load-pages.js';
import { advanceTasks, schedulesApi } from './schedules-api.js';
import { filesApi } from './files-api.js';
import { commitData, currentData, getRevision, startSession } from './store.js';

const networkFetch = window.fetch.bind(window);
const apiPath = (url) => url.origin === location.origin && url.pathname.startsWith('/api/');
let fixtures;
let requests = Promise.resolve();

function sampleSettings({ path, method, body, db }) {
  const settings = db.settings;
  if (path === '/state-revision' && method === 'GET') return { revision: getRevision() };
  if (path === '/status' && method === 'GET') return { ...fixtures[path], browser: settings.browser };
  if (path !== '/settings') return undefined;
  if (method === 'GET') return settings;
  if (method !== 'PUT') return undefined;
  if (body.expected_data_id && body.expected_data_id !== settings.data_id) throw new Error('示例数据已经变更，请刷新设置');
  if (body.dashboard) Object.assign(settings.dashboard, body.dashboard);
  if (['system', 'bundled'].includes(body.browser)) {
    settings.browser.mode = body.browser;
    settings.browser.using = settings.browser[body.browser];
  }
  return settings;
}

async function dispatch(url, method, body, headers) {
  const before = currentData();
  const now = Date.now();
  const advanced = advanceRuns(before, now);
  const scheduled = advanceTasks(before, now, fixtures);
  if (advanced || scheduled) commitData(before);
  const write = method !== 'GET' && method !== 'HEAD';
  const db = write ? structuredClone(before) : before;
  const context = { path: url.pathname.slice('/api'.length), method, params: url.searchParams, body, headers, db, fixtures };
  if (method === 'GET' && location.hash.split('?')[0] === '#' + context.path && context.path.startsWith('/load-tests/')) {
    const offset = new URLSearchParams(location.hash.split('?')[1]).get('detail_offset');
    if (offset && !context.params.has('detail_offset')) context.params.set('detail_offset', offset);
  }
  for (const handler of [sampleSettings, casesApi, schedulesApi, runsApi, filesApi]) {
    const value = await handler(context);
    if (value === undefined) continue;
    const response = value instanceof Response ? value : Response.json(value);
    if (write && response.ok) commitData(db);
    return response;
  }
  if (!write) {
    const value = fixtures[context.path + url.search] ?? fixtures[context.path];
    if (value !== undefined) return Response.json(value);
  }
  return Response.json({ detail: '示例中找不到这项数据或操作' }, { status: 404 });
}

async function previewFetch(input, options) {
  const url = new URL(input instanceof Request ? input.url : input, location.href);
  if (!apiPath(url)) return networkFetch(input, options);
  const request = new Request(input instanceof Request ? input : url, options);
  let body;
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    body = request.headers.get('Content-Type')?.includes('application/json') ? await request.json() : await request.blob();
  }
  const operation = requests.then(() => dispatch(url, request.method, body, request.headers)).catch((error) =>
    Response.json({ detail: error.message || '操作失败' }, { status: 400 }));
  requests = operation.then(() => undefined);
  return operation;
}

function downloadFiles() {
  document.addEventListener('click', async (event) => {
    const link = event.target.closest?.('a[href]');
    if (!link || event.defaultPrevented || !apiPath(new URL(link.href))) return;
    event.preventDefault();
    try {
      const response = await previewFetch(link.href);
      if (!response.ok) throw new Error((await response.json()).detail);
      const header = response.headers.get('Content-Disposition') || '';
      const encoded = header.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const name = encoded ? decodeURIComponent(encoded) : header.match(/filename="([^"]+)"/i)?.[1];
      const blobUrl = URL.createObjectURL(await response.blob());
      const save = document.createElement('a');
      save.download = name || link.download || 'autoSkill';
      save.href = blobUrl;
      document.body.append(save); save.click(); save.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 30000);
    } catch (error) {
      const { toast } = await import('../skills/autoskill/app/ui/js/core.js');
      toast(error.message, { bad: true });
    }
  });
}

function localReceipts() {
  // Do not let the shared run-launcher's tab receipts survive a demo workspace reset.
  const entries = new Map();
  Object.defineProperty(window, 'sessionStorage', { configurable: true, value: {
    getItem: (key) => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, String(value)),
    removeItem: (key) => entries.delete(key), clear: () => entries.clear(),
  } });
}

function dateSampleRuns(catalog) {
  const runs = catalog['/runs'].items;
  const observed = new Set(runs.filter((run) => run.project_id === 'runoob').map((run) => run.id));
  const seedTime = Math.max(...runs.filter((run) => !observed.has(run.id))
    .map((run) => Date.parse(run.finished_at || run.created_at)).filter(Number.isFinite));
  if (!Number.isFinite(seedTime)) return;
  const delta = Date.now() - seedTime - 60000;
  const dateField = /^(created_at|started_at|updated_at|finished_at|last_run_at|last_finished|next_due|planned_at|generated_at|archived_at|saved_at)$/;
  function shift(value) {
    if (!value || typeof value !== 'object') return;
    if (value.project_id === 'runoob' || value.id === 'runoob' || observed.has(value.id) || observed.has(value.run_id)) return;
    for (const [key, child] of Object.entries(value)) {
      if (typeof child === 'string' && dateField.test(key)) {
        const time = Date.parse(child);
        if (Number.isFinite(time)) value[key] = new Date(time + delta).toISOString();
      } else shift(child);
    }
  }
  for (const [path, value] of Object.entries(catalog)) {
    if (!path.split(/[/?]/).some((part) => part === 'runoob' || observed.has(part))) shift(value);
  }
}

function bindObservedScreenshots(source) {
  const images = new Map();
  for (const record of [...source.previous_cases, ...source.cases]) {
    if (!record.screenshot_path) continue;
    const hash = record.artifact_hashes.screenshot_sha256;
    const url = new URL('../' + record.screenshot_path, import.meta.url);
    if (url.origin !== location.origin) continue;
    const key = JSON.stringify([record.run_id, record.case_id, record.revision, hash]);
    images.set(key, url.href);
  }
  setRunScreenshotResolver((run, row, name) => {
    const hash = row.detail.artifact_hashes?.screenshot_sha256;
    if (run.simulated || run.project_id !== source.project_id || row.run_id !== run.id || name !== hash + '.png') return null;
    return images.get(JSON.stringify([run.id, row.case_id, row.case_revision, hash]));
  });
}

async function start() {
  const response = await networkFetch(new URL('./fixtures.json', import.meta.url));
  if (!response.ok) throw new Error('示例数据加载失败，请刷新重试');
  fixtures = await response.json();
  dateSampleRuns(fixtures);
  if (fixtures['/observations/runoob']) bindObservedScreenshots(fixtures['/observations/runoob']);
  startSession(fixtures);
  localReceipts();
  window.fetch = previewFetch;
  downloadFiles();
  if (!location.hash.startsWith('#/')) history.replaceState(null, '', '#/' + (document.body.dataset.route || 'dashboard'));
  await import('../skills/autoskill/app/ui/js/main.js');
  const { showObservations } = await import('./observations.js');
  showObservations(fixtures['/observations/runoob']);
  installLoadPages(currentData);
  const notice = document.createElement('span');
  notice.textContent = '预览操作仅在当前页面有效，刷新后还原';
  document.querySelector('.side-foot')?.prepend(notice);
}

start().catch((error) => {
  document.getElementById('view').textContent = error.message || '无法打开示例，请刷新重试';
});
