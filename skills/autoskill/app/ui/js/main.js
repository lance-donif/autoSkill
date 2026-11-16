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
let token = 0;
let lastKey = '';
let lastScope = '';
let refreshingFrame = null;
let initialRevision = null;
let snapshotStarted = false;

function projectSignature() {
  return JSON.stringify(state.projects);
}

function refreshFrame() {
  if (refreshingFrame) return refreshingFrame;
  const before = projectSignature();
  refreshingFrame = refreshShell(async () => {
    if (projectSignature() !== before && ['cases', 'dashboard'].includes(route().page)) await show({ quiet: true });
  }).finally(() => { refreshingFrame = null; });
  return refreshingFrame;
}

async function show({ quiet = false } = {}) {
  const { page, id } = route();
  const mine = ++token;
  const key = page + '/' + (id || '');
  const fresh = key !== lastKey;
  const currentScope = JSON.stringify(scope());
  if (fresh || currentScope !== lastScope) renderSide();
  if (fresh || currentScope !== lastScope) renderScope();
  if (!PAGES[page]) { location.hash = '#/dashboard'; return; }
  if (!view.childElementCount) {
    replace(view, h('div', { class: 'card' }, skeletonRows(6)));
  }
  view.setAttribute('aria-busy', 'true');
  try {
    const module = await PAGES[page]();
    if (mine !== token) return;
    const holder = document.createElement('div');
    snapshotStarted = true;
    await module.render(holder, { id, quiet });
    if (mine !== token) return;
    if (!fresh && module.commit) module.commit(view, holder, { id, quiet });
    else if (quiet && !fresh) updateLiveView(view, holder);
    else view.replaceChildren(...holder.childNodes);
    if (fresh) window.scrollTo(0, 0);
    lastKey = key;
    lastScope = currentScope;
  } catch (error) {
    if (mine !== token) return;
    replace(view, h('div', { class: 'card' }, errorState(error, () => show())));
  } finally {
    if (mine === token) view.removeAttribute('aria-busy');
  }
}

applyTheme(preferredTheme());
onPreferences(() => { applyTheme(preferredTheme()); renderSide(); renderScope(); });
if (new URLSearchParams(location.search).get('embed') === '1') document.documentElement.dataset.embed = 'true';
if (!location.hash) location.hash = '#/dashboard';

// Metadata never gates the first view or its navigation.
onChange(() => show());
void api('/state-revision').then((value) => {
  if (!snapshotStarted) initialRevision = value.revision;
}).catch(() => undefined);
const firstView = show();
void loadPreferences().catch((error) => toast('偏好没能加载：' + error.message + '。刷新页面后重试。', { bad: true }));
void refreshFrame();
await firstView;

// A late baseline stays unknown, forcing a fresh snapshot for writes during startup.
const polling = revisionPoll({
  initialRevision,
  read: () => api('/state-revision'),
  changed: async () => {
    // Request-owned screens keep drafts and receipts until an explicit refresh.
    if (!['data', 'diagnostics', 'conversations', 'project-materials'].includes(route().page)) await show({ quiet: true });
    // Updating a run must not wait for slower project and runtime metadata reads.
    void refreshFrame();
  },
  unavailable: () => { state.online = false; renderSide(); },
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) void polling.check(); });
window.addEventListener('focus', () => { void polling.check(); });
window.addEventListener('pageshow', () => { void polling.check(); });
window.addEventListener('pagehide', (event) => { if (!event.persisted) polling.close(); });
