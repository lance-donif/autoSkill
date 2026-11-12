// Shared interface pieces: overlays, menus, empty states, the run launcher.
import { h, icon, api, attempt, toast, go, ENGINE } from './core.js';

const layer = () => document.getElementById('layer');
let closeCurrent = null;

function open(panel, onClose) {
  if (closeCurrent) closeCurrent();
  const previous = document.activeElement;
  const scrim = h('div', { class: 'scrim', onClick: () => close() });
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  function close() {
    document.removeEventListener('keydown', onKey);
    scrim.remove();
    panel.remove();
    closeCurrent = null;
    if (previous && previous.focus) previous.focus();
    if (onClose) onClose();
  }
  document.addEventListener('keydown', onKey);
  layer().append(scrim, panel);
  closeCurrent = close;
  const first = panel.querySelector('[autofocus], input, select, button.primary') || panel.querySelector('button');
  if (first) first.focus();
  return close;
}
export function closeOverlay() { if (closeCurrent) closeCurrent(); }

export function drawer({ title, subtitle, body, foot, wide, onClose }) {
  const panel = h('aside', { class: 'drawer' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'drawer-head' },
      h('div', { class: 'grow' }, h('h2', {}, title), subtitle && h('p', { class: 'sub' }, subtitle)),
      h('button', { class: 'btn ghost icon', 'aria-label': '关闭', onClick: () => closeOverlay() }, icon('x'))),
    h('div', { class: 'drawer-body' }, body),
    foot && h('div', { class: 'drawer-foot' }, foot));
  open(panel, onClose);
  return panel;
}

export function dialog({ title, body, actions }) {
  const panel = h('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'card-body stack' }, h('h2', {}, title), body),
    h('div', { class: 'drawer-foot' }, actions));
  open(panel);
  return panel;
}

export function confirm({ title, text, action, danger }) {
  return new Promise((resolve) => {
    let answered = false;
    const done = (value) => { answered = true; closeOverlay(); resolve(value); };
    const panel = h('div', { class: 'dialog', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': title },
      h('div', { class: 'card-body stack' }, h('h2', {}, title), h('p', { class: 'sub' }, text)),
      h('div', { class: 'drawer-foot' },
        h('button', { class: 'btn', onClick: () => done(false) }, '取消'),
        h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), autofocus: true, onClick: () => done(true) }, action)));
    open(panel, () => { if (!answered) resolve(false); });
  });
}

/** A button that opens a popover menu; closes on outside click or Escape. */
export function menuButton(trigger, build, alignRight = false) {
  const wrap = h('div', { class: 'pop' }, trigger);
  let menu = null;
  const outside = (event) => { if (!wrap.contains(event.target)) close(); };
  const key = (event) => { if (event.key === 'Escape') { close(); trigger.focus(); } };
  function close() {
    if (!menu) return;
    menu.remove();
    menu = null;
    trigger.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', key);
  }
  trigger.setAttribute('aria-haspopup', 'true');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.addEventListener('click', () => {
    if (menu) { close(); return; }
    menu = h('div', { class: 'menu' + (alignRight ? ' right' : ''), role: 'menu' });
    build(menu, close);
    wrap.append(menu);
    trigger.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', key);
  });
  return wrap;
}

export function menuItem(label, { checked, meta, onPick, glyph } = {}) {
  return h('button', {
    class: 'menu-item', role: checked === undefined ? 'menuitem' : 'menuitemcheckbox',
    'aria-checked': checked === undefined ? null : String(!!checked), onClick: onPick,
  },
  checked === undefined ? (glyph ? icon(glyph) : null) : icon('check', 'tick'),
  h('span', { class: 'ellipsis' }, label),
  meta != null && h('span', { class: 'meta' }, meta));
}

export function empty({ glyph = 'folder', title, text, actions }) {
  return h('div', { class: 'empty' }, h('div', { class: 'art' }, icon(glyph)), h('h3', {}, title),
    text && h('p', {}, text), actions && h('div', { class: 'row wrap' }, actions));
}

export function errorState(error, retry) {
  return empty({
    glyph: 'warn', title: '没能加载', text: error.message || String(error),
    actions: h('button', { class: 'btn', onClick: retry }, icon('reset'), '重试'),
  });
}

export function skeletonRows(count = 5) {
  return h('div', { class: 'card-body stack', 'aria-busy': 'true' },
    Array.from({ length: count }, (_, i) => h('div', { class: 'skeleton', style: `height:18px;width:${88 - i * 9}%` })));
}

/** The "how is this counted" note beside a figure. */
export function definition(text) {
  return h('span', { class: 'def', tabindex: '0', role: 'note', 'aria-label': '统计口径：' + text },
    icon('info'), h('span', { class: 'def-text' }, text));
}

// ---- running ----
const ENGINE_KEY = 'autoskill.engine';
export function preferredEngine() { return localStorage.getItem(ENGINE_KEY) || 'playwright'; }
export function setEngine(engine) { localStorage.setItem(ENGINE_KEY, engine); }
const HEADED_KEY = 'autoskill.headed';
export function preferredHeaded() { return localStorage.getItem(HEADED_KEY) !== '0'; }  // visible unless switched off
export function setHeaded(on) { localStorage.setItem(HEADED_KEY, on ? '1' : '0'); }

/** Queue runs and take the user to the live result. `items` = [{project_id, case_ids?}]. */
export async function startRun(items, button, engine = preferredEngine()) {
  const result = await attempt(() => api('/runs', { method: 'POST', body: { items, engine, headed: preferredHeaded() } }), button);
  if (!result) return null;
  if (result.skipped.length) toast(`${result.skipped.length} 个项目没有启动：${result.skipped[0].reason}`, { bad: true });
  if (result.runs.length === 1) go('runs/' + result.runs[0].id);
  else { toast(`已开始 ${result.runs.length} 个运行`); go('runs'); }
  return result;
}

/** A "run" button with an engine chooser beside it. */
export function runButton(label, getItems, { small = false, primary = true } = {}) {
  const size = small ? ' sm' : '';
  const main = h('button', {
    class: `btn${primary ? ' primary' : ''}${size}`,
    onClick: () => { const items = getItems(); if (items.length) startRun(items, main); },
  }, icon('play'), label);
  const chooser = h('button', {
    class: `btn${size} icon`, 'aria-label': '回放选项', title: '回放选项：引擎、是否显示浏览器窗口',
  }, icon('chevron'));
  const pop = menuButton(chooser, (menu, close) => {
    menu.append(h('div', { class: 'menu-title' }, '页面用例的回放引擎（接口用例始终走 HTTP）'));
    for (const engine of ['playwright', 'selenium']) {
      menu.append(menuItem(ENGINE[engine], {
        checked: preferredEngine() === engine,
        onPick: () => { setEngine(engine); close(); },
      }));
    }
    menu.append(h('div', { class: 'menu-sep' }), menuItem('显示浏览器窗口（最大化）', {
      checked: preferredHeaded(), onPick: () => { setHeaded(!preferredHeaded()); close(); },
    }), h('div', { class: 'menu-title' }, '默认显示；取消勾选后在后台运行、不显示窗口'));
  }, true);
  return h('div', { class: 'row', style: 'gap:4px' }, main, pop);
}
