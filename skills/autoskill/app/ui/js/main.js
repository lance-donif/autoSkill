// Entry point: routing, the shared frame, and a quiet refresh while runs are in progress.
import { route, scope, onChange, h, replace, live, toast } from './core.js';
import { refreshShell, renderSide, renderScope, applyTheme, state } from './shell.js';
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

async function show({ quiet = false } = {}) {
  const { page, id } = route();
  const mine = ++token;
  const key = page + '/' + (id || '');
  const fresh = key !== lastKey;
  const currentScope = JSON.stringify(scope());
  if (fresh) renderSide();
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
    await module.render(holder, { id, quiet });
    if (mine !== token) return;
    if (!fresh && module.commit) module.commit(view, holder, { id, quiet });
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

function busy() {
  const focused = document.activeElement;
  return document.hidden || document.getElementById('layer').childElementCount > 0 || document.querySelector('.menu')
    || (focused && view.contains(focused) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(focused.tagName));
}

applyTheme(preferredTheme());
onPreferences(() => { applyTheme(preferredTheme()); renderSide(); });
if (new URLSearchParams(location.search).get('embed') === '1') document.documentElement.dataset.embed = 'true';
if (!location.hash) location.hash = '#/dashboard';

try { await loadPreferences(); } catch (error) { toast('偏好没能加载：' + error.message + '。刷新页面后重试。', { bad: true }); }
await refreshShell();
onChange(() => show());
await show();

// While something is running, keep numbers and progress current without flashing the page.
setInterval(async () => {
  if (busy()) return;
  const wasActive = state.active;
  await refreshShell();
  if (route().page !== 'data' && (state.active > 0 || wasActive > 0 || !state.online || Date.now() < live.until)) show({ quiet: true });
}, 2500);
