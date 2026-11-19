import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { aggregateLoadReport } from '../skills/autoskill/app/ui/js/pages/load-dashboard.js';

const read = (file) => readFileSync(new URL('./fixtures/jmeter-5.6.3/' + file, import.meta.url), 'utf8');
const official = JSON.parse(read('expected.json'));
const rows = read('samples.csv').trim().split(/\r?\n/).slice(1).map((line) => line.split(','));
const samples = rows.map(([time, elapsed, label, code, message, , , ok, failure, received, sent]) =>
  ({ label, kind: 'api', start_ms: Number(time), elapsed_ms: Number(elapsed), success: ok === 'true',
    code, error: failure || message, received_bytes: Number(received), sent_bytes: Number(sent) }));
const threads = rows.map((row) => ({ time_ms: Number(row[0]) + Number(row[1]), count: Number(row[12]) }));
const report = aggregateLoadReport({ load_report: { samples, threads } });
const close = (actual, expected) => {
  assert.equal(typeof actual, 'number');
  assert.ok(Math.abs(actual - expected) < Math.max(1e-6, Math.abs(expected) * 1e-8), actual + ' != ' + expected);
};

test('statistics, APDEX and errors reproduce the offline JMeter report', () => {
  for (const row of [report.total, ...report.statistics]) {
    for (const [key, value] of Object.entries(official.statistics[row.transaction])) {
      if (key !== 'transaction') close(row[key], value);
    }
    close(row.apdex, official.apdex[row.transaction]);
  }
  assert.equal(report.errors.length, official.errors.length);
  official.errors.forEach(([key, ...values], i) => {
    assert.equal(report.errors[i][0], key);
    values.forEach((value, j) => { close(report.errors[i][j + 1], value); });
  });
  assert.deepEqual(report.total.topErrors, [{ name: '503/Service Unavailable', count: 2 }, { name: 'Expected stock', count: 1 }]);
});

test('plotted points match JMeter time, throughput, code, distribution, thread and scatter graphs', () => {
  const graphs = { mean: 'responseTimesOverTime', percentiles: 'responseTimePercentilesOverTime',
    tps: 'transactionsPerSecond', codes: 'codesPerSecond', threads: 'activeThreadsOverTime',
    scatter: 'responseTimeVsRequest', distribution: 'responseTimeDistribution' };
  for (const [id, name] of Object.entries(graphs)) {
    const reference = official.graphs[name];
    const data = report.graphs[id];
    assert.deepEqual(data.map((s) => s.label).sort(), Object.keys(reference).sort(), id);
    for (const series of data) {
      const expected = reference[series.label];
      assert.equal(series.data.length, expected.length, name);
      series.data.forEach(([x, y], i) => { close(x, expected[i][0]); close(y, expected[i][1]); });
    }
  }
});

test('missing timestamps and I/O cannot become aggregate rates or fabricated zeroes', () => {
  assert.equal(aggregateLoadReport({}).total.sampleCount, null);
  assert.equal(aggregateLoadReport({ load_report: { samples: [] } }).total.sampleCount, 0);
  const rows = [{ ...samples[0], stage: 1 }, { ...samples[1], stage: 2, received_bytes: null, sent_bytes: 0 }];
  const input = { load_report: { samples: rows, stages: [], threads: [] } };
  assert.equal(aggregateLoadReport(input).total.receivedKBytesPerSec, null);
  const selected = aggregateLoadReport(input, { stage: '2' });
  assert.equal(selected.total.sampleCount, 1);
  assert.equal(selected.total.sentKBytesPerSec, 0);
  rows[1].start_ms = null;
  const { total, graphs } = aggregateLoadReport(input, { stage: 2 });
  assert.equal(total.meanResTime, rows[1].elapsed_ms);
  assert.equal(total.throughput, null);
  assert.equal(total.errorPct, 0);
  assert.equal(graphs.mean.length, 0);
  assert.equal(aggregateLoadReport(input, { stage: 3 }).total.apdex, null);
});

test('percentiles use the last 20000 samples and means retain the entire group', () => {
  const row = { label: 'API', success: true, kind: 'api' };
  const rows = Array.from({ length: 20001 }, (_, i) => ({ ...row, elapsed_ms: i ? 42 : 3000 }));
  const { total } = aggregateLoadReport({ load_report: { samples: rows } });
  assert.equal(total.pct3ResTime, 42);
  assert.equal(total.maxResTime, 3000);
  assert.ok(total.meanResTime > 42);
});

test('stage filtering follows dispatched samples and measured thread windows', () => {
  const rows = [
    { stage: 1, start_ms: 900, elapsed_ms: 200, success: true, kind: 'api', label: 'A', code: 200 },
    { stage: 2, start_ms: 1100, elapsed_ms: 400, success: false, kind: 'api', label: 'A', code: 502, error: 'Bad Gateway' },
  ];
  const raw = { load_report: { granularity_ms: 1000, samples: rows,
    stages: [{ index: 1, start_ms: 0, end_ms: 1000 }, { index: 2, start_ms: 1000, end_ms: 2000 }],
    threads: [{ time_ms: 900, count: 2 }, { time_ms: 1000, count: 6 }] } };
  const early = aggregateLoadReport(raw, { stage: 1 });
  const late = aggregateLoadReport(raw, { stage: 2 });
  assert.deepEqual(early.graphs.threads[0].data, [[0, 2]]);
  assert.deepEqual(late.graphs.threads[0].data, [[1000, 6]]);
  assert.deepEqual(early.graphs.mean[0].data, [[1000, 200]]);
  assert.deepEqual(late.graphs.codes, [{ label: '502', data: [[1000, 1]] }]);
  assert.equal(early.errors.length, 0);
  assert.equal(late.errors[0][0], '502/Bad Gateway');
  assert.equal(late.graphs.percentiles.length, 0);
  assert.equal(late.total.apdex, 0);
});

test('thread transitions retain observed levels and the count already active at a stage boundary', () => {
  const run = { load_report: { thread_events: true, samples: [],
    stages: [{ index: 2, start_ms: 2000, end_ms: 3000 }],
    threads: [{ time_ms: 900, count: 1 }, { time_ms: 900, count: 2 },
      { time_ms: 2300, count: 4 }, { time_ms: 3000, count: 0 }] } };
  const all = aggregateLoadReport(run);
  const stage = aggregateLoadReport(run, { stage: 2 });
  assert.deepEqual(all.graphs.threads[0].data, [[900, 2], [2300, 4], [3000, 0]]);
  assert.deepEqual(stage.graphs.threads[0].data, [[2000, 2], [2300, 4], [3000, 4]]);
});
