// A programmed SDK-shaped host drives the actual CLI bridge; this does not invoke a DSH model.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { HostAdapter } from '../plugin/host-adapter.mjs';

const [origin, workspace, python, app] = process.argv.slice(2);
const context = Buffer.from(JSON.stringify({ version: 1, workspace, port: Number(new URL(origin).port), host: 'dsh' })).toString('base64url');
const pending = await (await fetch(origin + '/api/host/deliveries?host=dsh')).json();
const deliveries = new Map(pending.items.map((delivery) => [delivery.id, delivery]));
const agents = new Map(pending.items.map((delivery) => [delivery.payload.parameters.host_session_id,
  { inbox: { nextTurn: [], nextStep: [] }, session: { snapshotEvents: () => [] } }]));
const calls = [];

function command(delivery, sessionId, args, payload) {
  return JSON.parse(execFileSync(python, ['-m', 'autoskill', '--runtime-context=' + context, '--session=' + delivery.payload.session_id,
    '--host-session=' + sessionId, ...args], {
    cwd: workspace, env: { ...process.env, PYTHONPATH: app, AUTOSKILL_BRIDGE: 'on' },
    input: payload ? JSON.stringify(payload) : '', encoding: 'utf8', timeout: 20000,
  }));
}

const controller = {
  resolveAgent: async (sessionId) => { await Promise.resolve(); return { agent: agents.get(sessionId) }; },
  prompt: async (request) => {
    await Promise.resolve();
    const delivery = deliveries.get(request.requestId.replace('autoskill-delivery-', ''));
    const task = command(delivery, request.sessionId, ['judge', 'next', '--run', delivery.payload.run_id,
      '--idempotency-key', request.requestId + '-next']).task;
    assert.equal(task.judge_id, delivery.payload.judge_id);
    const matches = task.input.answer === task.input.frozen_reference.expected;
    const judgement = { verdict: matches ? 'pass' : 'fail', action: 'finish', reason: '按冻结预期核对实际回答', evidence_refs: ['/answer', '/frozen_reference/expected'] };
    const finished = command(delivery, request.sessionId, ['judge', 'submit', task.judge_id, '--attempt-id', task.attempt_id,
      '--lease-token', task.lease_token, '--idempotency-key', request.requestId + '-submit', '--file', '-'], judgement);
    assert.equal(finished.status, 'completed');
    calls.push({ session_id: request.sessionId, task_session: delivery.payload.session_id, judge_id: task.judge_id, verdict: judgement.verdict });
    return { accepted: true };
  },
};
const sessions = { flush: async () => { await Promise.resolve(); return true; } };
const adapter = new HostAdapter({ controller, sessions, logger: { warn: (message) => { throw new Error(message); } } },
  { cwd: workspace, env: { AUTOSKILL_WORKSPACE: workspace }, origin }, { runtimeContext: context });
await adapter.poll();
await adapter.poll();
assert.equal(calls.length, 2);
process.stdout.write(JSON.stringify({ calls }));
