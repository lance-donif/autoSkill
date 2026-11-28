// Shared interface pieces: overlays, menus, empty states, the run launcher.
import { h, icon } from './core.js';
import { launchRuns } from './run-launcher.js';
export { preferredEngine, setEngine, preferredHeaded, setHeaded } from './preferences.js';

const layer = () => document.getElementById('layer');
let closeCurrent = null;
let closeMenu = null;

function open(panel, onClose, beforeClose) {
  if (closeCurrent && closeCurrent() === false) return null;
  const previous = document.activeElement;
  const scrim = h('div', { class: 'scrim', onClick: () => close() });
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  function close() {
    if (beforeClose && beforeClose() === false) return false;
    document.removeEventListener('keydown', onKey);
    scrim.remove();
    panel.remove();
    closeCurrent = null;
    if (previous?.focus) previous.focus();
    if (onClose) onClose();
    return true;
  }
  document.addEventListener('keydown', onKey);
  layer().append(scrim, panel);
  closeCurrent = close;
  const first = panel.querySelector('[autofocus], input, select, button.primary') || panel.querySelector('button');
  if (first) first.focus();
  return close;
}
export function closeOverlay() { return closeCurrent ? closeCurrent() : true; }

export function drawer({ title, subtitle, body, foot, wide, onClose, beforeClose }) {
  const panel = h('aside', { class: 'drawer' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'drawer-head' },
      h('div', { class: 'grow' }, h('h2', {}, title), subtitle && h('p', { class: 'sub' }, subtitle)),
      h('button', { class: 'btn ghost icon', 'aria-label': '关闭', onClick: () => closeOverlay() }, icon('x'))),
    h('div', { class: 'drawer-body' }, body),
    foot && h('div', { class: 'drawer-foot' }, foot));
  return open(panel, onClose, beforeClose) ? panel : null;
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

/** A compact, labelled select for list toolbars. */
export function filterSelect(label, value, options, onChange, name = label) {
  return h('label', { class: 'filter-field' }, h('span', { class: 'sub' }, label),
    h('select', { class: 'select', 'aria-label': name, onChange: (event) => onChange(event.currentTarget.value) },
      options.map(([key, text]) => h('option', { value: key, selected: key === value }, text))));
}

/** A button that opens a popover menu; closes on outside click or Escape. */
export function menuButton(trigger, build, alignRight = false) {
  const wrap = h('div', { class: 'pop' }, trigger);
  let menu = null;
  let removal;
  const outside = (event) => { if (!wrap.contains(event.target)) close(); };
  const key = (event) => { if (event.key === 'Escape') { close(); trigger.focus(); } };
  function close() {
    if (!menu) return;
    menu.remove();
    menu = null;
    trigger.setAttribute('aria-expanded', 'false');
    removal?.disconnect();
    removal = undefined;
    if (closeMenu === close) closeMenu = null;
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', key);
  }
  trigger.setAttribute('aria-haspopup', 'true');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.addEventListener('click', () => {
    if (menu) { close(); return; }
    closeMenu?.();
    menu = h('div', { class: 'menu' + (alignRight ? ' right' : ''), role: 'menu' });
    build(menu, close);
    wrap.append(menu);
    trigger.setAttribute('aria-expanded', 'true');
    closeMenu = close;
    removal = new MutationObserver(() => { if (!wrap.isConnected) close(); });
    removal.observe(document.body, { subtree: true, childList: true });
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

/** Queue runs and take the user to the live result. `items` = [{project_id, case_ids?}]. */
export async function startRun(items, button, engine) {
  return await launchRuns(items, button, engine);
}

/** A "run" button. How it replays (engine, window, browser) is set once, in the toolbar's replay settings. */
export function runButton(label, getItems, { small = false, primary = true, disabled = false, title } = {}) {
  const main = h('button', {
    class: `btn${primary ? ' primary' : ''}${small ? ' sm' : ''}`,
    disabled, title,
    onClick: () => { const items = getItems(); if (items.length) startRun(items, main); },
  }, icon('play'), label);
  return main;
}
