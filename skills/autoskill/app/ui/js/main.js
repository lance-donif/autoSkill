// Entry point: routing, the shared frame, and a quiet refresh while runs are in progress.
import { route, onChange, h, replace, live, toast } from './core.js';
import { refreshShell, renderSide, renderScope, applyTheme, state } from './shell.js';
import { skeletonRows, errorState } from './ui.js';
import { loadPreferences, onPreferences, preferredTheme } from './preferences.js';

const PAGES = {
  dashboard: () => import('./pages/dashboard.js'),
  cases: () => import('./pages/cases.js'),
  runs: () => import('./pages/runs.js'),
  schedules: () => import('./pages/schedules.js'),
  data: () => import('./pages/data.js'),
};
const view = document.getElementById('view');
let token = 0;
let lastKey = '';

async function show({ quiet = false } = {}) {
  const { page, id } = route();
  const mine = ++token;
  const key = page + '/' + (id || '');
  const fresh = key !== lastKey;
  renderSide();
  renderScope();
  if (!PAGES[page]) { location.hash = '#/dashboard'; return; }
  if (fresh) {
    replace(view, h('div', { class: 'card' }, skeletonRows(6)));
    window.scrollTo(0, 0);
  } else if (!quiet) {
    view.classList.add('stale');  // filters changed: keep the previous render in place while the new one loads
  }
  try {
    const module = await PAGES[page]();
    if (mine !== token) return;
    const holder = quiet || !fresh ? document.createElement('div') : view;
    await module.render(holder, { id, quiet });
    if (mine !== token) return;
    if (holder !== view) view.replaceChildren(...holder.childNodes);
    lastKey = key;
  } catch (error) {
    if (mine !== token) return;
    replace(view, h('div', { class: 'card' }, errorState(error, () => show())));
  } finally {
    if (mine === token) view.classList.remove('stale');
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
