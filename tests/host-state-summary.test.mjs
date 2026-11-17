import assert from 'node:assert/strict';
import test from 'node:test';
import { attachContext, contextText } from '../plugin/host-context.mjs';
import { HostService } from '../plugin/host-service.mjs';

const state = { lifecycle: 'completed', phase: 'settled', outcome: 'fail',
  checks: { business: 'unverified', performance: 'fail', quality: 'not_applicable' },
  evidence: 'invalid', report: 'failed', delivery: 'delivered', revision: 'case-v1', scope: { run_id: 'run-1' } };

test('the host model snapshot preserves all state axes without legacy success aliases', () => {
  const message = contextText({ host_session_id: 'session-1', state_revision: 'workspace-1',
    workflow: { workflow_id: 'flow-1', state, lifecycle: 'completed', conclusion: 'satisfied' },
    runs: { items: [{ id: 'run-1', state, execution_status: 'Pass', business_status: 'Pass' }], page: {} },
    reports: [{ report_id: 'report-1', state }], refs: {} });
  const snapshot = JSON.parse(message.split('\n')[1]);
  assert.deepEqual(snapshot.runs.items[0].state, state);
  assert.deepEqual(snapshot.workflow.state, state);
  assert.deepEqual(snapshot.reports[0].state, state);
  assert.equal(snapshot.runs.items[0].execution_status, undefined);
  assert.equal(snapshot.runs.items[0].business_status, undefined);
  assert.equal(snapshot.workflow.conclusion, undefined);
  assert.equal(snapshot.runs.items[0].state.delivery, 'delivered');
  assert.equal(snapshot.runs.items[0].state.checks.business, 'unverified');
  assert.match(message, /投递送达均不等于业务通过/);
});

test('paged run summaries keep the projected state while omitting large result details', async () => {
  const service = new HostService({ workspace: '/tmp/autoskill-state-summary', origin: 'http://127.0.0.1:8787' }, {
    fetch: () => Promise.resolve(new Response(JSON.stringify({ id: 'run-1', state, results: [{ id: 1, case_id: 'case-1',
      state, detail: { business_status: 'Pass', assertions: [{ passed: false }], steps: [{ text: 'large step' }] } }] }),
    { headers: { 'Content-Type': 'application/json' } })),
  });
  const output = await service.readRun({ run_id: 'run-1', host_session_id: 'session-1' }, { limit: 1 });
  assert.deepEqual(output.state, state);
  assert.deepEqual(output.results[0].state, state);
  assert.equal(output.results[0].detail, undefined);
  assert.equal(output.results[0].business_status, undefined);
  assert.equal(output.results[0].detail_loaded, false);
  assert.equal(output.results[0].assertion_count, 1);
});

test('manual login continuation across host turns retains the selected browser task and CLI binding', async () => {
  const cli = { host: 'dsh', host_session_id: 'host-1', session_id: 'browser-task-1', workflow_binding: null };
  const context = { host_session_id: cli.host_session_id, context_revision: 'selection-1',
    binding: { id: 'binding-1', task_session_id: cli.session_id, project_id: 'project-1' },
    selection: { binding_id: 'binding-1', revision: 1 }, cli_context: cli };
  const retained = [];
  const agent = { session: { surface: { nodes: retained }, eventAt: (node) => node } };
  let preStep;
  let reads = 0;
  attachContext({ on: (name, handler) => { assert.equal(name, 'agent/pre-step'); preStep = handler; } }, {
    runtimeContext: 'original-runtime', context: (owner) => {
      assert.equal(owner, agent);
      reads += 1;
      return Promise.resolve(structuredClone(context));
    },
  });
  const enter = (turn, messages = []) => preStep({ agent, turn, signal: new AbortController().signal },
    () => Promise.resolve({ kind: 'enter', messages }));
  const first = await enter(1);
  const original = first.messages[0];
  retained.push({ type: 'user/message', data: original });
  assert.equal((await enter(1)).messages.length, 0, 'the same turn does not append duplicate context');
  const reply = { role: 'user', content: [{ type: 'text', text: '已登录，继续' }] };
  const resumed = await enter(2, [reply]);
  assert.equal(resumed.messages[0], reply);
  const reminder = resumed.messages[1].content[0].text;
  const snapshot = JSON.parse(reminder.split('\n')[1]);
  assert.deepEqual(snapshot.cli_context, cli);
  assert.deepEqual(snapshot.binding, context.binding);
  assert.notEqual(resumed.messages[1].id, original.id, 'only the context message gets a new identity');
  assert.match(reminder, /--runtime-context=original-runtime/);
  assert.match(reminder, /cli_context.session_id.*--session.*browser snapshot.*browser tab list/);
  assert.match(reminder, /不取消录制、不重新 record start、不生成新会话/);
  assert.match(reminder, /--headed\/--headless.*优先于旧进程环境/);
  assert.equal(reads, 3);
});
