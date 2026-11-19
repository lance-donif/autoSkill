import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { casesApi } from '../preview/cases-api.js';
import { runsApi } from '../preview/runs-api.js';
import { currentData, getData, restoreData, startSession } from '../preview/store.js';

const fixtures = JSON.parse(readFileSync(new URL('../preview/fixtures.json', import.meta.url), 'utf8'));

function visit() {
  const db = { runs: Object.entries(fixtures).filter(([path]) => path.startsWith('/runs/')).map(([, run]) => structuredClone(run)) };
  return (path, method, body) => {
    const url = new URL(path, 'https://example.invalid/');
    const result = casesApi({ path: url.pathname, params: url.searchParams, method, body, fixtures, db });
    return result instanceof Response ? result : structuredClone(result);
  };
}

test('each listed case opens into its full detail and can be edited in the current visit', () => {
  const request = visit();
  for (const item of request('/cases').items) {
    const key = item.project_id + '/' + item.case_id;
    const detailPath = '/projects/' + item.project_id + '/cases/' + item.case_id;
    const detail = request(detailPath);
    assert.equal(detail.id, item.case_id);
    assert.ok(Array.isArray(detail.depends_on));
    if (detail.kind === 'ui') assert.ok(detail.steps.some((step) => step.check));
    else assert.ok(detail.request.method && detail.assertions.status);
    const source = request('/case-definitions/' + key);
    const updated = source.case;
    updated.title += '（演示修改）';
    for (const action of source.editing.actions) updated.extensions.automation.actions[action.index]._edit_id = action.id;
    const body = { expected_content_revision: source.content_revision, case: updated };
    assert.equal(request(detailPath, 'PUT', body).changed, true);
    assert.equal(request(detailPath).title, updated.title);
    assert.equal(request(detailPath).revision, item.revision + 1);
    assert.equal(request(detailPath, 'PUT', body).status, 409);
    assert.equal(request('/cases').items.find((row) => row.project_id === item.project_id && row.case_id === item.case_id).title, updated.title);
    assert.equal(visit()(detailPath).title, detail.title);
  }
});

test('folder operations use fresh revisions, reject cycles, and keep membership consistent after case deletion', () => {
  const request = visit();
  const { project_id, case_id } = request('/cases').items[0];
  const countBefore = request('/projects/' + project_id).cases.total;
  const path = '/projects/' + project_id + '/folders';
  const tree = request(path);
  const parent = request(path, 'POST', { name: '回归', parent_id: null, expected_revision: tree.revision });
  const top = parent.folders.at(-1).id;
  assert.equal(request(path, 'POST', { name: '旧草稿', expected_revision: tree.revision }).status, 409);
  const child = request(path, 'POST', { name: '子目录', parent_id: top, expected_revision: parent.revision });
  const dest = child.folders.at(-1).id;
  assert.equal(request(path + '/' + top, 'PATCH', { name: '回归', parent_id: dest, expected_revision: child.revision }).status, 400);
  const moved = request(path + '/move-cases', 'POST', { case_ids: [case_id], folder_id: dest, expected_revision: child.revision });
  assert.equal(moved.assignments[case_id], dest);
  const removed = request('/cases/delete', 'POST', { items: [{ project_id, case_id }] });
  assert.equal(removed.deleted[0].case_id, case_id);
  assert.equal(Object.hasOwn(request(path).assignments, case_id), false);
  assert.equal(request('/projects/' + project_id + '/cases/' + case_id).status, 404);
  assert.equal(request('/projects/' + project_id).cases.total, countBefore - 1);
});

test('project settings and credentials update within the visit', () => {
  const request = visit();
  const project = request('/projects')[0];
  const path = '/projects/' + project.id;
  request(path, 'PATCH', { name: '更新后的名称', timeout_seconds: 45, ignore_https_errors: true });
  assert.equal(request('/projects')[0].name, '更新后的名称');
  assert.equal(request(path).timeout_seconds, 45);
  request('/secrets/DEMO_KEY', 'PUT', { value: 'sample-value' });
  assert.deepEqual(request('/secrets').items.find((item) => item.name === 'DEMO_KEY'), { name: 'DEMO_KEY', value: 'sample-value' });
  request('/secrets/DEMO_KEY', 'DELETE');
  assert.equal(request('/secrets').items.some((item) => item.name === 'DEMO_KEY'), false);
});

test('restoring legacy results uses the same verdicts for case revisions, history filters, dashboard and run details', () => {
  startSession(fixtures);
  const saved = getData();
  const project = 'ticket-desk';
  const items = saved.caseLibrary.items.filter((item) => item.project_id === project);
  const template = saved.runs.find((run) => run.id === 'run-demo-06');
  const run = { ...template, id: 'legacy-run', status: 'Unverified', total: 6, done: 6, passed: 6,
    state: { ...template.state, outcome: 'unverified' }, state_counts: { pass: 0, unverified: 6 } };
  const samples = [
    ['Pass', 'valid', { execution_only_pass: true }, 'pass'],
    ['Pass', 'invalid', { business_status: 'Pass' }, 'fail'],
    ['Pass', 'valid', { business_status: 'Pass' }, 'pass'],
    ['Cancelled', 'valid', { target_started: false }, 'not_run'],
    ['Blocked', 'valid', { request: { method: 'GET' } }, 'fail'],
    ['Unverified', 'valid', { business_status: 'Unverified' }, 'fail'],
  ];
  run.results = samples.map(([status, evidence, detail], index) => {
    const item = items[index];
    item.state = { current: true, outcome: 'unverified', scope: { run_id: run.id } };
    const row = { ...template.results[0], id: 'old-' + index, run_id: run.id, case_id: item.case_id,
      case_revision: item.revision, state: { phase: 'settled', outcome: 'unverified', evidence }, status, detail };
    if (index === 2) item.revision++;
    return row;
  });
  run.results.push({ ...run.results[0], id: 'setup', status: 'Fail', prerequisite: true });
  saved.runs = [run];
  restoreData(saved);
  const db = currentData();
  const request = (target) => {
    const url = new URL(target, 'https://example.invalid/');
    const ctx = { path: url.pathname, params: url.searchParams, method: 'GET', fixtures, db };
    return casesApi(ctx) ?? runsApi(ctx);
  };
  assert.equal(saved.runs[0].state.outcome, 'unverified');
  const restored = request('/runs/' + run.id);
  assert.equal(restored.status, 'Unverified');
  assert.equal(restored.state.outcome, 'fail');
  assert.deepEqual(restored.state_counts, { pass: 2, fail: 3, not_run: 1 });
  assert.deepEqual(restored.results.slice(0, 6).map((r) => r.state.outcome), samples.map((s) => s[3]));
  for (const [status, count] of Object.entries({ Pass: 2, Fail: 3, NotRun: 1, Unverified: 0 })) {
    assert.equal(request('/results?status=' + status).total, count, status);
  }
  assert.equal(request('/runs?status=Fail').items[0].id, run.id);
  assert.equal(request('/runs?status=Unverified').total, 0);
  assert.deepEqual(request('/dashboard').summary.results.state_counts, restored.state_counts);
  const list = request('/cases?projects=' + project).items;
  for (const [index, item] of items.entries()) {
    const detail = request('/projects/' + project + '/cases/' + item.case_id);
    assert.deepEqual(detail.state, list.find((candidate) => candidate.case_id === item.case_id).state);
    assert.equal(detail.state.historical_outcome, samples[index][3]);
    assert.equal(detail.state.scope.run_id, run.id);
    assert.equal(detail.state.outcome, index === 2 ? 'not_run' : samples[index][3]);
    if (index === 2) assert.deepEqual([detail.current, detail.current_status, detail.last_status], [false, null, 'Pass']);
  }
  const once = getData();
  restoreData(once);
  assert.deepEqual(getData(), once);
});
