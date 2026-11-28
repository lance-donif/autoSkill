// Small shared toolkit: DOM building, API calls, URL-backed filter state, formatting.
// Everything that comes from data is inserted with textContent, never as HTML.
import { listen } from './dom-events.js';

export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) listen(node, key.slice(2).toLowerCase(), value);
    else if (key in node && key !== 'list' && key !== 'form') node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  append(node, children);
  return node;
}
function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}
export function replace(node, ...children) { node.replaceChildren(); append(node, children); return node; }

const ICONS = {
  dashboard: 'M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-3H4zM14 7h6V4h-6z',
  cases: 'M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01',
  runs: 'M7 4v16l13-8z',
  clock: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  play: 'M7 4v16l13-8z',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  x: 'M6 6l12 12M18 6L6 18',
  blocked: 'M5 12h14',
  info: 'M12 11v6M12 7.5h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  warn: 'M12 9v4M12 17h.01M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
  plus: 'M12 5v14M5 12h14',
  chevron: 'M6 9l6 6 6-6',
  right: 'M9 6l6 6-6 6',
  reset: 'M4 4v6h6M20 20v-6h-6M20 9a8 8 0 0 0-14.5-3M4 15a8 8 0 0 0 14.5 3',
  copy: 'M9 9h10v10H9zM5 15V5h10',
  sun: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6L4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  moon: 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z',
  lock: 'M7 11V8a5 5 0 0 1 10 0v3M6 11h12v9H6z',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  edit: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4',
  trash: 'M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13',
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  stop: 'M7 7h10v10H7z',
  image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M9 9.5h.01',
  spark: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z',
  table: 'M4 6h16v12H4zM4 10h16M4 14h16M10 6v12',
  globe: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18',
  api: 'M8 8l-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14',
  sliders: 'M4 6h10M18 6h2M4 12h2M10 12h10M4 18h8M16 18h4M14 4v4M6 10v4M12 16v4',
};
export function icon(name, cls = 'icon-svg') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', name === 'play' || name === 'runs' || name === 'stop' ? 'currentColor' : 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', name === 'play' || name === 'runs' || name === 'stop' ? '0' : '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', cls);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name] || ICONS.info);
  svg.append(path);
  return svg;
}

export class ApiError extends Error {}
export async function api(path, options = {}) {
  let response;
  const headers = options.body === undefined ? {} : { 'Content-Type': 'application/json' };
  if (options.knownRevision) headers['If-None-Match'] = '"' + options.knownRevision + '"';
  try {
    response = await fetch('/api' + path, {
      method: options.method || 'GET',
      headers, signal: options.signal,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch {
    throw new ApiError('连不上 autoSkill 服务，请确认它还在运行');
  }
  if (response.status === 304 && options.knownRevision) return { revision: options.knownRevision };
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const error = new ApiError((data && typeof data.detail === 'string' && data.detail) || `请求没有成功（${response.status}）`);
    error.status = response.status;
    error.code = data?.code;
    error.details = data;
    throw error;
  }
  return data;
}

export function deleteSelection(path, items, options = {}) {
  const workspace = items[0]?.workspace_identity;
  if (!workspace || items.some((item) => item.workspace_identity !== workspace)) {
    throw new Error('无法确认所选内容的数据目录，请刷新页面后重新选择');
  }
  const query = new URLSearchParams({ expected_workspace: workspace });
  return api(path + '?' + query, { method: 'DELETE', ...options });
}

const STATUS = {
  NotRun: ['idle', '未执行', 'info'],
  Pass: ['pass', '通过', 'check'],
  Fail: ['fail', '失败', 'x'],
};
export function statusPill(status) {
  const [cls, text, glyph] = STATUS[status] || ['idle', '—', 'info'];
  return h('span', { class: 'status ' + cls }, icon(glyph), text);
}
export const CATEGORY = {
  locator_not_found: '找不到元素', assertion_failed: '结果与预期不符', timeout: '超时', unauthorized_origin: '地址未授权',
  environment: '环境问题', missing_secret: '缺少凭据', prerequisite_failed: '前置用例未通过', session_expired: '登录已过期',
  missing_variable: '缺少前置数据', not_permitted: '操作未被允许',
};
export const ENGINE = { playwright: 'Playwright', selenium: 'Selenium', http: 'HTTP' };
export const TRIGGER = { manual: '手动', schedule: '定时' };

// ---- filter scope: projects x time range, kept in the URL so a reload or a shared link shows the same numbers ----
export const RANGES = [
  { id: 'today', label: '今天' }, { id: '7d', label: '近 7 天' }, { id: '30d', label: '近 30 天' },
  { id: 'all', label: '全部时间' },
];
export const DEFAULT_RANGE = '7d';
const listeners = new Set();
const routeGuards = new Set();
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function beforeRouteChange(fn) { routeGuards.add(fn); return () => routeGuards.delete(fn); }

export function route() {
  const [path, query = ''] = location.hash.replace(/^#\/?/, '').split('?');
  const params = new URLSearchParams(query);
  const parts = path.split('/').filter(Boolean);
  return { page: parts[0] || 'dashboard', id: parts[1] || null, params };
}
export function scope() {
  const { params } = route();
  const range = params.get('range') || DEFAULT_RANGE;
  return {
    projects: (params.get('projects') || '').split(',').filter(Boolean),
    range, from: params.get('from') || '', to: params.get('to') || '',
  };
}
export function isDefaultScope(s = scope()) { return !s.projects.length && s.range === DEFAULT_RANGE; }

function startOfDay(date) { const d = new Date(date); d.setHours(0, 0, 0, 0); return d; }
export function bounds(s = scope()) {
  const today = startOfDay(new Date());
  const days = (n) => new Date(today.getTime() - n * 86400000);
  if (s.range === 'today') return { start: today, end: null };
  if (s.range === '7d') return { start: days(6), end: null };
  if (s.range === '30d') return { start: days(29), end: null };
  if (s.range === 'custom' && s.from && s.to) {
    const end = startOfDay(new Date(s.to + 'T00:00:00'));
    end.setDate(end.getDate() + 1);
    return { start: startOfDay(new Date(s.from + 'T00:00:00')), end };
  }
  return { start: null, end: null };
}
export function scopeQuery(extra = {}) {
  const s = scope();
  const { start, end } = bounds(s);
  const query = new URLSearchParams();
  if (s.projects.length) query.set('projects', s.projects.join(','));
  if (start) query.set('start', start.toISOString());
  if (end) query.set('end', end.toISOString());
  query.set('tz', String(-new Date().getTimezoneOffset()));
  for (const [key, value] of Object.entries(extra)) if (value != null && value !== '' && value !== false) query.set(key, value);
  return '?' + query.toString();
}
/** Navigate, keeping the current scope unless told otherwise. */
export function go(page, params = {}, keepScope = true) {
  const current = scope();
  const query = new URLSearchParams();
  if (keepScope) {
    if (current.projects.length) query.set('projects', current.projects.join(','));
    if (current.range !== DEFAULT_RANGE) query.set('range', current.range);
    if (current.range === 'custom') { query.set('from', current.from); query.set('to', current.to); }
  }
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === '' || value === false) query.delete(key); else query.set(key, value);
  }
  const text = query.toString();
  location.hash = '#/' + page + (text ? '?' + text : '');
}
export function setScope(patch) {
  const next = { ...scope(), ...patch };
  const { page, id, params } = route();
  for (const key of ['projects', 'range', 'from', 'to', 'offset']) params.delete(key);
  if (next.projects.length) params.set('projects', next.projects.join(','));
  if (next.range !== DEFAULT_RANGE) params.set('range', next.range);
  if (next.range === 'custom') { params.set('from', next.from); params.set('to', next.to); }
  const text = params.toString();
  location.hash = '#/' + page + (id ? '/' + id : '') + (text ? '?' + text : '');
}
export function href(page, params = {}) {
  const current = scope();
  const query = new URLSearchParams();
  if (current.projects.length) query.set('projects', current.projects.join(','));
  if (current.range !== DEFAULT_RANGE) query.set('range', current.range);
  if (current.range === 'custom') { query.set('from', current.from); query.set('to', current.to); }
  for (const [key, value] of Object.entries(params)) if (value != null && value !== '') query.set(key, value);
  const text = query.toString();
  return '#/' + page + (text ? '?' + text : '');
}
window.addEventListener('hashchange', (event) => {
  for (const guard of routeGuards) {
    if (guard() === false) { event.stopImmediatePropagation(); return; }
  }
  for (const fn of listeners) fn();
});
/** Re-render the current page in place (after an action changed what it shows). */
export function refresh() { for (const fn of listeners) fn(); }
/** A page showing something still in progress asks to be kept fresh for a few seconds. */
export const live = { until: 0 };

// ---- formatting ----
const pad = (n) => String(n).padStart(2, '0');
export function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso), now = new Date();
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return '今天 ' + clock;
  const yesterday = new Date(now.getTime() - 86400000);
  if (d.toDateString() === yesterday.toDateString()) return '昨天 ' + clock;
  return `${d.getFullYear() === now.getFullYear() ? '' : d.getFullYear() + '-'}${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock}`;
}
export function fmtUntil(iso) {
  if (!iso) return '—';
  const minutes = Math.round((new Date(iso) - Date.now()) / 60000);
  if (minutes < 1) return '即将执行';
  if (minutes < 60) return `${minutes} 分钟后`;
  if (minutes < 24 * 60) return `${Math.round(minutes / 60)} 小时后`;
  return fmtTime(iso);
}
export function fmtDuration(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  return `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`;
}
export function fmtNumber(n) {
  if (n == null) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (n >= 1e4) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
  return n.toLocaleString('en-US');
}
export function fmtPercent(ratio, digits = 1) {
  if (ratio == null) return '—';
  const value = ratio * 100;
  return (Number.isInteger(value) ? String(value) : value.toFixed(digits)) + '%';
}
export function rangeText(s = scope()) {
  if (s.range === 'custom') return `${s.from} 至 ${s.to}`;
  return (RANGES.find((r) => r.id === s.range) || RANGES[1]).label;
}

// ---- toasts ----
export function toast(message, options = {}) {
  const node = h('div', { class: 'toast' + (options.bad ? ' bad' : '') }, message,
    options.action && h('button', { onClick: () => { options.action.run(); node.remove(); } }, options.action.label));
  document.getElementById('toasts').append(node);
  setTimeout(() => node.remove(), options.bad ? 7000 : 4200);
}
export async function attempt(fn, button) {
  const original = button && [...button.childNodes];
  if (button) { button.disabled = true; button.prepend(h('i', { class: 'spin' })); }
  try { return await fn(); } catch (error) { toast(error.message || '操作没有成功', { bad: true }); return undefined; } finally {
    if (button) { button.disabled = false; button.replaceChildren(...original); }
  }
}
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('已复制'); } catch { toast('复制失败，请手动选择文字复制', { bad: true }); }
}
