import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { advanceTasks, nextDue, schedulesApi, validateTasks } from '../preview/schedules-api.js';

const fixtures = JSON.parse(readFileSync(new URL('../preview/fixtures.json', import.meta.url), 'utf8'));
const projectIds = fixtures['/projects'].map((project) => project.id);
const sample = fixtures['/cases'].items.find((item) => item.replayable);

function session() {
  const db = { runs: [] };
  return { db, request(path, method = 'GET', body = undefined) {
    return schedulesApi({ db, fixtures, method, path, body, params: new URLSearchParams() });
  } };
}

test('schedule editing uses revisions, runs target cases and resets with a new page', () => {
  const { db, request } = session();
  const task = request('/tasks', 'POST', { project_id: sample.project_id, title: 'Example schedule', rule: { kind: 'interval', minutes: 15 },
    case_ids: [sample.case_id], engine: 'selenium', headed: false });
  const path = '/tasks/' + task.id;
  assert.equal(request(path).rule_text, '每 15 分钟');
  assert.equal(request(path, 'PATCH', { enabled: false, expected_revision: task.revision }).next_due, null);
  assert.equal(request(path, 'PATCH', { enabled: true, expected_revision: task.revision }).status, 409);
  assert.match(request(path, 'PATCH', { title: 'New title', enabled: true, expected_revision: task.revision + 1 }).next_due, /Z$/);
  const run = request(path + '/run', 'POST', {});
  assert.equal(request(path).last_run_id, run.id);
  assert.deepEqual(db.runs[0].case_ids, [sample.case_id]);
  assert.equal(db.runs[0].task_id, task.id);
  assert.equal(db.runs[0].simulated, true);
  assert.deepEqual(request(path, 'DELETE'), { deleted: task.id });
  assert.equal(request(path).status, 404);
  assert.equal(db.runs.length, 1);
  assert.equal(session().request('/tasks').length, fixtures['/tasks'].length);
});

test('a due once-only task advances once and retains its browser run', () => {
  const { db, request } = session();
  const at = Date.now() + 5000;
  const task = request('/tasks', 'POST', { project_id: sample.project_id, title: 'Once', rule: { kind: 'at', at: new Date(at).toISOString() } });
  assert.equal(advanceTasks(db, at - 1, fixtures), false);
  assert.equal(advanceTasks(db, at, fixtures), true);
  assert.equal(advanceTasks(db, at + 60000, fixtures), false);
  assert.equal(request('/tasks/' + task.id).enabled, false);
  assert.equal(db.runs[0].task_id, task.id);
  assert.equal(validateTasks(db.tasks, projectIds), true);
  assert.throws(() => validateTasks([{ ...task, project_id: 'missing' }], projectIds), /项目不存在/);
});

test('weekly schedules use the chosen timezone and skip nonexistent civil times', () => {
  assert.equal(nextDue({ kind: 'weekly', time: '09:15', days: [0], timezone: 'Asia/Shanghai' },
    Date.parse('2025-01-05T22:00:00Z')), '2025-01-06T01:15:00.000Z');
  assert.equal(nextDue({ kind: 'daily', time: '02:30', timezone: 'America/New_York' },
    Date.parse('2025-03-09T04:00:00Z')), '2025-03-10T06:30:00.000Z');
});
