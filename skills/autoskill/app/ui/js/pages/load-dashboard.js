export function aggregateLoadReport(run, { stage } = {}) {
  const report = run.load_report || {};
  const selected = stage == null || stage === '' || stage === 'all' ? null : Number(stage);
  const samples = (report.samples || []).filter((sample) => sample && typeof sample.success === 'boolean'
    && (selected == null || Number(sample.stage) === selected));
  const finite = Number.isFinite;
  const validTime = (ms) => finite(ms) && Math.abs(ms) <= 8.64e15;
  const measured = (value) => finite(value) && value >= 0;
  const windowMs = measured(report.granularity_ms) && report.granularity_ms > 0 ? report.granularity_ms : 60000;
  const bucket = (value, size) => Math.floor(value / size) * size;
  const end = (sample) => sample.start_ms + sample.elapsed_ms;
  const timed = (sample) => validTime(sample.start_ms) && measured(sample.elapsed_ms) && validTime(end(sample));
  const by = (rows, key) => {
    const groups = new Map();
    for (const row of rows) {
      const name = key(row);
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(row);
    }
    return groups;
  };
  const sum = (items, value) => items.reduce((total, item) => total + value(item), 0);
  const mean = (items, value) => items.length ? sum(items, value) / items.length : null;
  const minimum = (items, value) => items.reduce((n, item) => Math.min(n, value(item)), Infinity);
  const maximum = (items, value) => items.reduce((n, item) => Math.max(n, value(item)), -Infinity);
  const elapsed = (row) => row.elapsed_ms;
  const percentiles = (rows) => {
    const values = rows.slice(-20000).map(elapsed).sort((a, b) => a - b);
    return (percent) => {
      if (!values.length) return null;
      const pos = (values.length + 1) * percent / 100;
      if (pos <= 1) return values[0];
      if (pos >= values.length) return values.at(-1);
      const lower = Math.floor(pos);
      return values[lower - 1] + (pos - lower) * (values[lower] - values[lower - 1]);
    };
  };
  function errorKey(sample) {
    const code = String(sample.code ?? '');
    const message = String(sample.error || '');
    if (code && Number(code) >= 200 && Number(code) < 400) return message || 'Assertion failed';
    if (!code || !message || message === code || message.startsWith(code + '/')) return message || code;
    return code + '/' + message;
  }
  const errorsOf = (rows) => [...by(rows.filter((s) => !s.success), errorKey)]
    .map(([name, failures]) => ({ name, count: failures.length }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  function statistics(label, rows) {
    const allElapsed = rows.length > 0 && rows.every((s) => measured(s.elapsed_ms));
    const n = rows.length;
    const errors = rows.filter((s) => !s.success).length;
    const wallMs = n && rows.every(timed) ? maximum(rows, end) - minimum(rows, (s) => s.start_ms) : 0;
    const byteRate = (key) => wallMs > 0 && rows.every((s) => measured(s[key]))
      ? sum(rows, (s) => s[key]) * 1000 / wallMs / 1024 : null;
    const percentile = percentiles(allElapsed ? rows : []);
    const completeApdex = n > 0 && rows.every((s) => !s.success || measured(s.elapsed_ms));
    const apdexPoints = sum(rows, (s) => {
      if (!s.success || s.elapsed_ms > 1500) return 0;
      return s.elapsed_ms <= 500 ? 1 : 0.5;
    });
    return { transaction: label, sampleCount: n, errorCount: errors, errorPct: n ? 100 * errors / n : null,
      meanResTime: allElapsed ? mean(rows, elapsed) : null, minResTime: allElapsed ? minimum(rows, elapsed) : null,
      maxResTime: allElapsed ? maximum(rows, elapsed) : null, medianResTime: percentile(50), pct1ResTime: percentile(90),
      pct2ResTime: percentile(95), pct3ResTime: percentile(99), throughput: wallMs > 0 ? n * 1000 / wallMs : null,
      receivedKBytesPerSec: byteRate('received_bytes'), sentKBytesPerSec: byteRate('sent_bytes'),
      apdex: completeApdex ? apdexPoints / n : null, topErrors: errorsOf(rows).slice(0, 5) };
  }
  const labels = [...by(samples, (s) => String(s.label ?? ''))].sort(([a], [b]) => a.localeCompare(b));
  const total = statistics('Total', samples);
  if (!Array.isArray(report.samples)) {
    total.sampleCount = null;
    total.errorCount = null;
  }
  const stats = labels.map(([label, rows]) => statistics(label, rows));
  const graphData = Object.fromEntries(['mean', 'percentiles', 'tps', 'codes', 'threads', 'distribution', 'scatter'].map((id) => [id, new Map()]));
  function point(id, series, x, y) {
    const graph = graphData[id];
    if (!graph.has(series)) graph.set(series, []);
    graph.get(series).push([x, y]);
  }
  const knownTimes = samples.filter(timed);
  function addTimeBucket(rows, x) {
    for (const [label, group] of by(rows, (s) => s.label)) {
      point('mean', label, x, mean(group, elapsed));
      for (const [passed, part] of by(group, (s) => s.success)) {
        point('tps', label + (passed ? '-success' : '-failure'), x, part.length * 1000 / windowMs);
      }
    }
    for (const [code, group] of by(rows.filter((s) => s.code != null && s.code !== ''), (s) => String(s.code))) {
      point('codes', code, x, group.length * 1000 / windowMs);
    }
    const successes = rows.filter((s) => s.success);
    if (successes.length) {
      const percentile = percentiles(successes);
      point('percentiles', 'Min', x, minimum(successes, elapsed));
      point('percentiles', 'Max', x, maximum(successes, elapsed));
      for (const p of [50, 90, 95, 99]) point('percentiles', p === 50 ? 'Median' : p + 'th percentile', x, percentile(p));
    }
  }
  by(knownTimes, (s) => bucket(end(s), windowMs)).forEach(addTimeBucket);
  function addDistribution(label, rows) {
    for (const [x, group] of by(rows.filter((s) => measured(s.elapsed_ms)), (s) => bucket(s.elapsed_ms, 100))) {
      point('distribution', label, x, group.length);
    }
  }
  for (const [label, rows] of labels) addDistribution(label, rows);
  function addScatter() {
    const rateGroups = new Map();
    for (const group of by(knownTimes, (s) => bucket(end(s), 1000)).values()) {
      if (!rateGroups.has(group.length)) rateGroups.set(group.length, []);
      const destination = rateGroups.get(group.length);
      for (const row of group) destination.push(row);
    }
    for (const [rate, group] of rateGroups) {
      for (const [passed, part] of by(group, (s) => s.success)) point('scatter', passed ? 'Successes' : 'Failures', rate, percentiles(part)(50));
    }
  }
  addScatter();
  function threadTrace(events, window) {
    const timeline = new Map();
    for (const event of events) timeline.set(event.time_ms, event.count);
    const ordered = [...timeline].sort(([a], [b]) => a - b);
    if (!window) return ordered;
    const trace = ordered.filter(([time]) => time > window.start_ms && (!finite(window.end_ms) || time < window.end_ms));
    const inherited = ordered.findLast(([time]) => time <= window.start_ms);
    if (inherited) trace.unshift([window.start_ms, inherited[1]]);
    if (trace.length && finite(window.end_ms)) trace.push([window.end_ms, trace.at(-1)[1]]);
    return trace;
  }

  function addThreads() {
    const stages = report.stages || [];
    const window = stages.find((s) => s.index === selected);
    const events = (report.threads || []).filter((event) => validTime(event.time_ms) && measured(event.count));
    if (report.thread_events) {
      if (selected == null || window) {
        for (const [time, count] of threadTrace(events, window)) point('threads', 'Users', time, count);
      }
      return;
    }
    const threads = selected == null ? events : events.filter((event) => window && event.time_ms >= window.start_ms
      && (!finite(window.end_ms) || event.time_ms < window.end_ms));
    for (const [time, group] of by(threads, (e) => bucket(e.time_ms, windowMs))) {
      point('threads', 'Users', time, mean(group, (e) => e.count));
    }
  }
  addThreads();
  const graphs = Object.fromEntries(Object.entries(graphData).map(([id, series]) => [id, [...series]
    .sort(([a], [b]) => String(a).localeCompare(String(b)))
    .map(([label, data]) => ({ label, data: data.sort(([a], [b]) => a - b) }))]));
  const errors = errorsOf(samples).map(({ name, count }) => [name, count, count * 100 / total.errorCount, count * 100 / total.sampleCount]);
  return { stage: selected, total, statistics: stats, errors, graphs, granularity_ms: windowMs, thread_events: report.thread_events === true };
}

function createLoadRenderer(aggregate) {
  function element(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    for (const child of children.flat(Infinity)) if (child != null) node.append(child);
    return node;
  }
  function svg(tag, attrs, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text != null) node.textContent = text;
    return node;
  }
  const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
  const precise = new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 3 });
  const compact = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumSignificantDigits: 3 });
  const time = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const format = (n) => Number.isFinite(n) ? (n !== 0 && Math.abs(n) < 0.01 ? precise : number).format(n) : '—';
  const percent = (n) => Number.isFinite(n) ? format(n) + '%' : '—';
  const friendly = (name) => ({ Min: '最小值', Max: '最大值', Median: '中位数', '90th percentile': 'P90',
    '95th percentile': 'P95', '99th percentile': 'P99', Successes: '成功', Failures: '失败', Users: '活动线程' })[name] || name;
  function seriesLabel(id, name) {
    if (id === 'tps') return name.replace(/-success$/, ' · 成功').replace(/-failure$/, ' · 失败');
    return ['threads', 'percentiles', 'scatter'].includes(id) ? friendly(name) : name;
  }
  function table(name, titles, rows) {
    return element('div', { class: 'load-dash-scroll', tabindex: '0', role: 'region', 'aria-label': name + '表格' },
      element('table', { class: 'table', 'aria-label': name }, element('thead', {}, element('tr', {}, titles.map((title) =>
        element('th', { scope: 'col' }, title)))), element('tbody', {}, rows.map((row) => element('tr', {},
        row.map((value, index) => index ? element('td', {}, value ?? '—') : element('th', { scope: 'row' }, value ?? '—')))))));
  }
  function section(name, body) {
    return element('section', { class: 'card load-dash-panel' }, element('h2', {}, name), body);
  }
  const empty = (message = '暂无可用数据') => element('p', { class: 'sub load-dash-empty' }, message);
  function summaries(model) {
    const { total } = model;
    const all = [total, ...model.statistics];
    const label = (row) => row === total ? '总计' : row.transaction;
    const metrics = ['meanResTime', 'minResTime', 'maxResTime', 'medianResTime', 'pct1ResTime', 'pct2ResTime', 'pct3ResTime',
      'throughput', 'receivedKBytesPerSec', 'sentKBytesPerSec'];
    const passed = total.sampleCount == null ? null : total.sampleCount - total.errorCount;
    let proportions = null;
    if (total.sampleCount > 0) {
      const success = element('i', { class: 'load-share-pass' });
      success.style.flex = String(passed);
      const failed = element('i', { class: 'load-share-fail' });
      failed.style.flex = String(total.errorCount);
      proportions = element('div', { class: 'load-request-share', 'aria-hidden': 'true' }, success, failed);
    }
    return [
      section('请求汇总', [proportions, table('请求汇总', ['样本数', '成功', '失败', '错误率'],
        [[format(total.sampleCount), format(passed), format(total.errorCount), percent(total.errorPct)]])]),
      section('统计', table('统计', ['标签', '样本数', '失败数', '错误率', '平均值 (ms)', '最小值 (ms)', '最大值 (ms)',
        '中位数 (ms)', 'P90 (ms)', 'P95 (ms)', 'P99 (ms)', '吞吐量 (次/s)', '接收 KB/s', '发送 KB/s'],
      all.map((r) => [label(r), format(r.sampleCount), format(r.errorCount), percent(r.errorPct), ...metrics.map((key) => format(r[key]))]))),
      section('APDEX', table('APDEX', ['标签', 'APDEX', '满意阈值 (ms)', '容忍阈值 (ms)'],
        all.map((r) => [label(r), r.apdex == null ? '—' : r.apdex.toFixed(3), '500', '1,500']))),
    ];
  }
  function errorTables(model) {
    const topRows = [model.total, ...model.statistics.filter((r) => r.errorCount > 0)];
    return [
      section('错误汇总', table('错误汇总', ['错误类型', '错误数', '占错误比例', '占样本比例'],
        model.errors.map(([key, count, part, share]) => [key || '—', format(count), percent(part), percent(share)]))),
      section('每个采样器的前五项错误', table('每个采样器的前五项错误', ['标签', '样本数', '失败数', ...Array.from({ length: 5 }, (_, i) =>
        ['错误 ' + (i + 1), '次数']).flat()], topRows.map((row) => [
        row === model.total ? '总计' : row.transaction, format(row.sampleCount), format(row.errorCount),
        ...Array.from({ length: 5 }, (_, i) => row.topErrors[i] ? [row.topErrors[i].name || '—', format(row.topErrors[i].count)] : ['—', '—']).flat(),
      ]))),
    ];
  }
  const charts = {
    mean: ['响应时间趋势', '时间', '平均响应时间 (ms)'], percentiles: ['响应时间百分位趋势', '时间', '响应时间 (ms)'],
    tps: ['每秒事务数', '时间', '事务数/s'], threads: ['活动线程数', '时间', '活动线程数'],
    codes: ['每秒响应码数', '时间', '响应数/s'], distribution: ['响应时间分布', '响应时间区间 (ms)', '样本数'],
    scatter: ['响应时间与每秒请求数', '请求数/s', '响应时间 (ms)'],
  };
  const palette = ['var(--accent-strong)', 'var(--good-ink)', 'var(--bad-ink)', 'var(--warn-ink)', 'var(--ink)', 'var(--ink-3)'];
  function chartFrame(id, series) {
    let minX = Infinity, maxX = -Infinity, maxY = 0;
    for (const group of series) for (const [x, y] of group.data) {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    }
    if (id === 'distribution') maxX += 100;
    const xAt = (v) => 62 + (maxX === minX ? 0.5 : (v - minX) / (maxX - minX)) * 524;
    const yAt = (v) => 154 - v / (maxY || 1) * 124;
    const formatX = ['distribution', 'scatter'].includes(id) ? format : (x) => time.format(x);
    const node = svg('svg', { class: 'load-chart-svg', viewBox: '0 0 620 198', role: 'img', 'aria-label': charts[id][0] });
    node.append(svg('title', {}, charts[id][0]), svg('text', { x: 62, y: 16 }, charts[id][2]));
    for (let tick = 0; tick <= 4; tick++) {
      const value = (maxY || 1) * tick / 4;
      const y = yAt(value);
      node.append(svg('line', { x1: 62, x2: 586, y1: y, y2: y, class: 'load-grid' }),
        svg('text', { x: 55, y: y + 4, 'text-anchor': 'end' }, value >= 10000 ? compact.format(value) : format(value)));
    }
    const ticks = minX === maxX ? [minX] : [minX, maxX];
    ticks.forEach((v, i) => { node.append(svg('text', { x: xAt(v), y: 172, 'text-anchor': ticks.length === 1 ? 'middle' : i ? 'end' : 'start' }, formatX(v))); });
    node.append(svg('text', { x: 324, y: 192, 'text-anchor': 'middle' }, charts[id][1]));
    return { node, xAt, yAt, formatX };
  }
  function drawSeries(frame, id, group, index, count, interval, step) {
    const { xAt, yAt, node } = frame;
    const color = palette[index % palette.length];
    const scatter = id === 'scatter', histogram = id === 'distribution';
    if (!scatter && !histogram) {
      const path = group.data.map(([x, y], i) => {
        const px = xAt(x).toFixed(2), py = yAt(y).toFixed(2);
        if (step && i) return 'H' + px + ' V' + py;
        return (!i || x - group.data[i - 1][0] > interval ? 'M' : 'L') + px + ',' + py;
      }).join(' ');
      node.append(svg('path', { d: path, fill: 'none', stroke: color, 'stroke-width': 2 }));
    }
    for (const [x, y] of group.data) {
      let mark;
      if (histogram) {
        const barWidth = (xAt(x + 100) - xAt(x)) / count;
        mark = svg('rect', { x: xAt(x) + barWidth * index, width: Math.max(barWidth * 0.9, 0.3), y: yAt(y), height: yAt(0) - yAt(y), fill: color });
      } else {
        mark = svg('circle', { cx: xAt(x), cy: yAt(y), r: scatter ? 4 : 2.4, fill: color });
      }
      mark.append(svg('title', {}, seriesLabel(id, group.label) + ' · ' + frame.formatX(x) + ' · ' + format(y)));
      node.append(mark);
    }
  }
  function chart(id, model) {
    const [name, xTitle, yTitle] = charts[id];
    const series = model.graphs[id];
    const panel = section(name, null);
    panel.dataset.chart = id;
    if (!series.length) { panel.append(empty()); return panel; }
    const frame = chartFrame(id, series);
    series.forEach((group, i) => { drawSeries(frame, id, group, i, series.length, model.granularity_ms, id === 'threads' && model.thread_events); });
    const legend = element('ul', { class: 'load-chart-legend' }, series.map((group, i) => {
      const swatch = element('i', { 'aria-hidden': 'true' });
      swatch.style.background = palette[i % palette.length];
      return element('li', {}, swatch, seriesLabel(id, group.label));
    }));
    const rows = series.flatMap((group) => group.data.map(([x, y]) => [seriesLabel(id, group.label), frame.formatX(x), format(y)]));
    panel.append(frame.node, legend, element('details', { class: 'more' }, element('summary', {}, '查看图表数据'),
      table(name + '数据', ['系列', xTitle, yTitle], rows)));
    return panel;
  }
  return function render(run, options = {}) {
    const model = aggregate(run, options);
    const stress = run.load?.mode === 'stress';
    const order = stress ? ['threads', 'tps', 'codes', 'percentiles', 'scatter'] : ['mean', 'percentiles', 'tps', 'threads', 'distribution'];
    const graphs = element('div', { class: 'load-chart-panels' }, order.map((id) => chart(id, model)));
    const tables = summaries(model);
    return element('div', { class: 'load-dashboard', 'data-stage': model.stage ?? 'all' }, stress ? [graphs, tables] : [tables, graphs],
      errorTables(model));
  };
}

export const renderLoadDashboard = createLoadRenderer(aggregateLoadReport);

export function loadDashboardScript() {
  return 'const renderLoadDashboard = (' + createLoadRenderer.toString() + ')(' + aggregateLoadReport.toString() + ');';
}
