// Exercise real local HTTP and filesystem transport; SDK execution is tested separately.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { HostService } from '../plugin/host-service.mjs';
import { registerRoutes } from '../plugin/host-routes.mjs';
import { prepareSources, reportFiles } from '../plugin/host-files.mjs';
import { toolDefinitions } from '../plugin/host-tools.mjs';
import { runtimeContext } from '../plugin/index.mjs';
import { HostAdapter } from '../plugin/host-adapter.mjs';

function contextFor(id) {
  return { status: 'ready', context_revision: 'rev:' + id, binding: { id: 'binding:' + id, task_session_id: '任务:' + id, project_id: 'project', workflow_id: 'wf:' + id },
    selection: { binding_id: 'binding:' + id, revision: 1 }, bindings: { items: [], page: { total: 1 } }, runs: { items: [], page: { total: 0 } }, reports: [] };
}

async function bodyOf(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString();
  return text ? JSON.parse(text) : null;
}

function application(state, url, body) {
  if (url.pathname === '/api/host/context') return ['1', 'true'].includes(url.searchParams.get('revision_only'))
    ? { host_session_id: url.searchParams.get('host_session_id'), state_revision: 'opaque-workspace-revision' }
    : contextFor(url.searchParams.get('host_session_id'));
  if (url.pathname === '/api/host/requests') return state.receipt;
  if (url.pathname === '/api/execution-requests') return body ? { ...state.execution, scope: body.scope, idempotency_key: body.idempotency_key } : state.execution;
  if (url.pathname === '/api/host/bindings') return { binding: { id: 'new', ...body }, selection: { binding_id: null, revision: 0 } };
  if (url.pathname.startsWith('/api/workflows/')) return state.workflow;
  return { ok: true };
}

async function fixture(t, options = {}) {
  const workspace = await mkdtemp(join(tmpdir(), 'autoskill host backend-'));
  const state = { requests: [], receipt: { state: 'completed', response: { task: { id: 'leased-item' } } },
    workflow: { task: { id: 'leased-item' } }, execution: { state: 'accepted', request_id: 'execution-1', run_id: 'run-1', run: { id: 'run-1', status: 'Queued' } } };
  const server = createServer((request, response) => {
    void (async () => {
      const body = await bodyOf(request);
      const url = new URL(request.url, 'http://localhost');
      state.requests.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), headers: request.headers, body });
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(application(state, url, body)));
    })().catch((error) => { response.writeHead(500); response.end(JSON.stringify({ error: error.message })); });
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  t.after(async () => { server.closeAllConnections(); await new Promise((done) => server.close(done)); await rm(workspace, { recursive: true, force: true }); });
  const spec = { workspace, cwd: join(workspace, 'program'), env: { AUTOSKILL_WORKSPACE: join(workspace, 'wrong') }, origin: 'http://127.0.0.1:' + server.address().port };
  return { state, workspace, spec, service: new HostService(spec, options) };
}

const agent = (id) => ({ session: { id } });
const start = (requestId, extra = {}) => ({ operation: 'run.start', request_id: requestId, payload: { request: { family: 'replay', project_id: 'project', engine: 'playwright', ...extra } } });
const writes = (state, path) => state.requests.filter((request) => request.path === path && request.body);

test('confirmed data root wins over program location and inherited workspace in every host adapter', async (t) => {
  const { spec, workspace, service } = await fixture(t);
  const adapter = new HostAdapter({ controller: {}, sessions: {} }, spec);
  assert.equal(adapter.workspace, workspace);
  assert.equal(service.workspace, workspace);
  assert.equal(JSON.parse(Buffer.from(runtimeContext(spec), 'base64url').toString()).workspace, workspace);
});

test('two host sessions retain separate task identities through concurrent real HTTP calls', async (t) => {
  const { service, state, workspace } = await fixture(t);
  const outputs = await Promise.all([service.task(agent('甲'), start('same-id')), service.task(agent('乙/../../'), start('same-id'))]);
  assert.deepEqual(outputs.map((output) => output.identity.task_session_id), ['任务:甲', '任务:乙/../../']);
  const sent = writes(state, '/api/execution-requests');
  assert.deepEqual(sent.map((request) => request.body.request.session_id).sort(), ['任务:乙/../../', '任务:甲'].sort());
  assert.ok(sent.every((request) => !('host' in request.body.request)));
  const files = await readdir(join(workspace, '.local', 'host-operations', 'dsh'));
  assert.equal(files.length, 2);
  assert.ok(files.every((file) => /^[a-f0-9]{64}\.json$/.test(file)));
});

test('opaque revision reads retain the owning session through the same host transport', async (t) => {
  const { service, state } = await fixture(t);
  const revision = await service.context(agent('session-A'), { revision_only: true, url: 'https://unrelated.invalid' });
  assert.deepEqual(revision, { version: 1, host_session_id: 'session-A', state_revision: 'opaque-workspace-revision' });
  assert.deepEqual(state.requests[0].query, { revision_only: '1', host: 'dsh', host_session_id: 'session-A' });
  assert.equal(state.requests[0].body, null);
  assert.ok(!('binding' in revision) && !('runs' in revision) && !('reports' in revision));
});

test('concurrent retries and reordered object keys retain one accepted request', async (t) => {
  const { service, state } = await fixture(t);
  const one = start('repeat');
  const two = { payload: { request: { engine: 'playwright', project_id: 'project', family: 'replay' } }, request_id: 'repeat', operation: 'run.start' };
  const [a, b] = await Promise.all([service.task(agent('session'), one), service.task(agent('session'), two)]);
  assert.equal(a.data.run_id, b.data.run_id);
  assert.equal(writes(state, '/api/execution-requests').length, 1);
  await assert.rejects(service.task(agent('session'), start('repeat', { engine: 'selenium' })), { code: 'idempotency_conflict' });
});

test('lost enqueue response is recovered by GET and never reenqueued', async (t) => {
  let lost = false;
  const { service, state } = await fixture(t, { fetch: async (url, options) => {
    const response = await fetch(url, options);
    if (!lost && options.method === 'POST') { lost = true; await response.arrayBuffer(); throw new Error('synthetic disconnected response'); }
    return response;
  } });
  const unknown = await service.task(agent('session'), start('lost'));
  assert.equal(unknown.request_state, 'unknown');
  const recovered = await service.read(agent('session'), { resource: 'operation', query: { request_id: 'lost' } });
  assert.equal(recovered.data.request_state, 'completed');
  assert.equal(recovered.data.data.run_id, 'run-1');
  await service.task(agent('session'), start('lost'));
  assert.equal(writes(state, '/api/execution-requests').length, 1);
});

test('unknown workflow claim queries its original receipt scope without another next', async (t) => {
  let lost = false;
  const { service, state } = await fixture(t, { fetch: async (url, options) => {
    const response = await fetch(url, options);
    if (!lost && options.method === 'POST') { lost = true; await response.arrayBuffer(); throw new Error('lost'); }
    return response;
  } });
  const args = { operation: 'workflow.next', request_id: 'claim', payload: {} };
  assert.equal((await service.task(agent('A'), args)).request_state, 'unknown');
  assert.equal((await service.task(agent('A'), args)).data.task.id, 'leased-item');
  const lookup = state.requests.find((request) => request.path === '/api/host/requests');
  assert.deepEqual(lookup.query, { host: 'dsh', host_session_id: 'A', key: 'claim', kind: 'workflow', scope: 'wf:A' });
  assert.equal(writes(state, '/api/workflows/wf%3AA/next').length, 1);
});

test('a deleted or missing accepted run is reported without recreating its request', async (t) => {
  const { service, state } = await fixture(t);
  const args = start('removed');
  await service.task(agent('A'), args);
  state.execution = { ...state.execution, state: 'deleted', run: null };
  assert.equal((await service.task(agent('A'), args)).data.state, 'deleted');
  state.execution = { ...state.execution, state: 'missing' };
  assert.equal((await service.read(agent('A'), { resource: 'operation', query: { request_id: 'removed' } })).data.data.state, 'missing');
  assert.equal(writes(state, '/api/execution-requests').length, 1);
});

test('an already aborted call sends no write and native run metadata uses the real host identity', async (t) => {
  const { service, state } = await fixture(t);
  await assert.rejects(service.task(agent('A'), start('cancelled'), AbortSignal.abort()));
  assert.equal(state.requests.length, 0);
  await service.task(agent('A'), { operation: 'run.start', request_id: 'native', payload: { request: { family: 'native', project_id: 'project', asset_id: 'asset', host: 'forged', host_session_id: 'other' } } });
  const request = writes(state, '/api/execution-requests')[0].body.request;
  assert.equal(request.host, 'dsh');
  assert.equal(request.host_session_id, 'A');
});

function filesystem() {
  return { processPathFromHostPath: (path) => path, processPath: (target) => target,
    lstat: async (path) => ({ type: (await stat(path)).isFile() ? 'file' : 'directory' }),
    resolve: (path, options) => Promise.resolve(resolve(options.cwd ?? '.', path)),
    stat: async (path) => ({ type: 'file', size: (await stat(path)).size }), readBytes: (path) => readFile(path) };
}

test('report parameters require actual host-readable bytes and never claim present delivery', async (t) => {
  const { workspace } = await fixture(t);
  const receipt = { status: 'ready' };
  for (const format of ['excel', 'markdown', 'bugs']) {
    const content = 'real bytes ' + format;
    const file = join(workspace, format);
    await writeFile(file, content);
    receipt[format] = file;
    receipt[format === 'excel' ? 'sha256' : format + '_sha256'] = createHash('sha256').update(content).digest('hex');
  }
  const actual = await reportFiles(receipt, { fs: filesystem(), cwd: workspace });
  assert.equal(actual.status, 'verified');
  assert.equal(actual.presented, false);
  assert.equal(actual.present_input.files.length, 3);
  const legacy = await reportFiles({ ...receipt, bugs: undefined }, { fs: filesystem(), cwd: workspace });
  assert.equal(legacy.status, 'unavailable');
  assert.equal(legacy.present_input, undefined);
  await writeFile(receipt.excel, 'changed');
  const unavailable = await reportFiles(receipt, { fs: filesystem(), cwd: workspace });
  assert.equal(unavailable.status, 'unavailable');
  assert.equal(unavailable.present_input, undefined);
});

test('explicit source files retain bytes in the selected data directory and unrelated file keys stay unchanged', async (t) => {
  const { workspace } = await fixture(t);
  const input = join(workspace, '原始需求.txt');
  await writeFile(input, '保留原始事实\n第二行');
  const output = await prepareSources({ spec: { sources: [{ file: input }], custom: { file: 'business data' } } },
    { fs: filesystem(), cwd: workspace, folder: join(workspace, '.local', 'inputs') });
  assert.equal(await readFile(output.spec.sources[0].file, 'utf8'), '保留原始事实\n第二行');
  assert.equal(output.spec.sources[0].uri, input);
  assert.equal(output.spec.custom.file, 'business data');
});

test('same-origin routes pass explicit session identity and expose no arbitrary URL proxy', async () => {
  const routes = new Map();
  const calls = [];
  const ctx = { effect: (create) => create(), connection: { fetch: { register: (route) => { routes.set(route.path, route); return () => Promise.resolve(); } } } };
  registerRoutes(ctx, { spec: { origin: 'http://127.0.0.1:38817' }, context: (id, query) => { calls.push({ id, query }); return Promise.resolve(contextFor(id)); },
    read: (_id, args) => Promise.resolve(args), download: (_id, _report, format) => Promise.resolve(new Response(format)),
    task: (id, args) => { calls.push({ id, args }); return Promise.resolve({ request_state: 'unknown' }); } });
  const response = await routes.get('/api/autoskill.context').fetch(new Request('https://host/api/autoskill.context?session_id=A&runs_limit=3'));
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.deepEqual(calls[0], { id: 'A', query: { runs_limit: 3 } });
  await routes.get('/api/autoskill.context').fetch(new Request('https://host/api/autoskill.context?session_id=B&revision_only=true'));
  assert.deepEqual(calls[1], { id: 'B', query: { revision_only: true } });
  const metadata = await routes.get('/api/autoskill.host').fetch(new Request('https://host/api/autoskill.host'));
  assert.deepEqual(await metadata.json(), { version: 1, dashboard_origin: 'http://127.0.0.1:38817' });
  assert.ok([...routes.values()].every((route) => route.path.startsWith('/api/') && route.requestBody === 'buffered'));
  const invalid = await routes.get('/api/autoskill.read').fetch(new Request('https://host/api/autoskill.read?resource=report'));
  assert.equal(invalid.status, 400);
});

test('native tool definitions require an owning Agent and keep business failure in the service result', async () => {
  const definitions = toolDefinitions({ read: () => Promise.resolve({ version: 1, operation: 'run', request_state: 'completed', identity: { run_id: 'run' }, data: { status: 'Fail' } }) });
  const read = definitions[0];
  await assert.rejects(read.execute({ resource: 'run' }, { signal: new AbortController().signal }), /会话身份/);
  const result = await read.execute({ resource: 'run' }, { agent: agent('A'), signal: new AbortController().signal });
  assert.equal(result.data.status, 'Fail');
  assert.equal(read.output.presentationMeta({}, result).autoskill.run_id, 'run');
  assert.match(read.output.render({}, result)[0].text, /Fail/);
});
