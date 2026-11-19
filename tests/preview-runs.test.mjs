import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { advanceRuns, runsApi } from '../preview/runs-api.js';

const examples = JSON.parse(await readFile(new URL('../preview/fixtures.json', import.meta.url), 'utf8'));
function environment() {
  const fixtures = structuredClone(examples);
  const db = { runs: fixtures['/runs'].items.map((r) => structuredClone(fixtures['/runs/' + r.id])) };
  const request = (url, method = 'GET', body = {}) => {
    const { pathname: path, searchParams: params } = new URL(url, 'https://example.invalid/');
    return runsApi({ path, method, body, params, db, fixtures });
  };
  return { db, request, project_id: fixtures['/results'].items.find((r) => r.state.outcome === 'fail').project_id };
}

test('demo history filters canonical outcomes and dashboard reflects simulated runs and deletions', () => {
  const { db, request, project_id } = environment();
  const before = request('/dashboard');
  for (const status of ['Pass', 'Fail', 'NotRun']) {
    const expected = examples['/results'].items.filter((r) => r.history_status === status);
    assert.equal(request('/results?status=' + status).total, expected.length);
  }
  assert.equal(request('/results?start=2400-01-01').total, 0);
  assert.equal(request('/runs?projects=another-project').total, 0);
  const selected = request('/results?status=Pass&projects=' + project_id).items[0].case_id;
  const { runs } = request('/runs', 'POST', { items: [{ project_id, case_ids: [selected] }] });
  const path = '/runs/' + runs[0].id;
  assert.equal(request(path).status, 'Queued');
  const start = Date.parse(request(path).created_at);
  assert.equal(advanceRuns(db, start + 700), true);
  assert.equal(request(path).status, 'Running');
  assert.equal(advanceRuns(db, start + 10000), true);
  assert.equal(request(path).status, 'Pass');
  assert.equal(request(path).state.report, 'ready');
  assert.equal(request('/dashboard').summary.results.passed, before.summary.results.passed + 1);
  assert.equal(request('/dashboard').summary.runs.latest_batch.pass_rate, 1);
  assert.equal(request(path, 'DELETE').deleted[0].run_id, runs[0].id);
  assert.deepEqual(request('/dashboard').summary.results, before.summary.results);
});

test('cancelled runs distinguish work already started from untouched targets; retry selects failures', () => {
  const { db, request, project_id } = environment();
  const options = request('/load-tests/options?projects=' + project_id);
  const case_ids = options.items.slice(0, 2).map((r) => r.case_id);
  const path = '/runs/' + request('/runs', 'POST', { project_id, case_ids }).runs[0].id;
  advanceRuns(db, Date.parse(request(path).created_at) + 650);
  const cancelled = request(path + '/cancel', 'POST');
  assert.equal(cancelled.status, 'Cancelled');
  assert.deepEqual(cancelled.state_counts, { pass: 0, fail: 1, not_run: 1 });
  const retry = request(path + '/rerun', 'POST', { failed_only: true });
  assert.equal(retry.total, 1);
  assert.deepEqual(request('/runs/' + retry.id).case_ids, case_ids.slice(0, 1));
  const queued = request('/runs/' + retry.id + '/cancel', 'POST');
  assert.deepEqual(queued.state_counts, { pass: 0, fail: 0, not_run: 1 });
});

test('load simulation honors iterations and remains separate from dashboard replay statistics', () => {
  const { db, request, project_id } = environment();
  const initial = request('/dashboard').summary.results;
  const case_ids = request('/load-tests/options?projects=' + project_id).items.slice(0, 2).map((c) => c.case_id);
  const body = { items: [{ project_id, case_ids }], concurrency: 2, iterations: 3, ramp_up_seconds: 0 };
  const { runs } = request('/load-tests', 'POST', body);
  const run = request('/load-tests/' + runs[0].id);
  advanceRuns(db, Date.parse(run.created_at) + 11000);
  assert.equal(run.done, 6);
  assert.equal(run.metrics.latency_samples, 6);
  assert.deepEqual(run.results.map((r) => r.detail.load_iteration), [1, 1, 2, 2, 3, 3]);
  assert.deepEqual(request('/dashboard').summary.results, initial);
  assert.equal(request('/load-tests').items[0].id, run.id);
});

test('batch pass rate covers each accepted project and skipped items do not leave partial runs', () => {
  const { db, request, project_id } = environment();
  const cases = request('/load-tests/options?projects=' + project_id).items;
  const passing = request('/results?status=Pass&projects=' + project_id).items[0].case_id;
  const failing = request('/results?status=Fail&projects=' + project_id).items[0].case_id;
  db.caseLibrary.projects.push({ ...db.caseLibrary.projects[0], id: 'other-demo', name: '第二个示例项目' });
  db.caseLibrary.items.push({ ...cases.find((c) => c.case_id === passing), project_id: 'other-demo' });
  const created = request('/runs', 'POST', { items: [
    { project_id, case_ids: [failing] }, { project_id: 'other-demo', case_ids: [passing] }, { project_id: 'missing' },
  ] });
  assert.equal(created.runs.length, 2);
  assert.equal(created.skipped[0].project_id, 'missing');
  assert.ok(created.batch_id);
  advanceRuns(db, Date.now() + 10000);
  assert.deepEqual(request('/dashboard').summary.runs.latest_batch, { total: 2, passed: 1, failed: 1, not_run: 0, pass_rate: 0.5 });
  assert.deepEqual(request('/dashboard?projects=other-demo').summary.runs.latest_batch, { total: 1, passed: 1, failed: 0, not_run: 0, pass_rate: 1 });
});

test('large simulated loads keep only case templates and generate any detail page with whole-run counts', () => {
  const { db, request, project_id } = environment();
  const passed = request('/results?status=Pass&projects=' + project_id).items[0].case_id;
  const failed = request('/results?status=Fail&projects=' + project_id).items[0].case_id;
  const iterations = 1000003;
  const body = { items: [{ project_id, case_ids: [passed, failed] }], concurrency: 5, iterations, ramp_up_seconds: 1 };
  const path = '/load-tests/' + request('/load-tests', 'POST', body).runs[0].id;
  const raw = db.runs[0];
  assert.equal(raw.results.length, 0);
  assert.equal(raw.load_templates.length, 2);
  const total = iterations * 2;
  assert.equal(request(path).not_run, total);
  advanceRuns(db, Date.parse(raw.created_at) + 9000);
  const first = request(path);
  assert.deepEqual(first.result_page, { offset: 0, end: 200, total });
  assert.equal(first.passed, iterations);
  assert.equal(first.failed, iterations);
  assert.equal(first.metrics.latency_samples, total);
  assert.equal(first.metrics.error_rate, 0.5);
  const last = request(path + '?detail_offset=' + (total - 1));
  assert.equal(last.results.length, 6);
  assert.equal(last.results.at(-1).detail.load_iteration, iterations);
  assert.equal(last.results.at(-1).case_id, failed);
  assert.equal(last.results.at(-1).history_status, 'Fail');
  assert.match(last.project_name, /2000001–2000006/);
  assert.equal(raw.results.length, 0);
});

test('cancelling a compact simulation counts the interrupted target and preserves pending targets on all pages', () => {
  const { db, request, project_id } = environment();
  const selected = request('/results?status=Pass&projects=' + project_id).items[0].case_id;
  const { runs } = request('/load-tests', 'POST', { project_id, case_ids: [selected], iterations: 100000,
    concurrency: 4, ramp_up_seconds: 0 });
  const path = '/load-tests/' + runs[0].id;
  advanceRuns(db, Date.parse(db.runs[0].created_at) + 800);
  const previous = db.runs[0].done;
  request(path + '/cancel', 'POST');
  const detail = request(path + '?detail_offset=' + previous);
  const interrupted = detail.results[previous - detail.result_page.offset];
  assert.equal(interrupted.reason, '模拟执行已取消');
  assert.equal(interrupted.history_status, 'Fail');
  assert.deepEqual(detail.state_counts, { pass: previous, fail: 1, not_run: detail.total - previous - 1 });
  assert.equal(request(path + '?detail_offset=99000').results[0].history_status, 'NotRun');
});
