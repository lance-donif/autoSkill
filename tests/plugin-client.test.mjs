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
const context = (sessionId, extra = {}) => ({ version: 1, host_session_id: sessionId, status: 'bound', context_revision: JSON.stringify(extra), state_revision: JSON.stringify(extra),
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
  const taskCard = slots.find((entry) => entry.options.key === 'autoskill_task');
  const injected = (sessionId = 'session-a') => taskCard.options.inject(sessionId);
  return { slots, requests, notifications, panels, events, timers, taskCard, injected, dependencies: plugin.inject,
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

function reportCard(app, props, reportId) {
  return app.taskCard.component({ ...props, phase: 'result', toolName: 'autoskill_read', block: {
    content: [{ type: 'text', text: JSON.stringify(wire({}, { identity: { host_session_id: 'session-a', binding_id: 'binding-a', report_id: reportId } })) }],
  } });
}

test('native cards and sidebar panels register without any conversation task dock or passive polling', () => {
  const app = harness();
  assert.deepEqual(app.slots.filter((slot) => slot.options.name === 'tool.call.toolview').map((slot) => slot.options.key), ['autoskill_read', 'autoskill_task']);
  assert.equal(app.slots.some((slot) => slot.options.name === 'conversation.input.dock'), false);
  assert.equal(app.slots.some((slot) => slot.options.name === 'conversation.input.activity'), false);
  assert.deepEqual(Array.from(app.dependencies), ['slots', 'layout']);
  assert.notEqual(app.injected('session-a').autoskill.store, app.injected('session-b').autoskill.store);
  assert.equal(app.injected('session-a').autoskill.store, app.injected('session-a').autoskill.store);
  assert.deepEqual(app.slots.filter((slot) => slot.options.name === 'sidebar.panellist').map((slot) => slot.options.id), ['autoskill-dashboard', 'autoskill-schedules']);
  assert.equal(app.requests.length, 0);
  assert.equal(app.timers.size, 0);
  app.dispose();
});

test('empty conversations show one activation status from their own latest successful command', () => {
  const app = harness();
  const entries = app.slots.filter((slot) => slot.options.name === 'conversation.hero.options');
  assert.equal(entries.length, 1);
  const { component: Status } = entries[0];
  const confirmation = '已加载 autoSkill 技能。请直接描述要完成的任务。';
  const success = { kind: 'success', text: confirmation };
  const command = (name, outcome) => ({ kind: 'command', data: { name, outcome } });
  const activation = (outcome) => command('autoskill-use', outcome);
  const render = (commands, activeTargets = []) => {
    const nodes = new Map(commands?.map((node, index) => [String(index), node]));
    const chat = commands && { order: [...nodes.keys()], nodes };
    return Status({ useConversation: (select) => select({
      activeTargets: new Set(activeTargets), views: new Map([['chat', chat]]),
    }) });
  };

  assert.equal(render(undefined), null);
  assert.equal(render([]), null);
  assert.equal(render([command('export', success)]), null);
  assert.equal(render([activation(null)]), null);
  assert.equal(render([activation({ kind: 'success' })]), null);
  assert.equal(render([activation(success), activation({ kind: 'error', text: '未加载' })]), null);
  assert.equal(render([activation(success)], ['chat']), null);

  const status = render([activation(success), activation(success), command('export', success)]);
  assert.equal(status.props.role, 'status');
  assert.deepEqual(Array.from(status.props.children), [confirmation]);
  assert.equal(render([]), null);
  assert.equal(render([activation(success)]).props.children[0], confirmation);
  assert.deepEqual(app.requests, []);
  assert.deepEqual(app.panels, []);
  assert.deepEqual(app.notifications, []);
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

test('only requested report reads run, delayed receipts stay in their owning session, and subscribing never polls', async () => {
  const waiting = new Map();
  const app = harness(({ url }) => new Promise((resolve) => waiting.set(url.searchParams.get('session_id'), resolve)));
  const first = app.injected('session-a').autoskill.store;
  const second = app.injected('session-b').autoskill.store;
  const offFirst = first.subscribe(() => undefined);
  const offSecond = second.subscribe(() => undefined);
  assert.equal(app.requests.length, 0);
  const readFirst = first.verifyReport('run_a');
  const readSecond = second.verifyReport('run_b');
  const receipt = (reportId) => ({ report_id: reportId, status: 'ready', excel: '/cases.xlsx', markdown: '/summary.md', bugs: '/bugs.md',
    sha256: 'e', markdown_sha256: 'm', bugs_sha256: 'b' });
  waiting.get('session-b')(response(wire(receipt('run_b'))));
  waiting.get('session-a')(response(wire(receipt('run_a'))));
  await Promise.all([readFirst, readSecond]);
  assert.equal(first.getSnapshot().reports.run_a.state, 'ready');
  assert.equal(second.getSnapshot().reports.run_b.state, 'ready');
  assert.equal(first.getSnapshot().reports.run_b, undefined);
  assert.ok(app.requests.every((request) => request.url.pathname === '/nested/api/autoskill.read'));
  assert.ok(app.requests.every((request) => request.options.credentials === 'same-origin'));
  offFirst(); offSecond();
  assert.equal(app.timers.size, 0);
  app.dispose();
});

test('an inactive host route shows a useful unavailable message instead of a JSON parse exception', async () => {
  const app = harness(() => ({ ok: false, status: 404, json: () => Promise.reject(new SyntaxError('Unexpected token in not found')) }));
  const store = app.injected().autoskill.store;
  await store.verifyReport('run_a');
  assert.match(store.getSnapshot().reports.run_a.error, /autoSkill 服务暂不可用.*404.*插件/);
  assert.doesNotMatch(store.getSnapshot().reports.run_a.error, /Unexpected token/);
  assert.equal(app.notifications.length, 0);
  app.dispose();
});

test('disconnect invalidates pending responses and unloading aborts requested reads without restarting them', async () => {
  const waiting = [];
  const app = harness(() => new Promise((resolve) => waiting.push(resolve)));
  const store = app.injected().autoskill.store;
  const oldRead = store.verifyReport('run_old');
  app.events.get('connection/reset')();
  assert.equal(app.requests[0].options.signal.aborted, true);
  assert.equal(app.requests.length, 1);
  const newRead = store.verifyReport('run_new');
  waiting[0](response(wire({ report_id: 'run_old', status: 'failed', error: 'old response' })));
  waiting[1](response(wire({ report_id: 'run_new', status: 'failed', error: 'new response' })));
  await Promise.all([oldRead, newRead]);
  assert.equal(store.getSnapshot().reports.run_old, undefined);
  assert.equal(store.getSnapshot().reports.run_new.error, 'new response');
  const late = store.verifyReport('run_late');
  app.dispose();
  assert.equal(app.requests.at(-1).options.signal.aborted, true);
  waiting[2](response(wire({ report_id: 'run_late', status: 'failed', error: 'late response' })));
  await late;
  assert.equal(store.getSnapshot().reports.run_late.state, 'checking');
  assert.equal(app.timers.size, 0);
  app.dispose();
});

test('completed, failed and uncertain actions keep their receipts without notifications or automatic navigation', async () => {
  let state = 'completed';
  const app = harness(() => response(wire({ status: 'Fail', business_status: 'Fail' }, { request_state: state })));
  const store = app.injected('session-a').autoskill.store;
  const off = store.subscribe(() => undefined);
  for (const current of ['completed', 'failed', 'unknown']) {
    state = current;
    const action = await store.command('run.cancel', { run_id: 'run-' + current }, 'binding-a');
    assert.equal(action.state, current);
    assert.equal(action.result.data.business_status, 'Fail');
  }
  assert.equal(app.notifications.length, 0);
  assert.deepEqual(app.panels, []);
  assert.equal(app.timers.size, 0);
  off(); app.dispose();
});

test('a completed workflow retains its violated business conclusion in a closed inline record without progress popups', () => {
  const app = harness();
  const props = app.injected();
  const view = app.taskCard.component({ ...props, phase: 'result', toolName: 'autoskill_read', block: {
    content: [{ type: 'text', text: JSON.stringify(wire({ conclusion: 'violated' }, { identity: { host_session_id: 'session-a', workflow_id: 'flow-a' } })) }],
  } });
  assert.match(text(view), /不符合预期/);
  assert.equal(view.tag, 'details');
  assert.equal(view.props.open, undefined);
  assert.equal(view.props['aria-label'], 'autoSkill 工具结果');
  assert.equal(app.notifications.length, 0);
  assert.deepEqual(app.panels, []);
  app.dispose();
});

test('recorded execution receipts show their nested run status, including after reopening the conversation', () => {
  const app = harness();
  const props = app.injected();
  const identity = { host_session_id: 'session-a', run_id: 'run-original' };
  const receipt = (status) => wire({ state: 'accepted', run: { id: 'run-original', status } }, { identity, operation: 'run.start' });
  const card = (result) => app.taskCard.component({ ...props, phase: 'result', toolName: 'autoskill_read', block: {
    content: [{ type: 'text', text: JSON.stringify(result) }],
  } });
  const queued = card(receipt('Queued'));
  assert.match(text(queued), /执行状态：排队中/);
  assert.match(text(queued), /取消执行/);
  const reopened = card(wire(receipt('Fail'), { operation: 'operation' }));
  assert.match(text(reopened), /执行状态：失败/);
  assert.match(text(reopened), /打开执行报告/);
  assert.doesNotMatch(text(reopened), /取消执行/);
  const removed = card(wire({ state: 'deleted', run: null }, { identity, operation: 'run.start' }));
  assert.match(text(removed), /执行记录已删除/);
  assert.doesNotMatch(text(removed), /打开执行报告|取消执行/);
  app.dispose();
});

test('two lost run receipts in the same conversation recover their own run and report navigation', async () => {
  const app = harness(({ url }) => {
    const requestId = url.searchParams.get('request_id');
    const runId = 'run-' + requestId;
    return response(wire(wire({ state: 'accepted', run: { id: runId, status: 'Pass' } }, {
      operation: 'run.start', request_id: requestId, identity: { host_session_id: 'session-a', run_id: runId },
    }), { operation: 'operation' }));
  });
  const props = app.injected();
  const card = (requestId) => app.taskCard.component({ ...props, phase: 'result', toolName: 'autoskill_task', block: {
    content: [{ type: 'text', text: JSON.stringify(wire(null, { operation: 'run.start', request_id: requestId,
      request_state: 'unknown' })) }],
  } });
  click(card('first'), '查询操作结果');
  click(card('second'), '查询操作结果');
  await setImmediate();
  for (const id of ['first', 'second']) {
    const recovered = card(id);
    assert.match(text(recovered), new RegExp('执行：run-' + id));
    assert.match(text(recovered), /执行状态：通过/);
    assert.doesNotMatch(text(recovered), /尚未确认|取消执行/);
    click(recovered, '打开执行报告');
    assert.equal(props.autoskill.navigation.route('autoskill-dashboard'), 'runs/run-' + id);
  }
  assert.deepEqual(app.requests.filter((request) => request.url.searchParams.get('resource') === 'operation')
    .map((request) => request.url.searchParams.get('request_id')), ['first', 'second']);
  app.dispose();
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
  assert.equal(app.notifications.length, 0);
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
  await props.autoskill.store.command('binding.select', { binding_id: 'binding-b', expected_revision: 3 }, 'binding-b');
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
  assert.equal(nodes(reportCard(app, props, reportId), (node) => node.tag === 'a').length, 0);
  click(reportCard(app, props, reportId), '校验并获取报告');
  await setImmediate();
  const links = nodes(reportCard(app, props, reportId), (node) => node.tag === 'a');
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
  const view = reportCard(app, props, 'run_a');
  assert.equal(nodes(view, (node) => node.tag === 'a').length, 0);
  assert.match(text(view), /文件已变化/);
  assert.match(text(view), /重试导出报告/);
  off(); app.dispose();
});

test('a receipt missing a required Bug artifact cannot become ready', async () => {
  const app = harness(({ url }) => response(url.searchParams.get('resource') === 'report'
    ? wire({ report_id: 'run_old', status: 'ready', excel: '/old.xlsx', markdown: '/old.md', sha256: 'e', markdown_sha256: 'm' })
    : context('session-a', { reports: [{ report_id: 'run_old', receipt_status: 'ready' }] })));
  const props = app.injected();
  const off = props.autoskill.store.subscribe(() => undefined);
  await setImmediate();
  await props.autoskill.store.verifyReport('run_old');
  assert.equal(props.autoskill.store.getSnapshot().reports.run_old.state, 'failed');
  assert.deepEqual(nodes(reportCard(app, props, 'run_old'), (node) => node.tag === 'a').map(text), []);
  assert.match(text(reportCard(app, props, 'run_old')), /重试导出报告/);
  off(); app.dispose();
});

test('unknown tool history can query its own receipt without issuing a new task', async () => {
  const identity = { host_session_id: 'session-a', binding_id: 'old-binding', run_id: 'old-run' };
  const app = harness(() => response(wire(wire({}, { request_id: 'historic-request', identity, operation: 'run.cancel' }))));
  const props = app.injected();
  const view = () => app.taskCard.component({ ...props, phase: 'result', toolName: 'autoskill_task', block: {
    meta: { autoskill: { ...identity, operation: 'run.cancel', request_id: 'historic-request', request_state: 'unknown' } }, content: [],
  } });
  click(view(), '查询操作结果');
  await setImmediate();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].options.method, 'GET');
  assert.equal(app.requests[0].url.searchParams.get('request_id'), 'historic-request');
  assert.match(text(view()), /工具操作已完成.*执行：old-run/);
  app.dispose();
});

test('rendering and subscribing to native tool results never reads a task, claims work or starts progress timers', () => {
  const app = harness(({ url }) => response(context(url.searchParams.get('session_id'))));
  const store = app.injected().autoskill.store;
  const off = store.subscribe(() => undefined);
  reportCard(app, app.injected(), 'run_a');
  assert.equal(app.requests.length, 0);
  assert.equal(app.timers.size, 0);
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

test('inline state axes retain the failed SLA and invalid evidence independently of delivery', () => {
  const app = harness();
  const data = { id: 'run-a', status: 'Running', conclusion: 'satisfied', state_counts: { pass: 1, fail: 1, not_run: 2 }, state: {
    lifecycle: 'completed', phase: 'settled', outcome: 'fail',
    checks: { business: 'pass', performance: 'fail', quality: 'unverified' }, evidence: 'invalid', report: 'failed', delivery: 'delivered',
    scope: { historical: true, cycle_id: 'cycle-old', current_cycle_id: 'cycle-current' },
  } };
  const view = app.taskCard.component({ ...app.injected(), phase: 'result', toolName: 'autoskill_read', block: {
    content: [{ type: 'text', text: JSON.stringify(wire(data, { identity: { host_session_id: 'session-a', run_id: 'run-a' } })) }],
  } });
  const rendered = text(view);
  for (const fact of ['当前阶段：已结束', '综合结果：失败', '业务验证：通过', '性能：失败', '质量：未验证', '用例结果：通过 1 · 失败 1 · 未执行 2',
    '证据：已失效', '报告：失败', '投递：已送达', '历史周期：cycle-old', '当前周期：cycle-current']) assert.ok(rendered.includes(fact), fact);
  assert.doesNotMatch(rendered, /业务验证：符合预期/);
  assert.equal(nodes(view, (node) => node.tag === 'button' && text(node) === '取消执行').length, 0);
  assert.equal(view.tag, 'details');
  assert.equal(view.props.open, undefined);
  assert.equal(app.requests.length, 0);
  assert.equal(app.timers.size, 0);
  assert.equal(app.notifications.length, 0);
  app.dispose();
});

test('a selected historical run in an inline query keeps its own state facts', () => {
  const app = harness();
  const state = { lifecycle: 'completed', phase: 'settled', outcome: 'unverified',
    checks: { business: 'unverified', performance: 'not_applicable', quality: 'not_applicable' },
    evidence: 'unverified', report: 'ready', delivery: 'delivered' };
  const view = app.taskCard.component({ ...app.injected(), phase: 'result', toolName: 'autoskill_read', block: {
    content: [{ type: 'text', text: JSON.stringify(wire({ runs: { items: [{ id: 'run-a', state, can_cancel: false }] } },
      { identity: { host_session_id: 'session-a', run_id: 'run-a' } })) }],
  } });
  assert.match(text(view), /综合结果：未验证/);
  assert.match(text(view), /投递：已送达/);
  assert.doesNotMatch(text(view), /综合结果：通过/);
  assert.equal(app.requests.length, 0);
  app.dispose();
});

test('report retry is explicit and cannot replay a test after verification fails', async () => {
  const app = harness(({ options }) => response(options.method === 'POST' ? wire({})
    : wire({ report_id: 'run-a', status: 'failed', error: '证据文件已变化' })));
  const props = app.injected();
  await props.autoskill.store.verifyReport('run-a');
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].options.method, 'GET');
  const view = reportCard(app, props, 'run-a');
  assert.match(text(view), /证据文件已变化/);
  click(view, '重试导出报告');
  await setImmediate();
  assert.equal(app.requests.length, 2);
  const sent = JSON.parse(app.requests[1].options.body);
  assert.equal(sent.operation, 'report.retry');
  assert.equal(sent.payload.report_id, 'run-a');
  assert.equal(app.requests.some((request) => request.url.pathname.includes('rerun')), false);
  assert.equal(app.timers.size, 0);
  assert.equal(app.notifications.length, 0);
  app.dispose();
});
