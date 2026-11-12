// Charts, hand-drawn in SVG. One axis per chart; status colours always come with a legend and a table view.
import { h, fmtNumber, fmtPercent } from './core.js';

const NS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) if (value != null) node.setAttribute(key, value);
  node.append(...children.filter(Boolean));
  return node;
}
// Stack order bottom -> top. Blocked sits between pass and fail: green and red never touch,
// which is what keeps the stack readable with red-green colour blindness (validated).
export const SERIES = [
  { key: 'passed', label: '通过', color: 'var(--good)' },
  { key: 'blocked', label: '阻塞', color: 'var(--warn)' },
  { key: 'failed', label: '失败', color: 'var(--bad)' },
];

function niceMax(value) {
  if (value <= 4) return 4;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 1.2, 1.6, 2, 2.4, 3.2, 4, 6, 8, 10]) if (value <= step * power) return step * power;  // all divide by 4 cleanly
  return 10 * power;
}
export function bucketLabel(bucket, unit) {
  return unit === 'hour' ? bucket.slice(11, 16) : `${Number(bucket.slice(5, 7))}/${Number(bucket.slice(8, 10))}`;
}
/** Fill gaps so a day with no runs shows as empty instead of disappearing from the axis. */
export function fillBuckets(points, unit, start, end) {
  if (!points.length && !start) return [];
  const byKey = new Map(points.map((p) => [p.bucket, p]));
  const pad = (n) => String(n).padStart(2, '0');
  const key = (d) => unit === 'hour'
    ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:00`
    : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parse = (text) => new Date(text.replace(' ', 'T') + (unit === 'hour' ? ':00' : 'T00:00:00'));
  const cursor = start ? new Date(start) : parse(points[0].bucket);
  const last = end ? new Date(end.getTime() - 1) : new Date();
  if (unit === 'hour') cursor.setMinutes(0, 0, 0); else cursor.setHours(0, 0, 0, 0);
  const out = [];
  for (let guard = 0; cursor <= last && guard < 400; guard++) {
    out.push(byKey.get(key(cursor)) || { bucket: key(cursor), passed: 0, failed: 0, blocked: 0, pass_rate: null });
    if (unit === 'hour') cursor.setHours(cursor.getHours() + 1); else cursor.setDate(cursor.getDate() + 1);
  }
  return out;
}

export function legend() {
  return h('div', { class: 'legend' }, SERIES.map((s) => h('span', { class: 'legend-item' },
    h('i', { class: 'swatch', style: `background:${s.color}` }), s.label)));
}

/** Stacked columns: executions per bucket split by outcome. */
export function trendChart(points, unit, onPick) {
  const width = 760, height = 236, left = 36, right = 8, top = 12, bottom = 26;
  const plotW = width - left - right, plotH = height - top - bottom;
  const max = niceMax(Math.max(1, ...points.map((p) => p.passed + p.failed + p.blocked)));
  const slot = plotW / Math.max(points.length, 1);
  const bar = Math.max(3, Math.min(24, slot - 6));
  const y = (value) => top + plotH - (value / max) * plotH;
  const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', class: 'chart-svg',
    'aria-label': `执行趋势：${points.length} 个时间段的通过、阻塞、失败条次` });
  for (let i = 0; i <= 4; i++) {
    const value = (max / 4) * i;
    root.append(svg('line', { x1: left, x2: width - right, y1: y(value), y2: y(value), class: i ? 'grid' : 'axis' }));
    const tick = svg('text', { x: left - 8, y: y(value) + 4, 'text-anchor': 'end', class: 'tick' });
    tick.textContent = fmtNumber(value);
    root.append(tick);
  }
  const every = Math.ceil(points.length / 10);
  const tip = h('div', { class: 'tip', hidden: true });
  const wrap = h('div', { class: 'chart-wrap' }, root, tip);
  points.forEach((point, index) => {
    const cx = left + slot * index + slot / 2;
    const group = svg('g', { class: 'col', tabindex: '0', role: 'button' });
    let base = 0;
    const total = point.passed + point.failed + point.blocked;
    const visible = SERIES.filter((s) => point[s.key] > 0);
    visible.forEach((series, position) => {
      const from = y(base), to = y(base + point[series.key]);
      const gap = position === 0 ? 0 : 2;  // surface-coloured gap between stacked segments
      const topMost = position === visible.length - 1;
      const hgt = Math.max(1, from - to - gap);
      const x = cx - bar / 2, r = topMost ? Math.min(4, bar / 2, hgt) : 0;
      const yTop = from - gap - hgt;
      const d = `M${x},${yTop + hgt}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + bar - r}Q${x + bar},${yTop} ${x + bar},${yTop + r}V${yTop + hgt}Z`;
      group.append(svg('path', { d, fill: series.color }));
      base += point[series.key];
    });
    group.append(svg('rect', { x: left + slot * index, y: top, width: slot, height: plotH, class: 'hit' }));
    const name = bucketLabel(point.bucket, unit);
    group.setAttribute('aria-label', `${point.bucket}：通过 ${point.passed}，阻塞 ${point.blocked}，失败 ${point.failed}`);
    const show = () => {
      tip.replaceChildren(
        h('div', { class: 'tip-title' }, unit === 'hour' ? point.bucket.slice(5) : point.bucket),
        ...SERIES.map((s) => h('div', { class: 'tip-row' }, h('i', { class: 'key', style: `background:${s.color}` }),
          h('b', {}, String(point[s.key])), h('span', {}, s.label))),
        h('div', { class: 'tip-foot' }, total ? `通过率 ${fmtPercent(point.pass_rate)}` : '没有执行'));
      tip.hidden = false;
      const share = (cx / width) * 100;
      tip.style.left = `clamp(70px, ${share}%, calc(100% - 70px))`;
      tip.style.top = `${(Math.min(y(total), top + plotH - 40) / height) * 100}%`;
    };
    group.addEventListener('pointerenter', show);
    group.addEventListener('focus', show);
    group.addEventListener('pointerleave', () => { tip.hidden = true; });
    group.addEventListener('blur', () => { tip.hidden = true; });
    if (onPick && total) {
      group.addEventListener('click', () => onPick(point));
      group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onPick(point); } });
    }
    root.append(group);
    if (index % every === 0 || index === points.length - 1) {
      const label = svg('text', { x: cx, y: height - 8, 'text-anchor': 'middle', class: 'tick' });
      label.textContent = name;
      root.append(label);
    }
  });
  return wrap;
}

/** The same data as a table: the accessible twin of the chart. */
export function trendTable(points, unit) {
  return h('div', { class: 'chart-table' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['时间', '通过', '阻塞', '失败', '通过率'].map((t) => h('th', {}, t)))),
    h('tbody', {}, points.filter((p) => p.passed + p.failed + p.blocked).reverse().map((p) => h('tr', {},
      h('td', { class: 'num' }, unit === 'hour' ? p.bucket.slice(5) : p.bucket),
      h('td', { class: 'num' }, String(p.passed)), h('td', { class: 'num' }, String(p.blocked)),
      h('td', { class: 'num' }, String(p.failed)), h('td', { class: 'num' }, fmtPercent(p.pass_rate)))))));
}

/** Pass rate over time: a single-series sparkline in a muted hue, last point in the accent. */
export function sparkline(points) {
  const values = points.map((p) => p.pass_rate);
  const known = values.filter((v) => v != null);
  if (known.length < 2) return null;
  const width = 132, height = 36, pad = 5;
  const lo = Math.min(...known), hi = Math.max(...known);
  const span = hi - lo || 1;
  const x = (i) => pad + (i / (values.length - 1)) * (width - pad * 2);
  const y = (v) => height - pad - ((v - lo) / span) * (height - pad * 2);
  let d = '', pen = false, last = null;
  values.forEach((value, index) => {
    if (value == null) return;  // a period with no runs has no rate: the line bridges it
    d += `${pen ? 'L' : 'M'}${x(index).toFixed(1)},${y(value).toFixed(1)}`;
    pen = true;
    last = [x(index), y(value)];
  });
  return svg('svg', { viewBox: `0 0 ${width} ${height}`, class: 'spark', 'aria-hidden': 'true' },
    svg('path', { d, class: 'spark-line' }),
    svg('circle', { cx: last[0], cy: last[1], r: 4, class: 'spark-dot' }));
}

/** One whole split into outcomes, thin, no text: the figures sit beside it. */
export function splitBar(counts) {
  const bar = h('div', { class: 'split', role: 'img',
    'aria-label': SERIES.map((s) => `${s.label} ${counts[s.key] || 0}`).join('，') });
  for (const series of SERIES) {
    if (!counts[series.key]) continue;
    bar.append(h('i', { style: `flex:${counts[series.key]};background:${series.color}`, title: `${series.label} ${counts[series.key]}` }));
  }
  return bar;
}
