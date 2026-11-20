import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { casesApi } from '../preview/cases-api.js';
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

test('weekly tasks retain one occurrence across the New York spring gap and fall fold', (t) => {
  const clock = t.mock.method(Date, 'now');
  for (const [time, start, first, missed, following] of [
    ['02:30', '2027-03-07T06:00Z', '2027-03-07T07:30Z', '2027-03-14T07:30Z', '2027-03-21T06:30Z'],
    ['01:30', '2027-11-07T04:00Z', '2027-11-07T05:30Z', '2027-11-07T06:30Z', '2027-11-14T06:30Z'],
  ]) {
    clock.mock.mockImplementation(() => Date.parse(start));
    const { db, request } = session();
    db.tasks = [];
    const rule = { kind: 'weekly', days: [6], time, timezone: 'America/New_York' };
    const task = request('/tasks', 'POST', { title: 'Calendar task', project_id: sample.project_id, case_ids: [sample.case_id], rule });
    assert.equal(Date.parse(task.next_due), Date.parse(first));
    assert.equal(advanceTasks(db, Date.parse(first), fixtures), true);
    assert.equal(advanceTasks(db, Date.parse(missed), fixtures), false);
    assert.equal(Date.parse(request('/tasks/' + task.id).next_due), Date.parse(following));
    assert.equal(db.runs.length, 1);
  }
});

test('a task with a deleted case can be paused and needs a valid target before enabling', () => {
  const { db, request } = session();
  const task = request('/tasks', 'POST', { title: 'May pause', project_id: sample.project_id, case_ids: [sample.case_id], rule: { kind: 'interval', minutes: 5 } });
  casesApi({ db, fixtures, path: '/cases/delete', method: 'POST', body: { items: [{ project_id: sample.project_id, case_id: sample.case_id }] } });
  const path = '/tasks/' + task.id;
  const paused = request(path, 'PATCH', { enabled: false, expected_revision: task.revision });
  assert.equal(paused.next_due, null);
  assert.equal(request(path, 'PATCH', { enabled: true, expected_revision: paused.revision }).status, 400);
  assert.equal(request(path).enabled, false);
  const replacement = fixtures['/cases'].items.find((item) => item.project_id === sample.project_id && item.case_id !== sample.case_id && item.replayable);
  assert.equal(request(path, 'PATCH', { enabled: true, case_ids: [replacement.case_id], expected_revision: paused.revision }).enabled, true);
});
