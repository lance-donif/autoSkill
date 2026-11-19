import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { casesApi } from '../preview/cases-api.js';

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
