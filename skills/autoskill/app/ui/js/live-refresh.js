// Apply changed server snapshots without losing drafts and scroll.
import { adoptHandlers } from './dom-events.js';
const FOCUSABLE = 'input,select,textarea,button,a,summary,[tabindex]';

function nodeKey(node) {
  if (node.nodeType !== 1) return node.nodeName;
  const controlText = node.tagName === 'DETAILS' ? 'DETAILS:' + node.querySelector(':scope > summary')?.textContent
    : ['BUTTON', 'A', 'SUMMARY'].includes(node.tagName) ? node.tagName + ':' + node.textContent : node.tagName;
  const key = node.id || node.dataset.result || node.dataset.case || node.getAttribute('aria-label') || node.getAttribute('name')
    || (node.tagName === 'A' && node.getAttribute('href')) || (node.classList.contains('result-head') && 'result-head') || controlText;
  const result = node.parentElement?.closest('[data-result], [data-case]');
  return result ? (result.dataset.result || result.dataset.case) + '/' + key : key;
}

function focusRecord(view) {
  const focused = document.activeElement;
  if (!focused || !view.contains(focused)) return null;
  const nodes = [...view.querySelectorAll(FOCUSABLE)];
  const key = nodeKey(focused);
  const index = nodes.filter((node) => nodeKey(node) === key).indexOf(focused);
  return { key, index, node: focused, value: focused.value, start: focused.selectionStart, end: focused.selectionEnd,
    editable: ['INPUT', 'SELECT', 'TEXTAREA'].includes(focused.tagName) };
}

function reuseEditor(holder, focused) {
  if (!focused?.editable || ['checkbox', 'radio', 'file', 'submit', 'button'].includes(focused.node.type)) return;
  const fresh = [...holder.querySelectorAll(FOCUSABLE)]
    .filter((node) => nodeKey(node) === focused.key)[focused.index];
  if (!fresh || fresh.tagName !== focused.node.tagName) return;
  const editor = focused.node;
  adoptAttributes(editor, fresh);
  if (editor.tagName === 'SELECT') editor.replaceChildren(...fresh.childNodes);
  fresh.replaceWith(editor);
}

function adoptAttributes(current, fresh) {
  adoptHandlers(current, fresh);
  for (const attribute of [...current.attributes]) if (!fresh.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
  for (const attribute of [...fresh.attributes]) current.setAttribute(attribute.name, attribute.value);
}

function reuseScrollRegions(view, holder) {
  const incoming = [...holder.querySelectorAll('.table-scroll[aria-label]')];
  for (const current of view.querySelectorAll('.table-scroll[aria-label]')) {
    const fresh = incoming.find((node) => nodeKey(node) === nodeKey(current));
    if (!fresh) continue;
    const position = [current.scrollLeft, current.scrollTop];
    adoptAttributes(current, fresh);
    current.replaceChildren(...fresh.childNodes);
    fresh.replaceWith(current);
    current.scrollLeft = position[0];
    current.scrollTop = position[1];
  }
}

export function updateLiveView(view, holder) {
  const focused = focusRecord(view);
  const scroll = [window.scrollX, window.scrollY];
  const positions = [...view.querySelectorAll('*')].filter((node) => node.scrollTop || node.scrollLeft)
    .map((node) => ({ key: nodeKey(node), top: node.scrollTop, left: node.scrollLeft }));
  const details = [...view.querySelectorAll('details')];
  const expanded = details.map((node) => ({ key: nodeKey(node),
    index: details.filter((candidate) => nodeKey(candidate) === nodeKey(node)).indexOf(node), open: node.open }));
  reuseEditor(holder, focused);
  reuseScrollRegions(view, holder);
  // Render-local callbacks close over fresh DOM nodes; move the complete changed view together.
  // The revision gate avoids rebuilding it for unchanged polls, including while typing.
  view.replaceChildren(...holder.childNodes);
  for (const entry of expanded) {
    const node = [...view.querySelectorAll('details')].filter((candidate) => nodeKey(candidate) === entry.key)[entry.index];
    if (node) node.open = entry.open;
  }
  for (const position of positions) {
    const node = [...view.querySelectorAll('*')].find((candidate) => nodeKey(candidate) === position.key);
    if (node) { node.scrollTop = position.top; node.scrollLeft = position.left; }
  }
  if (focused) {
    const node = [...view.querySelectorAll(FOCUSABLE)]
      .filter((candidate) => nodeKey(candidate) === focused.key)[focused.index];
    if (node) {
      if (focused.editable && node.value !== focused.value) node.value = focused.value;
      node.focus({ preventScroll: true });
      if (typeof focused.start === 'number' && ['text', 'search', 'tel', 'url', 'password'].includes(node.type)) {
        node.setSelectionRange(focused.start, focused.end);
      }
    }
  }
  if (window.scrollX !== scroll[0] || window.scrollY !== scroll[1]) window.scrollTo(...scroll);
}

export function revisionPoll({ read, changed, unavailable, visible = () => !document.hidden, interval = 250, initialRevision = null }) {
  let revision = initialRevision;
  let reading = false;
  let disposed = false;
  let failed = false;
  let pending = null;
  let applying = null;
  let timer = null;
  const lifetime = new AbortController();

  function failure(error) {
    if (!disposed) { failed = true; unavailable(error); }
  }

  async function applyPending() {
    while (!disposed && pending) {
      const latest = pending;
      pending = null;
      if (!failed && revision === latest.revision) continue;
      try {
        await changed(latest);
        if (!disposed) { revision = latest.revision; failed = false; }
      } catch (error) { failure(error); }
    }
  }

  function queue(value) {
    if (!applying && !failed && revision === value.revision) return undefined;
    pending = value;
    if (!applying) applying = applyPending().finally(finishedApplying);
    return applying;
  }

  function finishedApplying() {
    applying = null;
    if (!disposed && pending) {
      const value = pending;
      pending = null;
      queue(value);
    }
  }

  async function check() {
    if (disposed || reading || !visible()) return;
    reading = true;
    let value;
    try {
      value = await read(lifetime.signal);
    } catch (error) { failure(error); }
    finally { reading = false; }
    if (!disposed && value) await queue(value);
  }
  function pause() {
    clearInterval(timer);
    timer = null;
  }

  function visibilityChanged() {
    if (disposed || !visible()) { pause(); return; }
    timer ??= setInterval(check, interval);
    void check();
  }

  if (visible()) timer = setInterval(check, interval);
  return { check, visibilityChanged, close: () => { disposed = true; pending = null; pause(); lifetime.abort(); } };
}
