// Client component and transport contracts use the actual shipped module with SDK-shaped services.
// They do not stand in for an installed DSH application's interactive acceptance.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../plugin/client.js', import.meta.url), 'utf8');
const response = (value, status = 200) => ({ ok: status < 400, status, json: () => Promise.resolve(value) });
const wire = (data, extra = {}) => ({ version: 1, request_state: 'completed', identity: { host_session_id: 'session-a' }, data, ...extra });
const context = (sessionId, extra = {}) => ({ version: 1, host_session_id: sessionId, status: 'bound', context_revision: '1',
  binding: { id: 'binding-a', project_id: 'project-a', task_session_id: 'task-a' }, selection: { binding_id: 'binding-a', revision: 3 },
  bindings: { items: [{ id: 'binding-a', label: '原任务' }, { id: 'binding-b', label: '第二任务' }], page: {} },
  runs: { items: [], page: {} }, reports: [], ...extra });

function harness(fetcher, hostOrigin = 'http://127.0.0.1:8788') {
  let registration;
  const slots = [];
  const disposers = [];
  const events = new Map();
  const timers = new Map();
  const requests = [];
  const notifications = [];
  const panels = [];
  const scopes = new Map();
  let timerId = 0;
  const React = {
    createElement: (tag, props, ...children) => ({ tag, props: { ...props, children: children.flat(Infinity) } }),
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
  };
  const window = { __ModuleLoader__: { load: (value) => { registration = value; } } };
  vm.runInNewContext(source, {
    window, URL, URLSearchParams, AbortController,
    fetch: (url, options) => {
      const request = { url: new URL(url, 'http://dsh.test/nested/'), options };
      requests.push(request);
      if (request.url.pathname.endsWith('/api/autoskill.host')) return Promise.resolve(response({ version: 1, dashboard_origin: hostOrigin }));
      return Promise.resolve(fetcher ? fetcher(request) : response(context(request.url.searchParams.get('session_id'))));
    },
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: (id) => timers.delete(id),
  });
  const ctx = {
    effect: (fn) => { const dispose = fn(); if (dispose) disposers.push(dispose); return dispose; },
    on: (event, fn) => { events.set(event, fn); return () => events.delete(event); },
    slots: { inject: (_name, fn) => fn(), register: (options, component) => { slots.push({ options, component }); return () => undefined; } },
    layout: { selectPanel: (id) => panels.push(id) },
    sessions: { scope: (id) => {
      if (!scopes.has(id)) scopes.set(id, { id, get: (name) => {
        assert.equal(name, 'conversation');
        return { input: { for: (scope) => ({ notify: (level, text) => notifications.push({ sessionId: scope.id, level, text }) }) } };
      } });
      return scopes.get(id);
    } },
  };
  const plugin = registration.factory((name) => { assert.equal(name, 'react'); return React; });
  plugin.apply(ctx, { port: 8788 });
  const dock = slots.find((entry) => entry.options.name === 'conversation.input.dock');
  const taskCard = slots.find((entry) => entry.options.key === 'autoskill_task');
  const injected = (sessionId = 'session-a') => dock.options.inject(sessionId);
  return { slots, requests, notifications, panels, events, timers, dock, taskCard, injected,
    dispose: () => { for (const dispose of disposers.reverse()) dispose(); } };
}

function nodes(tree, predicate) {
  if (!tree || typeof tree !== 'object') return [];
  return [...(predicate(tree) ? [tree] : []), ...tree.props.children.flatMap((child) => nodes(child, predicate))];
}

function text(tree) {
  if (tree === null || tree === undefined || tree === false) return '';
  return typeof tree === 'object' ? tree.props.children.map(text).join(' ') : String(tree);
}

function click(tree, title) {
  const match = nodes(tree, (node) => node.tag === 'button' && text(node) === title)[0];
  assert.ok(match, 'Missing button: ' + title);
  assert.equal(match.props.disabled, false);
  match.props.onClick();
}

test('native cards and the list dock register without replacing voice activity', () => {
  const app = harness();
  assert.deepEqual(app.slots.filter((slot) => slot.options.name === 'tool.call.toolview').map((slot) => slot.options.key), ['autoskill_read', 'autoskill_task']);
  assert.equal(app.slots.filter((slot) => slot.options.name === 'conversation.input.dock').length, 1);
  assert.equal(app.slots.some((slot) => slot.options.name === 'conversation.input.activity'), false);
  assert.notEqual(app.injected('session-a').autoskill.store, app.injected('session-b').autoskill.store);
  assert.equal(app.injected('session-a').autoskill.store, app.injected('session-a').autoskill.store);
  app.dispose();
});

test('panels resolve the actual host runtime port before loading any iframe', async () => {
  const app = harness(undefined, 'http://127.0.0.1:49667');
  const props = app.injected();
  const panel = app.slots.find((entry) => entry.options.key === 'autoskill-dashboard');
  assert.equal(panel.component().tag, 'div');
  props.autoskill.navigation.open('runs/run-actual');
  await props.autoskill.navigation.refreshHost();
  const frame = panel.component();
  assert.equal(frame.tag, 'iframe');
  assert.equal(new URL(frame.props.src).origin, 'http://127.0.0.1:49667');
  assert.equal(new URL(frame.props.src).hash, '#/runs/run-actual');
  assert.equal(app.requests[0].url.pathname, '/nested/api/autoskill.host');
  app.events.get('connection/reset')();
  assert.equal(panel.component().tag, 'div');
  app.dispose();
});

test('delayed reads stay in their owning session and polling stops after the last subscriber', async () => {
  const waiting = new Map();
  const app = harness(({ url }) => new Promise((resolve) => waiting.set(url.searchParams.get('session_id'), resolve)));
  const first = app.injected('session-a').autoskill.store;
  const second = app.injected('session-b').autoskill.store;
  const offFirst = first.subscribe(() => undefined);
  const offSecond = second.subscribe(() => undefined);
  waiting.get('session-b')(response(context('session-b', { context_revision: 'b' })));
  await setImmediate();
  waiting.get('session-a')(response(context('session-a', { context_revision: 'a' })));
  await setImmediate();
  assert.equal(first.getSnapshot().data.context_revision, 'a');
  assert.equal(second.getSnapshot().data.context_revision, 'b');
  assert.ok(app.requests.every((request) => request.url.pathname === '/nested/api/autoskill.context'));
  assert.ok(app.requests.every((request) => request.options.credentials === 'same-origin'));
  offFirst(); offSecond();
  assert.equal(app.timers.size, 0);
  app.dispose();
});

test('an inactive host route shows a useful unavailable message instead of a JSON parse exception', async () => {
  const app = harness(() => ({ ok: false, status: 404, json: () => Promise.reject(new SyntaxError('Unexpected token in not found')) }));
  const store = app.injected().autoskill.store;
  const off = store.subscribe(() => undefined);
  await setImmediate();
  assert.match(store.getSnapshot().error, /autoSkill 服务暂不可用.*404.*插件/);
  assert.doesNotMatch(store.getSnapshot().error, /Unexpected token/);
  assert.equal(store.getSnapshot().data, null);
  off(); app.dispose();
});

test('disconnect invalidates pending responses and aborts reads on unmount', async () => {
  const waiting = [];
  const app = harness(() => new Promise((resolve) => waiting.push(resolve)));
  const store = app.injected().autoskill.store;
  const off = store.subscribe(() => undefined);
  app.events.get('connection/reset')();
  assert.equal(app.requests[0].options.signal.aborted, true);
  waiting[0](response(context('session-a', { context_revision: 'old' })));
  waiting[1](response(context('session-a', { context_revision: 'new' })));
  await setImmediate();
  assert.equal(store.getSnapshot().data.context_revision, 'new');
  void store.refresh();
  off();
  assert.equal(app.requests.at(-1).options.signal.aborted, true);
  waiting[2](response(context('session-a', { context_revision: 'late' })));
  await setImmediate();
  assert.equal(store.getSnapshot().data.context_revision, 'new');
  assert.equal(app.timers.size, 0);
  app.dispose();
});

test('completion notifications name the real result and stay with the original session', async () => {
  let status = 'Running';
  const app = harness(({ url }) => response(context(url.searchParams.get('session_id'), {
    runs: { items: [{ id: 'run-a', execution_status: status, status }], page: {} },
  })));
  const store = app.injected('session-a').autoskill.store;
  const off = store.subscribe(() => undefined);
  await setImmediate();
  assert.equal(app.notifications.length, 0);
  app.injected('session-b');
  status = 'Fail';
  await store.refresh();
  await store.refresh();
  assert.equal(app.notifications.length, 1);
  assert.equal(app.notifications[0].sessionId, 'session-a');
  assert.equal(app.notifications[0].level, 'error');
  assert.match(app.notifications[0].text, /失败/);
  assert.doesNotMatch(app.notifications[0].text, /通过/);
  off(); app.dispose();
});

test('a completed workflow retains its violated business conclusion in the notification and dock', async () => {
  let lifecycle = 'active';
  const app = harness(() => response(context('session-a', {
    workflow: { workflow_id: 'flow-a', cycle_id: 'cycle-a', lifecycle, conclusion: 'violated', current_phase: 'knowledge', counts: { done: 5 } },
  })));
  const props = app.injected();
  const off = props.autoskill.store.subscribe(() => undefined);
  await setImmediate();
  lifecycle = 'completed';
  await props.autoskill.store.refresh();
  assert.equal(app.notifications.length, 1);
  assert.match(app.notifications[0].text, /已完成.*不符合预期/);
  assert.match(text(app.dock.component(props)), /不符合预期/);
  assert.match(text(app.dock.component(props)), /沉淀知识/);
  off(); app.dispose();
});

test('a lost cancel response is queried by request id and never automatically repeated', async () => {
  let operationState = 'unknown';
  const app = harness(({ url, options }) => {
    if (options.method === 'POST') return Promise.reject(new Error('connection lost'));
    if (url.searchParams.get('resource') === 'operation') return response(wire(wire({}, { request_state: operationState })));
    return response(context('session-a'));
  });
  const store = app.injected().autoskill.store;
  const action = await store.command('run.cancel', { run_id: 'run-original' }, 'binding-original');
  await store.command('run.cancel', { run_id: 'run-original' }, 'binding-original');
  const sent = JSON.parse(app.requests[0].options.body);
  assert.equal(sent.session_id, 'session-a');
  assert.equal(sent.binding_id, 'binding-original');
  assert.equal(sent.payload.run_id, 'run-original');
  assert.equal(action.state, 'unknown');
  await store.queryAction(action.key);
  assert.equal(store.getSnapshot().actions[action.key].state, 'unknown');
  operationState = 'completed';
  await store.queryAction(action.key);
  assert.equal(store.getSnapshot().actions[action.key].state, 'completed');
  assert.equal(app.requests.filter((request) => request.options.method === 'POST').length, 1);
  assert.ok(app.requests.slice(1).every((request) => request.url.searchParams.get('request_id') === sent.request_id));
  app.dispose();
});

test('historical cards navigate and cancel their recorded run without borrowing the selected task', async () => {
  const app = harness(({ url, options }) => options.method === 'POST' ? response(wire({})) : response(context(url.searchParams.get('session_id'))));
  const props = app.injected();
  const off = props.autoskill.store.subscribe(() => undefined);
  await setImmediate();
  const identity = { host_session_id: 'session-a', binding_id: 'old-binding', project_id: 'old-project', run_id: 'old-run' };
  const view = app.taskCard.component({ ...props, phase: 'result', toolName: 'autoskill_task',
    block: { meta: { autoskill: { ...identity, request_state: 'completed' } }, content: [{ type: 'text', text: '原操作已接收' }] } });
  click(view, '打开执行报告');
  await props.autoskill.navigation.refreshHost();
  const panel = app.slots.find((entry) => entry.options.key === 'autoskill-dashboard');
  assert.equal(new URL(panel.component().props.src).hash, '#/runs/old-run');
  click(view, '打开项目');
  assert.equal(new URL(panel.component().props.src).hash, '#/cases?projects=old-project&range=all');
  assert.deepEqual(app.panels, ['autoskill-dashboard', 'autoskill-dashboard']);
  click(view, '取消执行');
  await setImmediate();
  const sent = JSON.parse(app.requests.find((request) => request.options.method === 'POST').options.body);
  assert.equal(sent.binding_id, 'old-binding');
  assert.equal(sent.payload.run_id, 'old-run');
  assert.equal(props.autoskill.store.getSnapshot().data.selection.binding_id, 'binding-a');
  off(); app.dispose();
});

test('a terminal run in the recorded tool snapshot keeps report navigation without a cancel action', () => {
  const app = harness();
  const identity = { host_session_id: 'session-a', binding_id: 'old-binding', run_id: 'old-failure' };
  const view = app.taskCard.component({ ...app.injected(), phase: 'result', toolName: 'autoskill_read', block: {
    content: [{ type: 'text', text: JSON.stringify(wire({ runs: { items: [{ id: 'old-failure', status: 'Fail', can_cancel: false }] } }, { identity })) }],
  } });
  assert.equal(nodes(view, (node) => node.tag === 'button' && text(node) === '取消执行').length, 0);
  assert.equal(nodes(view, (node) => node.tag === 'button' && text(node) === '打开执行报告').length, 1);
  app.dispose();
});

test('binding choice includes the observed selection revision and never claims next work', async () => {
  const app = harness(({ options }) => response(options.method === 'POST' ? wire({}) : context('session-a')));
  const props = app.injected();
  const off = props.autoskill.store.subscribe(() => undefined);
  await setImmediate();
  click(app.dock.component(props), '第二任务');
  await setImmediate();
  const sent = JSON.parse(app.requests.find((request) => request.options.method === 'POST').options.body);
  assert.equal(sent.operation, 'binding.select');
  assert.equal(sent.payload.binding_id, 'binding-b');
  assert.equal(sent.payload.expected_revision, 3);
  assert.ok(sent.request_id);
  assert.ok(app.requests.every((request) => !request.options.body?.includes('workflow.next')));
  off(); app.dispose();
});

test('report links appear only after same-report verification and include a real Bug artifact', async () => {
  const reportId = 'run_old-run';
  const receipt = { report_id: reportId, status: 'ready', excel: '/frozen/cases.xlsx', markdown: '/frozen/summary.md', sha256: 'excel-hash',
    markdown_sha256: 'md-hash', bugs: '/frozen/bugs.md', bugs_sha256: 'bugs-hash' };
  const app = harness(({ url }) => response(url.searchParams.get('resource') === 'report' ? wire(receipt)
    : context('session-a', { reports: [{ report_id: reportId, receipt_status: 'ready', verification: 'not_checked' }] })));
  const props = app.injected();
  const off = props.autoskill.store.subscribe(() => undefined);
  await setImmediate();
  assert.equal(nodes(app.dock.component(props), (node) => node.tag === 'a').length, 0);
  click(app.dock.component(props), '校验并获取报告');
  await setImmediate();
  const links = nodes(app.dock.component(props), (node) => node.tag === 'a');
  assert.deepEqual(links.map(text), ['下载 Excel', '下载 Markdown 汇总', '下载 Bug 清单']);
  for (const link of links) {
    const url = new URL(link.props.href, 'http://dsh.test/nested/');
    assert.equal(url.pathname, '/nested/api/autoskill.report');
    assert.equal(url.searchParams.get('report_id'), reportId);
    assert.equal(url.searchParams.get('session_id'), 'session-a');
  }
  off(); app.dispose();
});

test('changed or missing artifacts never inherit ready state from the context listing', async () => {
  const app = harness(({ url }) => response(url.searchParams.get('resource') === 'report'
    ? wire({ report_id: 'run_a', status: 'failed', error: '文件已变化' })
    : context('session-a', { reports: [{ report_id: 'run_a', receipt_status: 'ready' }] })));
  const props = app.injected();
  const off = props.autoskill.store.subscribe(() => undefined);
  await setImmediate();
  await props.autoskill.store.verifyReport('run_a');
  const view = app.dock.component(props);
  assert.equal(nodes(view, (node) => node.tag === 'a').length, 0);
  assert.match(text(view), /文件已变化/);
  assert.match(text(view), /重试导出报告/);
  off(); app.dispose();
});

test('an older verified receipt keeps its two real links without inventing a Bug file', async () => {
  const app = harness(({ url }) => response(url.searchParams.get('resource') === 'report'
    ? wire({ report_id: 'run_old', status: 'ready', excel: '/old.xlsx', markdown: '/old.md', sha256: 'e', markdown_sha256: 'm' })
    : context('session-a', { reports: [{ report_id: 'run_old', receipt_status: 'ready' }] })));
  const props = app.injected();
  const off = props.autoskill.store.subscribe(() => undefined);
  await setImmediate();
  await props.autoskill.store.verifyReport('run_old');
  assert.deepEqual(nodes(app.dock.component(props), (node) => node.tag === 'a').map(text), ['下载 Excel', '下载 Markdown 汇总']);
  off(); app.dispose();
});

test('unknown tool history can query its own receipt without issuing a new task', async () => {
  const app = harness(() => response(wire(wire({}, { request_state: 'completed', request_id: 'historic-request' }))));
  const props = app.injected();
  const view = app.taskCard.component({ ...props, phase: 'result', toolName: 'autoskill_task', block: {
    meta: { autoskill: { host_session_id: 'session-a', binding_id: 'old-binding', run_id: 'old-run',
      operation: 'run.cancel', request_id: 'historic-request', request_state: 'unknown' } }, content: [],
  } });
  click(view, '查询操作结果');
  await setImmediate();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].options.method, 'GET');
  assert.equal(app.requests[0].url.searchParams.get('request_id'), 'historic-request');
  assert.equal(props.autoskill.store.getSnapshot().actions['run.cancel:old-run'].state, 'completed');
  app.dispose();
});

test('binding and run pagination use independent cursors without changing the selected binding', async () => {
  const app = harness(({ url }) => response(context(url.searchParams.get('session_id'))));
  const store = app.injected().autoskill.store;
  const off = store.subscribe(() => undefined);
  await setImmediate();
  store.page('bindings', 'bindings-next');
  await setImmediate();
  store.page('runs', 'runs-next');
  await setImmediate();
  const last = app.requests.at(-1).url;
  assert.equal(last.searchParams.get('cursor'), 'bindings-next');
  assert.equal(last.searchParams.get('runs_cursor'), 'runs-next');
  assert.equal(store.getSnapshot().data.selection.binding_id, 'binding-a');
  assert.equal(app.requests.filter((request) => request.options.method === 'POST').length, 0);
  off(); app.dispose();
});

test('unloading aborts pending writes and never leaves polling or completion notifications running', async () => {
  let finish;
  const app = harness(() => new Promise((resolve) => { finish = resolve; }));
  const store = app.injected().autoskill.store;
  const pending = store.command('run.cancel', { run_id: 'run-a' }, 'binding-a');
  app.dispose();
  assert.equal(app.requests[0].options.signal.aborted, true);
  finish(response(wire({})));
  await pending;
  assert.equal(app.notifications.length, 0);
  assert.equal(app.timers.size, 0);
});
