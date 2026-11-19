// Browse the captured examples entirely in the browser.
const side = document.getElementById('side');
const toggle = side.querySelector('.nav-toggle');
const themeButton = side.querySelector('.theme-toggle');
const version = new URL(document.currentScript.src).searchParams.get('v');

function menu(open) {
  side.dataset.navOpen = String(open);
  toggle.setAttribute('aria-expanded', String(open));
}
function theme(value) {
  document.documentElement.dataset.theme = value;
  const label = value === 'dark' ? '浅色' : '深色';
  themeButton.querySelector('.text').textContent = label;
  themeButton.setAttribute('aria-label', label);
}
let preferred = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
try { preferred = sessionStorage.getItem('preview-theme') || preferred; } catch { /* Storage is optional. */ }
theme(preferred);
themeButton.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  theme(next);
  try { sessionStorage.setItem('preview-theme', next); } catch { /* Storage is optional. */ }
});
toggle.addEventListener('click', () => menu(side.dataset.navOpen !== 'true'));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && side.dataset.navOpen === 'true') { menu(false); toggle.focus(); }
});
document.addEventListener('pointerdown', (event) => { if (!side.contains(event.target)) menu(false); });
function listURL(mode, status) {
  const url = new URL(mode === 'results' ? 'results.html' : 'index.html', location.href);
  if (version) url.searchParams.set('v', version);
  if (status) url.searchParams.set('status', status);
  return url;
}

function bindHistory(toolbar) {
  const mode = toolbar.dataset.historyMode;
  const buttons = [...toolbar.querySelectorAll('[data-history-status]')];
  const rows = [...document.querySelectorAll('#view tr[data-history-verdict]')];
  const total = document.querySelector('[data-history-count]');
  // Dashboard links to a specific failed or blocked result retain that narrower scope.
  const legacyLabels = { Fail: '失败', Blocked: '阻塞' };
  const clear = document.createElement('button');
  clear.className = 'btn sm';
  clear.hidden = true;
  document.querySelector('[data-history-region="filters"]').append(clear);

  function selected() { return new URL(location.href).searchParams.get('status') || ''; }
  function render() {
    const status = selected();
    const legacy = Object.hasOwn(legacyLabels, status);
    const binary = buttons.some((button) => button.dataset.historyStatus === status);
    let count = 0;
    for (const row of rows) {
      row.hidden = legacy ? row.dataset.historyOutcome !== status.toLowerCase() : binary && row.dataset.historyVerdict !== status;
      if (!row.hidden) count++;
      const link = row.querySelector('a[data-history-id]');
      const url = new URL(link.href);
      url.searchParams.set('from', mode);
      if (binary || legacy) url.searchParams.set('status', status);
      else url.searchParams.delete('status');
      link.href = url.href;
    }
    total.textContent = String(count);
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.historyStatus === status));
    clear.hidden = !legacy;
    if (legacy) clear.textContent = '清除状态：' + legacyLabels[status];
  }
  function filter(value) {
    const url = new URL(location.href);
    if (value) url.searchParams.set('status', value);
    else url.searchParams.delete('status');
    history.pushState(null, '', url);
    render();
  }
  for (const button of buttons) {
    const value = button.dataset.historyStatus;
    button.addEventListener('click', () => filter(selected() === value ? '' : value));
  }
  clear.addEventListener('click', () => { filter(''); buttons[0].focus(); });
  for (const button of toolbar.querySelectorAll('[data-history-view]')) {
    if (button.dataset.historyView !== mode) button.addEventListener('click', () => location.assign(listURL(button.dataset.historyView, selected())));
  }
  window.addEventListener('pageshow', render);
  window.addEventListener('popstate', render);
  render();
}

function bindReport(back) {
  const params = new URL(location.href).searchParams;
  back.href = listURL(params.get('from'), params.get('status')).href;
  function expand(row, open) {
    row.classList.toggle('open', open);
    row.querySelector('.result-head').setAttribute('aria-expanded', String(open));
    row.querySelector('.result-body').hidden = !open;
  }
  for (const row of document.querySelectorAll('.result[data-result]')) {
    const button = row.querySelector('.result-head');
    button.addEventListener('click', () => expand(row, button.getAttribute('aria-expanded') !== 'true'));
  }
  for (const link of document.querySelectorAll('#view a[href*="#result-"]')) {
    link.search = location.search;
    link.addEventListener('click', () => {
      const target = document.getElementById(link.hash.slice(1))?.closest('.result');
      if (target) expand(target, true);
    });
  }
  function focusResult() {
    const head = document.getElementById(location.hash.slice(1));
    if (head?.matches('.result-head')) {
      expand(head.closest('.result'), true);
      head.focus({ preventScroll: true });
    }
  }
  window.addEventListener('hashchange', focusResult);
  focusResult();
}

const toolbar = document.querySelector('[data-history-region="toolbar"]');
if (toolbar?.querySelector('[data-history-view]')) bindHistory(toolbar);
// A cached page from the original preview still has plain view links.
for (const tab of document.querySelectorAll('[data-preview-href]')) {
  tab.addEventListener('click', () => location.assign(tab.dataset.previewHref));
}
const back = document.querySelector('#view .back');
if (back && document.querySelector('.result-head[aria-controls]')) bindReport(back);
