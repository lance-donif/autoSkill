import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { casesApi } from '../preview/cases-api.js';

const fixtures = JSON.parse(readFileSync(new URL('../preview/fixtures.json', import.meta.url), 'utf8'));

function visit() {
  const db = {};
  return (path, method = 'GET', body = undefined) => {
    const url = new URL(path, 'https://example.invalid/');
    const result = casesApi({ path: url.pathname, params: url.searchParams, method, body, fixtures, db });
    return result instanceof Response ? result : structuredClone(result);
  };
}

test('demo projects expose switchable environments with their own variables', () => {
  const request = visit();
  const project = request('/projects/stock-room');
  assert.equal(project.environment.id, 'dev');
  assert.deepEqual(project.environment.variables, { warehouse_id: 'WH-DEV' });
  const switched = request('/projects/stock-room/environments', 'PUT', {
    environments: project.environments, active_environment_id: 'test',
  });
  assert.equal(switched.environment.id, 'test');
  assert.equal(switched.active_environment_id, 'test');
  assert.equal(request('/projects/stock-room').environment.variables.warehouse_id, 'WH-TEST');
  assert.equal(request('/projects').find((item) => item.id === 'stock-room').environment.id, 'test');
});

test('the project settings drawer payload saves environments and falls back to the default', () => {
  const request = visit();
  const project = request('/projects/ticket-desk');
  const rows = [...project.environments,
    { id: 'prod', name: '生产环境', base_url: 'https://tickets.example.invalid', variables: { tenant: 'prod' }, is_default: false }];
  const saved = request('/projects/ticket-desk', 'PATCH', { environments: rows, active_environment_id: '' });
  assert.equal(saved.environments.length, 3);
  assert.equal(saved.environment.id, 'test');
  const kept = request('/projects/ticket-desk', 'PATCH', { environments: rows.filter((item) => item.id !== 'prod') });
  assert.equal(kept.environment.id, 'test');
});

test('invalid environment edits are rejected without changing the project', () => {
  const request = visit();
  const before = request('/projects/stock-room');
  const invalid = [
    [{ ...before.environments[0], is_default: true }, { ...before.environments[1], is_default: true }],
    [{ id: 'Bad ID', name: '无效标识' }],
    [{ id: 'dev', name: '' }],
    [{ id: 'dev', name: '开发', variables: { '1bad': 'x' } }],
    [{ id: 'dev', name: '开发', variables: { nested: { x: 1 } } }],
    [{ id: 'dev', name: '开发', base_url: 'not-a-url' }],
  ];
  for (const environments of invalid) {
    const result = request('/projects/stock-room/environments', 'PUT', { environments });
    assert.equal(result.status, 400);
  }
  const unknown = request('/projects/stock-room/environments', 'PUT', { environments: before.environments, active_environment_id: 'missing' });
  assert.equal(unknown.status, 400);
  assert.deepEqual(request('/projects/stock-room').environments, before.environments);
  assert.equal(request('/projects/stock-room').environment.id, 'dev');
  assert.equal(request('/projects/stock-room/environments', 'POST', {}).status, 405);
});

test('the sync fixture shows imported, skipped and failed files', () => {
  const sync = fixtures['/sync'];
  assert.ok(sync.sync_dir && sync.error_dir);
  assert.deepEqual(sync.files.map((file) => file.status), ['imported', 'skipped', 'failed']);
  assert.ok(sync.last_scan.imported >= 1);
});
