// Entry point: routing, the shared frame, and a quiet refresh while runs are in progress.
import { route, scope, onChange, h, replace, api, toast } from './core.js';
import { refreshShell, renderSide, renderScope, applyTheme, state } from './shell.js';
import { revisionPoll, updateLiveView } from './live-refresh.js';
import { skeletonRows, errorState } from './ui.js';
import { loadPreferences, onPreferences, preferredTheme } from './preferences.js';

const PAGES = {
  dashboard: () => import('./pages/dashboard.js'),
  cases: () => import('./pages/cases.js'),
  'load-tests': () => import('./pages/load-tests.js'),
  'project-materials': () => import('./pages/project-materials.js'),
  'native-assets': () => import('./pages/native-assets.js'),
  'native-runs': () => import('./pages/native-runs.js'),
  workflows: () => import('./pages/workflows.js'),
  'case-plans': () => import('./pages/case-plans.js'),
  conversations: () => import('./pages/conversations.js'),
  diagnostics: () => import('./pages/diagnostics.js'),
  runs: () => import('./pages/runs.js'),
  schedules: () => import('./pages/schedules.js'),
  data: () => import('./pages/data.js'),
};
const view = document.getElementById('view');
const loadingText = '正在加载页面…';
// Keep the announcement outside the busy view so assistive technology can announce it immediately.
const loadingStatus = h('span', { class: 'sr', role: 'status' });
view.before(loadingStatus);
let token = 0;
let viewKey = '';
let lastScope = '';
let loadingPage = false;
let initialRevision = null;
let snapshotStarted = false;

async function show({ quiet = false } = {}) {
  const { page, id } = route();
  const mine = ++token;
  const hash = location.hash;
  const isCurrent = () => mine === token && hash === location.hash;
  if (!PAGES[page]) { location.hash = '#/dashboard'; return; }
  const key = page + '/' + (id || '');
  const changedPage = key !== viewKey;
  const currentScope = JSON.stringify(scope());
  if (changedPage || currentScope !== lastScope) {
    renderSide();
    renderScope();
    lastScope = currentScope;
  }
  if (changedPage) {
    viewKey = key;
    loadingPage = true;
    loadingStatus.textContent = loadingText;
    replace(view, h('div', { class: 'card', 'aria-hidden': 'true' },
      h('div', { class: 'card-head' }, h('p', { class: 'sub' }, loadingText)), skeletonRows(6)));
  }
  const fresh = loadingPage;
  view.setAttribute('aria-busy', 'true');
  try {
    const module = await PAGES[page]();
    if (!isCurrent()) return;
    const holder = document.createElement('div');
    snapshotStarted = true;
    await module.render(holder, { id, quiet, isCurrent });
    if (!isCurrent()) return;
    if (!fresh && module.commit) module.commit(view, holder, { id, quiet });
    else if (quiet && !fresh) updateLiveView(view, holder);
    else view.replaceChildren(...holder.childNodes);
    if (fresh) window.scrollTo(0, 0);
  } catch (error) {
    if (!isCurrent()) return;
    replace(view, h('div', { class: 'card' }, errorState(error, () => show())));
  } finally {
    if (mine === token) {
      loadingPage = false;
      loadingStatus.textContent = '';
      view.removeAttribute('aria-busy');
    }
  }
}

applyTheme(preferredTheme());
onPreferences(() => { applyTheme(preferredTheme()); renderSide(); renderScope(); });
if (new URLSearchParams(location.search).get('embed') === '1') document.documentElement.dataset.embed = 'true';
if (!location.hash) history.replaceState(history.state, '', '#/dashboard');

// Metadata never gates the first view or its navigation.
onChange(() => show());
void api('/state-revision').then((value) => {
  if (!snapshotStarted) initialRevision = value.revision;
}).catch(() => undefined);
const firstView = show();
void loadPreferences().catch((error) => toast('偏好没能加载：' + error.message + '。刷新页面后重试。', { bad: true }));
void refreshShell();
await firstView;

// A late baseline stays unknown, forcing a fresh snapshot for writes during startup.
const polling = revisionPoll({
  initialRevision,
  read: () => api('/state-revision'),
  changed: async () => {
    // Request-owned screens keep drafts and receipts until an explicit refresh.
    // Start independent reads together so pages that need the project list can share it.
    void refreshShell();
    if (!['data', 'diagnostics', 'conversations', 'project-materials'].includes(route().page)) await show({ quiet: true });
  },
  unavailable: () => { state.online = false; renderSide(); },
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) void polling.check(); });
window.addEventListener('focus', () => { void polling.check(); });
window.addEventListener('pageshow', () => { void polling.check(); });
window.addEventListener('pagehide', (event) => { if (!event.persisted) polling.close(); });
