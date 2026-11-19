import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { casesApi } from '../preview/cases-api.js';
import { currentData, getData, restoreData, startSession } from '../preview/store.js';
import { advanceRuns, runsApi, seedLoadRuns } from '../preview/runs-api.js';

const examples = JSON.parse(await readFile(new URL('../preview/fixtures.json', import.meta.url), 'utf8'));
function environment() {
  const fixtures = structuredClone(examples);
  const db = { runs: fixtures['/runs'].items.map((r) => structuredClone(fixtures['/runs/' + r.id])) };
  const request = (url, method = 'GET', body = {}) => {
    const { pathname: path, searchParams: params } = new URL(url, 'https://example.invalid/');
    const ctx = { path, method, body, params, db, fixtures };
    return runsApi(ctx) ?? casesApi(ctx);
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
  const definition = db.caseLibrary.definitions[project_id + '/' + selected].case;
  const original = structuredClone(definition);
  definition.title += '（运行后编辑）';
  assert.deepEqual(request(path).report_definitions[selected], original);
  assert.equal(request('/runs').items[0].report_definitions, undefined);
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
  const path = '/load-tests/' + runs[0].id;
  advanceRuns(db, Date.parse(request(path).created_at) + 11000);
  const run = request(path);
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
  db.caseLibrary.projects.push({ ...db.caseLibrary.projects.find((p) => p.id === project_id), id: 'other-demo', name: '第二个示例项目' });
  db.caseLibrary.items.push({ ...cases.find((c) => c.case_id === passing), project_id: 'other-demo' });
  db.caseLibrary.definitions['other-demo/' + passing] = structuredClone(db.caseLibrary.definitions[project_id + '/' + passing]);
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
  assert.equal(request(path).metrics.p95_ms, null);
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

test('browser simulation freezes actual UI steps and pairs a scrubbed API request with its offline response', () => {
  const { db, request } = environment();
  const api = '/projects/stock-room/cases/ST-API-002';
  const ui = '/projects/stock-room/cases/ST-UI-001';
  const edit = (path, change) => {
    const { case: definition, content_revision } = request(path.replace('/projects/', '/case-definitions/').replace('/cases/', '/'));
    const draft = structuredClone(definition);
    change(draft);
    assert.equal(request(path, 'PUT', { case: draft, expected_content_revision: content_revision }).changed, true);
  };
  edit(api, (d) => { d.headers = { Authorization: 'demo-hidden', 'X-Correlation': 'demo-001' }; });
  const body = { project_id: 'stock-room', case_ids: ['ST-UI-001', 'ST-API-002'] };
  const first = '/runs/' + request('/runs', 'POST', body).runs[0].id;
  assert.deepEqual(request(first).results[0].detail.steps.map((s) => s.status), ['not_run', 'not_run']);
  assert.equal(request(first).results[1].detail.response, undefined);
  assert.equal(request(first).simulation_cases, undefined);
  edit(ui, (d) => { d.extensions.automation.assertions[0].name = '新版搜索框'; });
  edit(api, (d) => { d.assertions.json_equals['/threshold'] = 10; });
  const next = '/runs/' + request('/runs', 'POST', body).runs[0].id;
  advanceRuns(db, Date.parse(request(next).created_at) + 9000);
  const [uiRow, apiRow] = request(first).results;
  assert.deepEqual(uiRow.detail.steps.map((s) => [s.text, s.status, s.check]), [
    ['打开 https://stock.example.invalid/stock', 'done', false], ['测试标识 stock-search 可见', 'done', true],
  ]);
  assert.equal(uiRow.detail.steps.reduce((s, step) => s + step.ms, 0), uiRow.duration_ms);
  const detail = apiRow.detail;
  assert.equal(apiRow.history_status, 'Fail');
  assert.equal(detail.target_started, true);
  assert.deepEqual(detail.request.headers, { Authorization: '<已隐藏>', 'X-Correlation': 'demo-001' });
  assert.deepEqual([detail.request.method, detail.request.url, detail.response.status, detail.response.bytes],
    ['GET', 'https://stock.example.invalid/api/stock', 200, 31]);
  assert.equal(JSON.parse(detail.response.body).threshold, 10);
  assert.deepEqual(detail.assertions.at(-1), { name: '字段 /threshold', expected: 20, actual: 10, passed: false, scope: 'business' });
  assert.deepEqual(detail.attempts.map((a) => [a.request, a.response, a.simulated, a.duration_ms]), [
    [detail.request, detail.response, true, apiRow.duration_ms],
  ]);
  assert.equal(request(next).results[0].detail.steps.at(-1).text, '新版搜索框');
  assert.equal(request(next).results[1].state.outcome, 'pass');
  assert.equal(request('/runs').items[0].simulation_cases, undefined);
});

test('queued cancellation and interrupted exchanges never supply an unobserved simulated response', () => {
  const { db, request } = environment();
  const launch = () => '/runs/' + request('/runs', 'POST', { project_id: 'stock-room', case_ids: ['ST-API-001', 'ST-UI-001'] }).runs[0].id;
  const queued = request(launch() + '/cancel', 'POST').results;
  assert.deepEqual(queued.map((r) => r.detail.target_started), [false, false]);
  assert.equal(queued[0].detail.request, undefined);
  assert.equal(queued[0].detail.response, undefined);
  assert.equal(queued[0].detail.attempts, undefined);
  assert.deepEqual(queued[1].detail.steps.map((s) => [s.status, s.ms]), [['not_run', undefined], ['not_run', undefined]]);
  const path = launch();
  advanceRuns(db, Date.parse(request(path).created_at) + 650);
  const [interrupted, untouched] = request(path + '/cancel', 'POST').results;
  assert.equal(interrupted.state.outcome, 'fail');
  assert.equal(interrupted.detail.attempts[0].request.url, 'https://stock.example.invalid/api/stock');
  assert.equal(interrupted.detail.attempts[0].response, undefined);
  assert.equal(interrupted.detail.assertions, undefined);
  assert.equal(untouched.detail.target_started, false);
});

test('compact load pages distinguish unresolved targets from completed offline exchanges in all counts', () => {
  const { db, request } = environment();
  const body = { project_id: 'stock-room', case_ids: ['ST-API-001', 'ST-API-003'], iterations: 103, concurrency: 2, ramp_up_seconds: 0 };
  const path = '/load-tests/' + request('/load-tests', 'POST', body).runs[0].id;
  advanceRuns(db, Date.parse(request(path).created_at) + 10000);
  const page = request(path + '?detail_offset=200');
  assert.deepEqual(page.state_counts, { pass: 103, fail: 0, not_run: 103 });
  assert.equal(page.done, 206);
  assert.equal(page.metrics.request_count, 103);
  assert.equal(page.metrics.latency_samples, 103);
  for (const row of page.results) {
    if (row.case_id === 'ST-API-003') {
      assert.match(row.reason, /未解析变量/);
      assert.equal(row.started_at, null);
      assert.equal(row.detail.target_started, false);
      assert.equal(row.detail.request, undefined);
      assert.equal(row.detail.response, undefined);
      assert.equal(row.detail.attempts, undefined);
    } else {
      assert.deepEqual(row.detail.response, row.detail.attempts[0].response);
      assert.equal(row.detail.request.method, 'GET');
    }
  }
});

test('load mode eligibility is limited to API targets while previous UI executions remain readable', () => {
  const { db, request } = environment();
  const options = request('/load-tests/options');
  assert.ok(options.items.length > 1);
  assert.ok(options.items.every((c) => c.kind === 'api' && c.replayable));
  assert.ok(!options.projects.some((project) => project.id === 'runoob'));
  const body = { mode: 'performance', concurrency: 3, target_requests: 5, case_ids: ['ST-UI-001'], project_id: 'stock-room' };
  assert.equal(request('/load-tests', 'POST', body).status, 422);
  assert.equal(request('/runs', 'POST', body).runs.length, 1);
  const previous = structuredClone(examples['/runs/run-83be44ec7c93']);
  Object.assign(previous, { id: 'old-ui-load', family: 'load', load: { concurrency: 1, iterations: 1 } });
  db.runs.push(previous);
  assert.ok(request('/load-tests?mode=performance').items.some((r) => r.id === previous.id));
  assert.ok(!request('/load-tests?mode=stress').items.some((r) => r.id === previous.id));
  const view = request('/load-tests/' + previous.id);
  assert.equal(view.load_report.samples[0].kind, 'ui');
  assert.equal(view.load_report.samples[0].elapsed_ms, previous.results[0].duration_ms);
  assert.equal(view.load_report.samples[0].code, undefined);
  assert.equal(view.load_report.threads.length, 0);
});

test('local API performance samples stop at the first bound and carry only available exchange data', () => {
  const { db, request } = environment();
  const base = { project_id: 'stock-room', case_ids: ['ST-API-001', 'ST-API-002'], mode: 'performance', concurrency: 1 };
  assert.equal(request('/load-tests', 'POST', base).status, 422);
  const launch = (duration_seconds, target_requests) => {
    const id = request('/load-tests', 'POST', { ...base, duration_seconds, target_requests }).runs[0].id;
    advanceRuns(db, Date.now() + 10000);
    return request('/load-tests/' + id);
  };
  const timed = launch(1, 999);
  assert.equal(timed.load_report.stop_reason, 'duration');
  assert.equal(timed.load_report.samples.length, 1);
  const capped = launch(99, 3);
  const { samples, threads, stages } = capped.load_report;
  assert.equal(capped.load_report.stop_reason, 'target_requests');
  assert.equal(capped.engine, 'locust');
  assert.equal(capped.load_report.granularity_ms, 1000);
  assert.equal(capped.simulated, true);
  assert.equal(capped.load.iterations, undefined);
  assert.deepEqual(samples.map((s) => s.success), [true, false, true]);
  assert.deepEqual(samples.map((s) => s.label), ['ST-API-001', 'ST-API-002', 'ST-API-001']);
  assert.equal(capped.metrics.request_count, 3);
  assert.equal(capped.metrics.p95_ms, Math.max(...samples.map((s) => s.elapsed_ms)));
  assert.ok(samples.every((s) => s.kind === 'api' && s.code === '200' && s.received_bytes > 0));
  assert.match(samples[1].error, /threshold/);
  assert.equal(samples[0].error, undefined);
  assert.equal(stages.length, 0);
  assert.equal(threads.at(-1).count, 0);
  assert.equal(Math.max(...threads.map((t) => t.count)), 1);
  assert.equal(capped.results.length, samples.length);
});

test('stress windows, worker counts and two local baseline records agree with the selected mode', () => {
  const { db, request } = environment();
  seedLoadRuns(db, examples);
  const performance = request('/load-tests?mode=performance').items;
  const stress = request('/load-tests?mode=stress').items;
  assert.deepEqual(performance.map((r) => r.id), ['load-demo-performance']);
  assert.deepEqual(stress.map((r) => r.id), ['load-demo-stress']);
  assert.equal(performance[0].load_report, undefined);
  const detail = request('/load-tests/load-demo-stress');
  const { samples, stages, threads } = detail.load_report;
  assert.deepEqual(stages.map((s) => s.concurrency), [2, 4, 6]);
  assert.deepEqual(stages.map((s) => s.index), [1, 2, 3]);
  assert.deepEqual(stages.map((s) => samples.filter((r) => r.stage === s.index).length), [12, 24, 36]);
  for (const stage of stages) {
    assert.equal(stage.end_ms - stage.start_ms, 6000);
    assert.ok(samples.filter((r) => r.stage === stage.index).every((r) => r.start_ms >= stage.start_ms && r.start_ms + r.elapsed_ms <= stage.end_ms));
    assert.equal(Math.max(...threads.filter((p) => p.time_ms >= stage.start_ms && p.time_ms < stage.end_ms).map((p) => p.count)), stage.concurrency);
  }
  assert.equal(detail.passed, 36);
  assert.equal(detail.failed, 36);
  assert.equal(request('/load-tests/load-demo-performance').load_report.samples.length, 54);
  assert.equal(request('/dashboard').summary.results.executions, examples['/results'].total);
});

test('request targets count only simulated HTTP exchanges and do not turn missing evidence into samples', () => {
  const { db, request } = environment();
  const plan = { mode: 'performance', project_id: 'stock-room', case_ids: ['ST-API-003', 'ST-API-001'], concurrency: 1, target_requests: 2 };
  const id = request('/load-tests', 'POST', plan).runs[0].id;
  advanceRuns(db, Date.now() + 10000);
  const report = request('/load-tests/' + id);
  assert.equal(report.total, 4);
  assert.equal(report.load_report.samples.length, 2);
  assert.equal(report.load_report.stop_reason, 'target_requests');
  assert.equal(report.state_counts.not_run, 2);
  const unresolved = request('/load-tests', 'POST', { ...plan, case_ids: ['ST-API-003'] }).runs[0].id;
  advanceRuns(db, Date.now() + 10000);
  const empty = request('/load-tests/' + unresolved);
  assert.equal(empty.load_report.samples.length, 0);
  assert.equal(empty.load_report.stop_reason, 'no_samples');
  assert.equal(empty.metrics.error_rate, null);
  assert.equal(empty.metrics.p95_ms, null);
});

test('load plans requiring a UI dependency, resource request, or missing case cannot be scheduled', () => {
  const { db, request } = environment();
  request('/load-tests/options');
  const library = db.caseLibrary.definitions;
  library['stock-room/ST-API-001'].case.extensions.depends_on = ['ST-UI-001'];
  library['stock-room/ST-API-002'].case.extensions.postconditions = ['ST-API-001'];
  library['stock-room/ST-API-003'].case.extensions.depends_on = ['missing'];
  library['stock-room/ST-API-004'].case.extensions.traffic = 'resource';
  const ineligible = ['ST-UI-001', 'ST-API-001', 'ST-API-002', 'ST-API-003', 'ST-API-004'];
  const options = request('/load-tests/options?projects=stock-room');
  assert.ok(options.items.every((r) => !ineligible.includes(r.case_id)));
  for (const id of ineligible) {
    const failed = request('/load-tests', 'POST', { mode: 'performance', concurrency: 2, target_requests: 2, project_id: 'stock-room', case_ids: [id] });
    assert.equal(failed.status, 422);
  }
});

test('browser backups preserve both frozen simulations and historical UI observations', () => {
  startSession(structuredClone(examples));
  const original = getData();
  const old = structuredClone(examples['/runs/run-83be44ec7c93']);
  Object.assign(old, { id: 'legacy-ui-load', family: 'load', load: { concurrency: 1, iterations: 1 } });
  original.runs.push(old);
  const load = (id) => runsApi({ path: '/load-tests/' + id, method: 'GET', params: new URLSearchParams(), db: currentData(), fixtures: examples });
  const stress = load('load-demo-stress').load_report;
  restoreData(JSON.parse(JSON.stringify(original)));
  assert.deepEqual(load('load-demo-stress').load_report, stress);
  assert.equal(load('legacy-ui-load').load_report.samples[0].kind, 'ui');
  assert.equal(load('legacy-ui-load').load_report.samples[0].elapsed_ms, old.results[0].duration_ms);
});

test('modern report capacity fails explicitly without allocating or recording a partial run', async () => {
  const { db, request } = environment();
  const total = db.runs.length;
  const response = request('/load-tests', 'POST', { mode: 'performance', project_id: 'stock-room',
    case_ids: ['ST-API-001'], concurrency: 1, target_requests: Number.MAX_SAFE_INTEGER });
  assert.equal(response.status, 422);
  assert.match((await response.json()).detail, /浏览器模拟.*50000/);
  assert.equal(db.runs.length, total);
});
