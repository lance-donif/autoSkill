// Contract fakes exercise delivery recovery. They do not claim a live DSH model was invoked.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { HostAdapter } from '../plugin/host-adapter.mjs';

function claimDelivery(state, body) {
  if (state.delivery.state === 'pending') state.delivery = { ...state.delivery, state: 'dispatching', token: 'token-1', owner: body.owner };
  return state.delivery;
}

function acknowledgeDelivery(state, body) {
  if (state.failAck) { state.failAck = false; throw new Error('lost acknowledgement'); }
  assert.equal(body.token, state.delivery.token);
  state.delivery = { ...state.delivery, state: 'delivered', receipt: body.receipt };
  return state.delivery;
}

function resolveDelivery(state, body) {
  assert.equal(state.delivery.state, 'unknown');
  assert.ok(body.reason);
  state.delivery = { ...state.delivery, state: body.receipt ? 'delivered' : 'pending', receipt: body.receipt ?? {} };
  return state.delivery;
}

function releaseDelivery(state, body) {
  assert.equal(state.delivery.state, 'dispatching');
  assert.equal(body.token, state.delivery.token);
  state.delivery = { ...state.delivery, state: 'pending', token: '', owner: '' };
  return state.delivery;
}

function readState(state, path) {
  if (path.startsWith('/api/judgements/')) return state.judge;
  return { items: state.delivery.state === 'delivered' ? [] : [state.delivery], has_more: false, next_offset: null };
}

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'autoskill-host-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const state = {
    delivery: { id: 'delivery-1', state: 'pending', token: '', owner: '', payload: { workflow_id: 'wf-1', session_id: 'task-1', parameters: {} } },
    agents: new Map(), prompts: [], creates: [], flush: true, failAck: false, failActivation: false, calls: [],
  };
  const controller = {
    resolveAgent: async (id) => {
      await Promise.resolve();
      if (state.failActivation) return { error: Object.assign(new Error('busy'), { code: 'session/agent-busy' }) };
      return state.agents.has(id) ? { agent: state.agents.get(id) } : { error: Object.assign(new Error('missing'), { code: 'session/not-found' }) };
    },
    create: async (request) => {
      await Promise.resolve();
      state.creates.push(request);
      const events = [];
      state.agents.set(request.sessionId, { inbox: { nextTurn: [], nextStep: [] }, session: { snapshotEvents: () => events }, events });
      return { sessionId: request.sessionId };
    },
    prompt: async (request, signal) => {
      await Promise.resolve();
      signal.throwIfAborted();
      state.prompts.push(request);
      state.agents.get(request.sessionId).events.push({ type: 'user/message', data: { source: { kind: 'user', rpcId: request.requestId } } });
      return { accepted: true };
    },
  };
  const sessions = { flush: async () => { await Promise.resolve(); return state.flush; } };
  const fetch = async (url, options) => {
    await Promise.resolve();
    const body = options.body ? JSON.parse(options.body) : {};
    const path = new URL(url).pathname;
    state.calls.push({ path, body });
    const handlers = { claim: claimDelivery, ack: acknowledgeDelivery, resolve: resolveDelivery, release: releaseDelivery };
    const result = options.method === 'GET' ? readState(state, path) : handlers[path.split('/').at(-1)](state, body);
    return { ok: true, json: async () => { await Promise.resolve(); return structuredClone(result); } };
  };
  const spec = { cwd: directory, env: { AUTOSKILL_WORKSPACE: directory }, origin: 'http://127.0.0.1:8787' };
  const adapter = new HostAdapter({ controller, sessions }, spec, { fetch, runtimeContext: 'opaque-context' });
  return { state, adapter };
}

test('delivers through the actual SDK-shaped prompt API and acknowledges only after persistence', async (t) => {
  const { state, adapter } = await fixture(t);
  await adapter.poll();
  assert.equal(state.delivery.state, 'delivered');
  assert.equal(state.prompts.length, 1);
  assert.equal(state.prompts[0].requestId, 'autoskill-delivery-delivery-1');
  assert.equal(state.prompts[0].mode, 'queue');
  assert.match(state.prompts[0].content[0].text, /--runtime-context=opaque-context/);
  assert.match(state.prompts[0].content[0].text, /--host-session=session-autoskill-/);
  assert.equal(state.delivery.receipt.completion, 'not_asserted');
  assert.equal((await adapter.journal('delivery-1')).state, 'delivered');
  await adapter.poll();
  assert.equal(state.prompts.length, 1);
});

test('a lost local acknowledgement retries the receipt without sending another prompt', async (t) => {
  const { state, adapter } = await fixture(t);
  state.failAck = true;
  await adapter.poll();
  assert.equal(state.delivery.state, 'dispatching');
  await adapter.poll();
  assert.equal(state.delivery.state, 'delivered');
  assert.equal(state.prompts.length, 1);
});

test('an uncertain send is recovered from the matching SDK request identity and flush', async (t) => {
  const { state, adapter } = await fixture(t);
  state.flush = false;
  await adapter.poll();
  assert.equal((await adapter.journal('delivery-1')).state, 'sending');
  state.delivery.state = 'unknown';
  state.flush = true;
  await adapter.poll();
  assert.equal(state.delivery.state, 'delivered');
  assert.equal(state.prompts.length, 1);
});

test('missing evidence never causes an unknown delivery to be sent again', async (t) => {
  const { state, adapter } = await fixture(t);
  state.flush = false;
  await adapter.poll();
  const agent = state.agents.get(state.prompts[0].sessionId);
  agent.events.length = 0;
  state.delivery.state = 'unknown';
  await adapter.poll();
  assert.equal(state.delivery.state, 'unknown');
  assert.equal(state.prompts.length, 1);
});

test('activation failure releases only the current token before prompt admission', async (t) => {
  const { state, adapter } = await fixture(t);
  state.failActivation = true;
  await adapter.poll();
  assert.equal(state.delivery.state, 'pending');
  assert.equal(state.prompts.length, 0);
  assert.equal(state.calls.filter((call) => call.path.endsWith('/release')).length, 1);
  state.failActivation = false;
  await adapter.poll();
  assert.equal(state.delivery.state, 'delivered');
});

test('an already completed judgement satisfies its delivery without waking another host turn', async (t) => {
  const { state, adapter } = await fixture(t);
  state.delivery.payload = { judge_id: 'judge-1', parameters: {} };
  state.judge = { judge_id: 'judge-1', status: 'completed', input_hash: 'frozen-input' };
  await adapter.poll();
  assert.equal(state.prompts.length, 0);
  assert.equal(state.creates.length, 0);
  assert.deepEqual(state.delivery.receipt, { host: 'dsh', method: 'already_reviewed', judge_id: 'judge-1', input_hash: 'frozen-input' });
});

test('an empty optional host identity creates a stable independent session', async (t) => {
  const { state, adapter } = await fixture(t);
  state.delivery.payload.parameters.host_session_id = '';
  await adapter.poll();
  assert.equal(state.delivery.state, 'delivered');
  assert.match(state.creates[0].sessionId, /^session-autoskill-[a-f0-9]{64}$/);
  assert.equal(state.creates[0].sessionId, state.prompts[0].sessionId);
});
