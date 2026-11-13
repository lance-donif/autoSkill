// The real Cordis waterfall, ReactLoopAgents and Python API are exercised here.
// No remote model request is made; SDK acceptance is separate from DSH Web acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import { HostService } from '../plugin/host-service.mjs';
import { kernelFixture, sdk, sdkHost, sdkSkip } from './dsh-sdk-tools.test.mjs';

const textOf = (message) => message.content.filter((block) => block.type === 'text').map((block) => block.text).join('\n');
const ownMessages = (decision) => decision.messages.filter((message) => message.source?.kind === 'autoskill-context');

async function contextFixture(t) {
  const kernel = await kernelFixture(t);
  const host = await sdkHost(t, kernel.workspace);
  const requests = [];
  const service = new HostService(kernel.spec, { fs: host.ctx.fs, workingDirectory: host.ctx.workingDirectory,
    fetch: async (url, options) => {
      requests.push({ path: new URL(url).pathname, method: options.method });
      return await fetch(url, options);
    } });
  return { kernel, ...host, requests, service,
    ownerA: await host.agent('dsh-sdk-A'), ownerB: await host.agent('dsh-sdk-B') };
}

test('host context is read-only, explicitly selected and isolated between real DSH Agents', { skip: sdkSkip }, async (t) => {
  const { kernel, ownerA, ownerB, service } = await contextFixture(t);
  const before = await kernel.snapshot();
  const unselected = await service.context(ownerA);
  assert.equal(unselected.status, 'selection_required');
  assert.equal(unselected.binding, null);
  assert.equal(unselected.bindings.page.total, 2);
  assert.deepEqual(unselected.runs.items, []);
  assert.deepEqual(unselected.reports, []);
  const selectedB = await service.context(ownerB);
  assert.equal(selectedB.binding.id, kernel.bindings.B.id);
  assert.equal(selectedB.workflow.workflow_id, kernel.flows.B.workflow_id);
  assert.equal(selectedB.cli_context.session_id, 'task-B');
  assert.equal(JSON.stringify(selectedB).includes(kernel.flows.A.workflow_id), false);
  assert.equal(JSON.stringify(selectedB).includes(kernel.bindings.report.id), false);
  await assert.rejects(service.context(ownerB, { binding_id: kernel.bindings.A.id }), { code: 'not_found' });
  const explicitA = await service.context(ownerA, { binding_id: kernel.bindings.A.id });
  assert.equal(explicitA.binding.id, kernel.bindings.A.id);
  assert.equal(explicitA.workflow.counts.leased, 1, 'reading an expired lease must not expire or claim it');
  assert.equal(explicitA.cli_context.session_id, 'task-A');
  const report = await service.context(ownerA, { binding_id: kernel.bindings.report.id });
  assert.equal(report.runs.items.length, 1);
  assert.equal(report.runs.items[0].id, kernel.run.id);
  assert.equal(report.runs.items[0].status, 'Fail');
  assert.equal(report.reports[0].receipt_status, 'ready');
  assert.equal(report.reports[0].verification, 'not_checked');
  assert.equal((await service.context(ownerA)).status, 'selection_required', 'explicit reads must not change selection');
  assert.deepEqual(await kernel.snapshot(), before, 'reads must add no DB rows, lease changes or report files');
});

test('real DSH pre-step waterfall preserves peers and rejection, deduplicates context and changes no kernel state', { skip: sdkSkip }, async (t) => {
  const { attachContext } = await import('../plugin/host-context.mjs');
  const { kernel, ctx, ownerA, ownerB, requests, service } = await contextFixture(t);
  const { createUserMessage } = await sdk('dsh-llm');
  const { agentEvents } = await sdk('dsh-agent');
  const message = (kind, text) => createUserMessage({ content: [{ type: 'text', text }], source: { kind } });
  const user = message('user', '保留用户原始请求');
  const outer = message('fixture-outer', '外层插件上下文');
  const inner = message('fixture-inner', '内层插件上下文');
  let rejectAfterContext = false;
  ctx.on('agent/pre-step', async (_payload, next) => {
    const decision = await next();
    if (rejectAfterContext) return { kind: 'reject' };
    return decision.kind === 'reject' ? decision : { ...decision, messages: [...decision.messages, outer], startsRequestSeries: true };
  });
  attachContext(ctx, service);
  ctx.on('agent/pre-step', async (_payload, next) => {
    const decision = await next();
    return decision.kind === 'reject' ? decision : { ...decision, messages: [...decision.messages, inner] };
  });
  function preStep(owner, step, options = {}) {
    const messages = options.messages ?? [user];
    return agentEvents(ctx, owner).waterfall('agent/pre-step', {
      messages, turn: options.turn ?? 1, step, signal: options.signal ?? new AbortController().signal,
      modelSelection: { provider: 'acceptance-fixture', model: 'no-model-call' },
    }, () => Promise.resolve({ kind: 'enter', messages }));
  }
  function admit(owner, decision) {
    // Match AgentLoop's accepted-message boundary without making a model request.
    for (const accepted of decision.messages) owner.session.append('user/message', accepted, { surfaceOp: 'append' });
  }
  ownerA.session.append('turn/start', { turn: 1 });
  ownerB.session.append('turn/start', { turn: 1 });
  const before = await kernel.snapshot();
  const firstA = await preStep(ownerA, 1);
  assert.equal(firstA.kind, 'enter');
  assert.equal(firstA.startsRequestSeries, true);
  assert.ok(firstA.messages.includes(user));
  assert.ok(firstA.messages.includes(outer));
  assert.ok(firstA.messages.includes(inner));
  assert.equal(ownMessages(firstA).length, 1);
  assert.match(textOf(ownMessages(firstA)[0]), /selection_required|选择|绑定/);
  admit(ownerA, firstA);
  const secondA = await preStep(ownerA, 2);
  assert.equal(ownMessages(secondA).length, 0, 'unchanged context must not be appended again within the turn');
  assert.ok(secondA.messages.includes(user));
  assert.ok(secondA.messages.includes(outer));
  assert.ok(secondA.messages.includes(inner));
  const retained = await preStep(ownerA, 3, { messages: firstA.messages });
  assert.equal(ownMessages(retained).length, 1, 'retaining a previous snapshot must not append a second copy');
  assert.equal(ownMessages(retained)[0], ownMessages(firstA)[0]);
  const firstB = await preStep(ownerB, 1);
  assert.equal(ownMessages(firstB).length, 1);
  assert.match(textOf(ownMessages(firstB)[0]), new RegExp(kernel.flows.B.workflow_id));
  assert.equal(textOf(ownMessages(firstB)[0]).includes(kernel.flows.A.workflow_id), false);
  admit(ownerB, firstB);
  assert.equal(ownMessages(await preStep(ownerB, 2)).length, 0);
  const callsBeforeReject = requests.length;
  const reject = ctx.on('agent/pre-step', () => Promise.resolve({ kind: 'reject' }));
  assert.deepEqual(await preStep(ownerA, 3), { kind: 'reject' });
  assert.equal(requests.length, callsBeforeReject, 'a rejected step must not read or inject host context');
  reject();
  const aborted = new AbortController();
  aborted.abort(new Error('pre-step cancelled before dispatch'));
  assert.equal(ownMessages(await preStep(ownerA, 4, { signal: aborted.signal })).length, 0);
  assert.equal(requests.length, callsBeforeReject);
  assert.equal(ownMessages(await preStep(ownerA, 1, { turn: 2 })).length, 1, 'a new turn receives its own current context');
  assert.ok(requests.every((request) => request.path === '/api/host/context' && request.method === 'GET'));
  assert.deepEqual(await kernel.snapshot(), before, 'pre-step cannot expire a lease, claim work or regenerate a report');
  rejectAfterContext = true;
  assert.deepEqual(await preStep(ownerB, 1, { turn: 20 }), { kind: 'reject' });
  rejectAfterContext = false;
  assert.equal(ownMessages(await preStep(ownerB, 1, { turn: 20 })).length, 1,
    'a snapshot rejected by an outer listener must be offered again on retry');
  const selected = await fetch(kernel.origin + '/api/host/bindings/select', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ host: 'dsh', host_session_id: ownerA.session.id,
      binding_id: kernel.bindings.A.id, expected_revision: 0, idempotency_key: 'fixture-select-after-first-step' }) });
  assert.equal(selected.ok, true);
  const afterSelection = await kernel.snapshot();
  const changed = await preStep(ownerA, 5);
  assert.equal(ownMessages(changed).length, 1, 'a changed selection must produce a fresh snapshot');
  assert.match(textOf(ownMessages(changed)[0]), new RegExp(kernel.flows.A.workflow_id));
  admit(ownerA, changed);
  assert.equal(ownMessages(await preStep(ownerA, 6)).length, 0);
  assert.deepEqual(await kernel.snapshot(), afterSelection, 'publishing the new snapshot remains read-only');
});
