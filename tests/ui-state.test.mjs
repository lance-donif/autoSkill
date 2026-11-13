import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { revisionPoll } from '../skills/autoskill/app/ui/js/live-refresh.js';

class TextNode {
  constructor(value) { this.value = value; this.children = []; }
}

class Element extends TextNode {
  constructor(tag) { super(''); this.tagName = tag; this.dataset = {}; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() { /* Presentation tests do not dispatch DOM actions. */ }
}

globalThis.Node = TextNode;
globalThis.document = { createElement: (tag) => new Element(tag), createElementNS: (_ns, tag) => new Element(tag),
  createTextNode: (value) => new TextNode(value) };
globalThis.window = { addEventListener: () => undefined };
globalThis.location = { hash: '#/cases?range=all' };
const { primaryState, stateResults, stateDetails } = await import('../skills/autoskill/app/ui/js/status-view.js');
const text = (node) => node.value + node.children.map(text).join(' ');

test('the primary phase remains ended while business and performance verdicts differ', () => {
  const entity = { state: { lifecycle: 'completed', phase: 'settled', outcome: 'fail',
    checks: { business: 'pass', performance: 'fail', quality: 'not_applicable' },
    evidence: 'valid', report: 'failed', delivery: 'unknown' } };
  assert.equal(text(primaryState(entity)).trim(), '已结束');
  const facts = text(stateResults(entity, false));
  assert.match(facts, /综合：失败/);
  assert.match(facts, /业务：通过/);
  assert.match(facts, /性能：失败/);
  assert.match(facts, /报告：失败/);
  assert.match(facts, /投递：尚未确认/);
});

test('an unverified business result is explicit and does not inherit an execution pass', () => {
  const entity = { state: { phase: 'settled', outcome: 'unverified',
    checks: { business: 'unverified', performance: 'not_applicable', quality: 'not_applicable' } } };
  assert.match(text(stateResults(entity)), /业务：未验证/);
  assert.doesNotMatch(text(stateResults(entity)), /业务：通过/);
  assert.equal(text(primaryState({})).trim(), '未同步');
});

test('historical executions are labelled without changing their original verdict', () => {
  const entity = { state: { phase: 'settled', outcome: 'pass', checks: { business: 'pass', performance: 'not_applicable', quality: 'not_applicable' },
    scope: { historical: true, cycle_id: 'old-cycle', current_cycle_id: 'new-cycle' } } };
  assert.match(text(primaryState(entity)), /已结束.*历史周期/);
  assert.match(text(stateResults(entity)), /业务：通过/);
});

test('an edited case keeps the current version unexecuted and labels prior passing facts as history', () => {
  const entity = { state: { current: false, phase: 'settled', lifecycle: 'completed', outcome: 'unverified', historical_outcome: 'pass',
    checks: { business: 'pass', performance: 'not_applicable', quality: 'not_applicable' },
    evidence: 'valid', report: 'ready', delivery: 'not_applicable', scope: { run_id: 'run-before-edit' } } };
  const before = JSON.stringify(entity);
  assert.equal(text(primaryState(entity)).trim(), '未执行');
  assert.match(text(stateResults(entity)), /综合：未验证/);
  assert.match(text(stateResults(entity)), /历史业务：通过/);
  const detail = stateDetails(entity);
  assert.match(text(detail), /历史阶段：已结束/);
  assert.match(text(detail), /历史综合：通过/);
  const history = detail.children.flatMap((node) => node.children).find((node) => node.tagName === 'a');
  assert.equal(history.attributes.href, '#/runs/run-before-edit?range=all');
  assert.equal(JSON.stringify(entity), before);
});

test('an executed current version with unverified business checks keeps its ended phase', () => {
  const entity = { state: { current: true, phase: 'settled', lifecycle: 'completed', outcome: 'unverified',
    checks: { business: 'unverified', performance: 'not_applicable', quality: 'not_applicable' } } };
  assert.equal(text(primaryState(entity)).trim(), '已结束');
  assert.match(text(stateResults(entity)), /业务：未验证/);
  assert.doesNotMatch(text(stateResults(entity)), /历史业务/);
});

test('revision polling notices idle external completion and skips unchanged snapshots', async () => {
  let revision = 'before';
  const rendered = [];
  const polling = revisionPoll({ read: () => Promise.resolve({ revision }),
    changed: (value) => { rendered.push(value.revision); }, unavailable: assert.fail, visible: () => true });
  try {
    await polling.check();
    await polling.check();
    revision = 'after-short-task';
    await polling.check();
    assert.deepEqual(rendered, ['before', 'after-short-task']);
  } finally { polling.close(); }
});

test('a pre-render baseline skips redundant work and still observes writes during initial rendering', async () => {
  let revision = 'before-render';
  const rendered = [];
  const polling = revisionPoll({ initialRevision: revision, read: () => Promise.resolve({ revision }),
    changed: (value) => { rendered.push(value.revision); }, unavailable: assert.fail, visible: () => true });
  try {
    await polling.check();
    assert.deepEqual(rendered, []);
    revision = 'write-during-first-render';
    await polling.check();
    assert.deepEqual(rendered, ['write-during-first-render']);
  } finally { polling.close(); }
});

test('a response arriving after disposal cannot update the current page', async () => {
  let finish;
  let rendered = 0;
  const polling = revisionPoll({ read: () => new Promise((resolve) => { finish = resolve; }),
    changed: () => { rendered += 1; }, unavailable: assert.fail, visible: () => true });
  const request = polling.check();
  polling.close();
  finish({ revision: 'stale' });
  await request;
  assert.equal(rendered, 0);
});

test('polling never overlaps and recovering a connection refreshes even the same revision', async () => {
  let pending;
  let calls = 0;
  let rendered = 0;
  let errors = 0;
  const polling = revisionPoll({ read: () => { calls += 1; return new Promise((resolve, reject) => { pending = { resolve, reject }; }); },
    changed: () => { rendered += 1; }, unavailable: () => { errors += 1; }, visible: () => true });
  try {
    const first = polling.check();
    await polling.check();
    assert.equal(calls, 1);
    pending.resolve({ revision: 'same' }); await first;
    const failure = polling.check(); pending.reject(new Error('offline')); await failure;
    const recovery = polling.check(); pending.resolve({ revision: 'same' }); await recovery;
    assert.equal(errors, 1);
    assert.equal(rendered, 2);
  } finally { polling.close(); }
});

test('visible polling checks at 250 ms without requiring an active task', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  let calls = 0;
  const polling = revisionPoll({ read: () => { calls += 1; return Promise.resolve({ revision: 'idle' }); },
    changed: () => undefined, unavailable: assert.fail, visible: () => true });
  try {
    t.mock.timers.tick(249); assert.equal(calls, 0);
    t.mock.timers.tick(1); await setImmediate(); assert.equal(calls, 1);
    t.mock.timers.tick(250); await setImmediate(); assert.equal(calls, 2);
  } finally { polling.close(); }
});

test('slow content updates do not block version checks and intermediate versions are coalesced', async () => {
  let revision = 'running';
  let finish;
  let reads = 0;
  const rendered = [];
  const polling = revisionPoll({ read: () => { reads += 1; return Promise.resolve({ revision }); },
    changed: async (value) => {
      rendered.push(value.revision);
      if (value.revision === 'running') await new Promise((resolve) => { finish = resolve; });
    }, unavailable: assert.fail, visible: () => true });
  try {
    const initial = polling.check();
    await setImmediate();
    revision = 'progress'; const progress = polling.check();
    await setImmediate();
    revision = 'completed'; const completed = polling.check();
    await setImmediate();
    assert.equal(reads, 3);
    assert.deepEqual(rendered, ['running']);
    finish();
    await Promise.all([initial, progress, completed]);
    assert.deepEqual(rendered, ['running', 'completed']);
  } finally { polling.close(); }
});
