// Parameterless activation loads the actual registry body through the host's non-waking Agent context API.
import assert from 'node:assert/strict';
import test from 'node:test';
import { registerCommands } from '../plugin/host-commands.mjs';

function fixture(options = {}) {
  const definitions = [];
  const loaded = [];
  const injected = [];
  const agent = { session: { id: 'session-a' }, inject: (message) => injected.push(message) };
  const skill = { name: 'autoskill', content: '# 已注册技能\nautoskill --runtime-context=bound context project',
    invocation: { userInvocable: true, modelInvocable: true } };
  const ctx = { effect: (create) => create(), commands: { register: (definition) => { definitions.push(definition); return () => undefined; } },
    workingDirectory: { ensure: (owner) => { assert.equal(owner, agent); return Promise.resolve('/host/project'); } },
    skills: { get: (name, lookup) => { loaded.push({ name, lookup }); return Promise.resolve(options.skill === undefined ? skill : options.skill); } } };
  registerCommands(ctx);
  const execute = (rawInput = '', signal = new AbortController().signal) => definitions[0].handler({ agent, rawInput, signal });
  return { definitions, loaded, injected, agent, execute, skill };
}

test('the only advertised command has no input descriptor and queues complete skill instructions for its own Agent', async () => {
  const app = fixture();
  assert.deepEqual(app.definitions.map((definition) => definition.name), ['autoskill-use']);
  assert.equal(app.definitions[0].input, undefined);
  const outcome = await app.execute();
  assert.equal(outcome.kind, 'success');
  assert.match(outcome.text, /已加载.*直接描述/);
  assert.equal(app.loaded[0].name, 'autoskill');
  assert.equal(app.loaded[0].lookup.scope, app.agent);
  assert.equal(app.loaded[0].lookup.cwd, '/host/project');
  assert.equal(app.injected.length, 1);
  assert.equal(app.injected[0].content[0].text, app.skill.content);
  assert.deepEqual(app.injected[0].source, { kind: 'skill-invocation', name: 'autoskill', form: 'instructions' });
});

test('arguments are rejected without mode dispatch, registry work or task execution', async () => {
  const app = fixture();
  for (const input of ['status', 'report run_1', 'none', 'https://example.com', '测试这个页面']) {
    const outcome = await app.execute(input);
    assert.equal(outcome.kind, 'error');
    assert.match(outcome.text, /无需参数/);
  }
  assert.equal(app.injected.length, 0);
  assert.equal(app.loaded.length, 0);
});

test('missing, unavailable or aborted skills never report successful activation', async () => {
  for (const skill of [null, { content: 'private', invocation: { userInvocable: false } }]) {
    const app = fixture({ skill });
    assert.equal((await app.execute()).kind, 'error');
    assert.equal(app.injected.length, 0);
  }
  const app = fixture();
  const controller = new AbortController();
  controller.abort(new Error('cancelled'));
  assert.equal((await app.execute('', controller.signal)).kind, 'error');
  assert.equal(app.injected.length, 0);
});
