import assert from 'node:assert/strict';
import test from 'node:test';
import { contextText } from '../plugin/host-context.mjs';
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
