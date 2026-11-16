// Real DSH 0.2.2-rc.1 SDK integration, without a remote model or DSH Web UI.
// Set AUTOSKILL_DSH_SDK_ROOT to a provisioned directory containing node_modules.
// This file also exports fixtures for dsh-session-context.test.mjs; imports do not rerun its tests.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const executeFile = promisify(execFile);
const sdkRequire = createRequire(process.env.AUTOSKILL_DSH_SDK_ROOT
  ? join(resolve(process.env.AUTOSKILL_DSH_SDK_ROOT), 'package.json') : import.meta.url);
export let sdkSkip;
try { sdkRequire.resolve('@deepseek-ai/dsh-tools'); }
catch (error) {
  if (process.env.AUTOSKILL_DSH_SDK_ROOT) throw error;
  sdkSkip = 'Real DSH SDK unavailable; set AUTOSKILL_DSH_SDK_ROOT. This skip is not SDK acceptance.';
}
export async function sdk(name) {
  const id = '@deepseek-ai/' + name;
  if (name.startsWith('dsh-')) {
    const manifest = JSON.parse(await readFile(sdkRequire.resolve(id + '/package.json'), 'utf8'));
    assert.equal(manifest.version, '0.2.2-rc.1', `${id}: this acceptance is pinned to the reviewed SDK`);
  }
  return import(pathToFileURL(sdkRequire.resolve(id)).href);
}

export async function sdkHost(t, cwd) {
  const { Context } = await sdk('cordis');
  const ctx = new Context();
  t.after(() => ctx.fiber.dispose());
  for (const name of ['dsh-system-prompt', 'dsh-tools', 'dsh-session-projection', 'dsh-agent',
    'dsh-session', 'dsh-llm', 'dsh-agent-loop', 'dsh-fs-local', 'dsh-working-directory',
    'dsh-subprocess-local', 'dsh-sandbox-local', 'dsh-sandbox-policy', 'dsh-ptc-runtime-node',
    'dsh-tool-executor-native', 'dsh-commands', 'dsh-tool-present', 'dsh-skill']) {
    const module = await sdk(name);
    const config = name === 'dsh-sandbox-policy' ? { mode: 'danger-full-access', workspaceRoot: cwd }
      : name === 'dsh-fs-local' ? { cwd }
      : name === 'dsh-working-directory' ? { defaultDirectory: cwd } : {};
    await ctx.plugin(module.default ?? module, config);
  }
  async function agent(id) {
    const handle = await ctx.agents.create({ sessionId: id, meta: { cwd } });
    assert.equal(handle.agent.constructor.name, 'ReactLoopAgent');
    assert.equal(ctx.agents.get(id), handle.agent);
    return handle.agent;
  }
  let callSequence = 0;
  async function call(owner, name, args, options = {}) {
    const callId = options.callId ?? `autoskill-sdk-${++callSequence}`;
    const batch = await ctx.tools.execute({
      calls: [{ type: 'tool_call', id: callId, name, arguments: JSON.stringify(args) }],
      signal: options.signal ?? new AbortController().signal,
      ...(owner ? { agent: owner } : {}), ...(options.log ? { log: options.log } : {}),
    });
    assert.equal(batch.results.length, 1);
    assert.equal(batch.results[0].callId, callId);
    return batch.results[0].result;
  }
  async function ptc(owner) {
    return owner.ctx.plugin(await sdk('dsh-tool-executor-ptc'));
  }
  return { ctx, agent, call, ptc };
}

const KERNEL_FIXTURE = `
import json, os, socket, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import uvicorn
from autoskill import builder, db, host_bindings, projects, runner, workflow
from autoskill.server import jobs
from autoskill.server.app import create_app
class Business(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        body = b'{"priority":"medium"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
business = ThreadingHTTPServer(("127.0.0.1", 0), Business)
threading.Thread(target=business.serve_forever, daemon=True).start()
project = projects.create("DSH SDK fixture", f"http://127.0.0.1:{business.server_port}", project_id="dsh-sdk")["id"]
case = builder.api_case("TC-SDK-001", "Priority must be high", "GET", "/priority", {"status":200,"json_equals":{"/priority":"high"}})
projects.save_cases(project, [case])
run = runner.execute(runner.create(project, [case["id"]])["id"])
assert run["status"] == "Fail", run
assert run["test_report"]["status"] == "ready", run["test_report"]
flows = {}
for label in ("A", "B"):
    flows[label] = workflow.create(project, {"title":"Session " + label, "sources":[{"id":"source-" + label,"text":"Priority must be high for " + label}]}, idempotency_key="fixture-" + label)
with patch.object(jobs, "ensure_worker", return_value={"status":"fixture-quiescent"}):
    lease = workflow.next_task(flows["A"]["workflow_id"], idempotency_key="fixture-expired-lease")["task"]
db.execute("UPDATE workflow_attempts SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", (lease["attempt_id"],))
bindings = {}
for label in ("A", "B"):
    bindings[label] = host_bindings.upsert({"host":"dsh","host_session_id":"dsh-sdk-" + label,"task_session_id":"task-" + label,"workflow_id":flows[label]["workflow_id"],"label":"Session " + label,"idempotency_key":"fixture-bind-" + label})["binding"]
bindings["report"] = host_bindings.upsert({"host":"dsh","host_session_id":"dsh-sdk-A","task_session_id":"task-report","run_id":run["id"],"label":"Existing failed report","idempotency_key":"fixture-bind-report"})["binding"]
host_bindings.select({"host":"dsh","host_session_id":"dsh-sdk-B","binding_id":bindings["B"]["id"],"expected_revision":0,"idempotency_key":"fixture-select-B"})
listener = socket.socket()
listener.bind(("127.0.0.1",0))
listener.listen(128)
port = listener.getsockname()[1]
app = create_app(port=port, start_worker=False)
print(json.dumps({"origin":f"http://127.0.0.1:{port}","project":project,"run":run,"flows":flows,"bindings":bindings,"lease":lease}), flush=True)
try:
    uvicorn.Server(uvicorn.Config(app, log_level="error")).run(sockets=[listener])
finally:
    for dispatcher in list(jobs._dispatchers.values()):
        try:
            dispatcher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            dispatcher.terminate()
            dispatcher.wait(timeout=2)
    executors = {row["pid"] for row in db.rows("SELECT DISTINCT pid FROM runs WHERE pid IS NOT NULL")} - {os.getpid()}
    deadline = time.monotonic() + 5
    while any(runner.process_alive(pid) for pid in executors) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(runner.process_alive(pid) for pid in executors), "A fixture executor still owns its reports"
    business.shutdown()
    business.server_close()
    listener.close()
`;

export async function kernelFixture(t) {
  const workspace = await mkdtemp(join(tmpdir(), 'autoskill-dsh-sdk-'));
  const localPython = join(ROOT, '.venv', 'bin', 'python');
  const python = process.env.AUTOSKILL_VALIDATION_PYTHON ?? process.env.AUTOSKILL_DEV_PYTHON ?? (existsSync(localPython) ? localPython : 'python3');
  const env = { ...process.env, PYTHONPATH: join(ROOT, 'skills', 'autoskill', 'app'),
    PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', AUTOSKILL_WORKSPACE: workspace,
    AUTOSKILL_CASE_SKILL: join(workspace, 'plans'), AUTOSKILL_BRIDGE: 'off', AUTOSKILL_HEADED: '0',
    AUTOSKILL_QUEUE_IDLE_SECONDS: '0.1',
    AUTOSKILL_SESSION: '', AUTOSKILL_WORKFLOW_BINDING: '' };
  const child = spawn(python, ['-u', '-c', KERNEL_FIXTURE], { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
  const exited = new Promise((done) => child.once('exit', done));
  t.after(async () => {
    if (child.exitCode === null) {
      child.kill('SIGTERM');
      const timer = setTimeout(() => child.kill('SIGKILL'), 15000);
      timer.unref();
      await exited;
      clearTimeout(timer);
    }
    await rm(workspace, { recursive: true, force: true });
  });
  const data = await new Promise((done, fail) => {
    let stdout = '';
    const timer = setTimeout(() => fail(new Error('Python fixture did not start: ' + stderr)), 30000);
    const onData = (chunk) => {
      stdout += chunk.toString();
      const end = stdout.indexOf('\n');
      if (end < 0) return;
      clearTimeout(timer);
      child.stdout.off('data', onData);
      try { done(JSON.parse(stdout.slice(0, end))); } catch (error) { fail(error); }
    };
    child.stdout.on('data', onData);
    child.once('error', (error) => { clearTimeout(timer); fail(error); });
    child.once('exit', (code) => { clearTimeout(timer); fail(new Error(`Python fixture exited ${code}: ${stderr}`)); });
  });
  let ready = false;
  for (let attempt = 0; attempt < 100 && !ready; attempt++) {
    try { ready = (await fetch(data.origin + '/api/status', { signal: AbortSignal.timeout(250) })).ok; }
    catch { /* The fixture prints its port immediately before uvicorn starts listening. */ }
    if (!ready) await delay(50);
  }
  assert.ok(ready, 'real Python API became available: ' + stderr);
  const spec = { cwd: join(ROOT, 'skills', 'autoskill'), env: { AUTOSKILL_WORKSPACE: workspace },
    origin: data.origin, port: Number(new URL(data.origin).port), python };
  async function snapshot() {
    const code = `
import hashlib, json, pathlib, sqlite3, sys
root = pathlib.Path(sys.argv[1])
connection = sqlite3.connect((root / ".local/autoskill.db").as_uri() + "?mode=ro", uri=True)
connection.execute("PRAGMA query_only=ON")
tables = {}
for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    quoted = '"' + name.replace('"', '""') + '"'
    tables[name] = sorted([list(row) for row in connection.execute("SELECT * FROM " + quoted)], key=lambda row: json.dumps(row, sort_keys=True))
connection.close()
files = {str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file() and not path.name.startswith("autoskill.db")}
print(json.dumps({"tables":tables,"files":files},sort_keys=True))
`;
    const result = await executeFile(python, ['-c', code, workspace], { cwd: ROOT, env, maxBuffer: 10 * 1024 * 1024 });
    return JSON.parse(result.stdout);
  }
  return { ...data, workspace, spec, snapshot };
}

export function resultText(result) {
  return result.content.filter((block) => block.type === 'text').map((block) => block.text).join('\n');
}

async function toolFixture(t, options = {}) {
  const kernel = await kernelFixture(t);
  const host = await sdkHost(t, kernel.workspace);
  const { HostService } = await import('../plugin/host-service.mjs');
  const { attachHostBackend } = await import('../plugin/host-backend.mjs');
  const requests = [];
  const service = new HostService(kernel.spec, { fs: host.ctx.fs, workingDirectory: host.ctx.workingDirectory,
    fetch: async (url, init) => {
      requests.push({ path: new URL(url).pathname, method: init.method, body: init.body ? JSON.parse(init.body) : null });
      const response = await fetch(url, init);
      await options.afterResponse?.(url, init, response);
      return response;
    } });
  await host.ctx.plugin((backend) => { attachHostBackend(backend, kernel.spec, { service }); });
  assert.ok(host.ctx.tools.get('autoskill_task'), 'the production backend registered its tools through Cordis injection');
  return { kernel, ...host, service, requests, owner: await host.agent('dsh-sdk-A') };
}

async function finishedRun(service, owner, runId) {
  let finished;
  for (let attempt = 0; attempt < 100; attempt++) {
    finished = (await service.read(owner, { resource: 'run', query: { run_id: runId } })).data;
    if (['Pass', 'Fail', 'Blocked', 'Cancelled'].includes(finished.status) && finished.test_report?.status === 'ready') return finished;
    await delay(100);
  }
  assert.fail('the real queued run did not finish with a ready report: ' + JSON.stringify({ id: runId, status: finished?.status }));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  test('real DSH native and Node PTC run_code share one kernel request and preserve business failure', { skip: sdkSkip }, async (t) => {
    const { kernel, ctx, owner, call, ptc, requests, agent } = await toolFixture(t);
    for (const name of ['autoskill_read', 'autoskill_task']) {
      const definition = ctx.tools.get(name, owner);
      assert.equal(typeof definition.output.render, 'function');
      assert.equal(definition.output.schema.type, 'object');
    }
    const before = await kernel.snapshot();
    const args = { operation: 'binding.put', request_id: 'native-ptc-same-request',
      payload: { project_id: kernel.project, task_session_id: '同一内核任务', label: 'Same native and PTC request' } };
    const native = await call(owner, 'autoskill_task', args);
    assert.equal(native.isError, false, resultText(native));
    assert.equal(native.value.request_state, 'completed');
    assert.equal(native.value.identity.host_session_id, owner.session.id);
    assert.equal(native.value.identity.task_session_id, '同一内核任务');
    assert.equal(native.meta.autoskill.request_id, args.request_id);
    const afterNative = await kernel.snapshot();
    assert.equal(afterNative.tables.host_task_bindings.length, before.tables.host_task_bindings.length + 1);
    assert.equal(afterNative.tables.host_task_requests.length, before.tables.host_task_requests.length + 1);
    const conflict = await call(owner, 'autoskill_task', { ...args, payload: { ...args.payload, label: 'A different intent' } });
    assert.equal(conflict.isError, true, 'the request ID must remain bound to its original payload');
    assert.deepEqual(await kernel.snapshot(), afterNative);
    await ptc(owner);
    const direct = await call(owner, 'autoskill_task', args);
    assert.equal(direct.isError, true, 'the real PTC executor must reject a native name at the root');
    const nested = await call(owner, 'run_code', {
      code: `return await tools.autoskill_task(${JSON.stringify(args)});`, description: 'Replay one existing autoSkill request through PTC',
    });
    assert.equal(nested.isError, false, resultText(nested));
    assert.deepEqual(nested.value.result, native.value);
    assert.deepEqual(await kernel.snapshot(), afterNative, 'PTC replay must not repeat a kernel mutation or write another receipt');
    assert.equal(requests.filter((request) => request.method === 'POST' && request.path === '/api/host/bindings').length, 1);
    const dispatches = owner.session.snapshotEvents().filter((event) => event.type === 'tool/ptc-dispatch' && event.data.name === 'autoskill_task');
    assert.equal(dispatches.length, 1, 'the real run_code transport records its nested dispatch');
    assert.equal(dispatches[0].data.arguments.request_id, args.request_id);
    assert.equal(dispatches[0].data.isError, false);
    const nativeReader = await ctx.agents.create({ sessionId: 'dsh-sdk-reader', meta: { cwd: kernel.workspace } });
    const read = await call(nativeReader.agent, 'autoskill_read', { resource: 'run', query: { run_id: kernel.run.id } });
    assert.equal(read.isError, false, resultText(read));
    assert.equal(read.value.data.status, 'Fail', 'business failure is data, not a transport error or a success claim');
    assert.equal(read.value.data.failed, 1);
    const report = await call(owner, 'run_code', {
      code: `return await tools.autoskill_read(${JSON.stringify({ resource: 'report', query: { report_id: kernel.run.test_report.report_id } })});`,
      description: 'Verify the existing failed task report through PTC',
    });
    assert.equal(report.isError, false, resultText(report));
    assert.equal(report.value.result.data.host_files.status, 'verified');
    assert.equal(report.value.result.data.host_files.presented, false);
    assert.equal(report.value.result.data.host_files.present_input.files.length, 3);
    assert.deepEqual(new Set(report.value.result.data.host_files.present_input.files.map((file) => file.path)),
      new Set([kernel.run.test_report.excel, kernel.run.test_report.markdown, kernel.run.test_report.bugs]));
    assert.equal(owner.session.snapshotEvents().some((event) => event.type === 'deliverables/presented' || event.type === 'turn/start'), false);
    assert.deepEqual(await kernel.snapshot(), afterNative, 'reading an existing report cannot generate another task or report');
    const ownerB = await agent('dsh-sdk-乙');
    const independent = await call(ownerB, 'autoskill_task', args);
    assert.equal(independent.isError, false, resultText(independent));
    assert.equal(independent.value.identity.host_session_id, ownerB.session.id);
    assert.notEqual(independent.value.data.binding.id, native.value.data.binding.id, 'the same request ID in another host Session is independent');
  });

  test('real DSH run.start enqueues and executes one frozen run across native and PTC retries', { skip: sdkSkip }, async (t) => {
    const { kernel, owner, call, ptc, requests, service } = await toolFixture(t);
    const before = await kernel.snapshot();
    const args = { operation: 'run.start', request_id: 'one-real-run', binding_id: kernel.bindings.report.id,
      payload: { request: { family: 'replay', case_ids: ['TC-SDK-001'], headed: false } } };
    const native = await call(owner, 'autoskill_task', args);
    assert.equal(native.isError, false, resultText(native));
    assert.equal(native.value.request_state, 'completed', resultText(native));
    assert.equal(native.value.data.state, 'accepted');
    const runId = native.value.data.run_id;
    assert.notEqual(runId, kernel.run.id);
    await ptc(owner);
    const nested = await call(owner, 'run_code', {
      code: `return await tools.autoskill_task(${JSON.stringify(args)});`, description: 'Recover the same queued autoSkill execution request',
    });
    assert.equal(nested.isError, false, resultText(nested));
    assert.deepEqual(nested.value.result.identity, native.value.identity);
    assert.equal(nested.value.result.request_id, native.value.request_id);
    for (const field of ['request_id', 'scope', 'idempotency_key', 'payload_hash', 'run_id', 'created_at']) {
      assert.equal(nested.value.result.data[field], native.value.data[field], 'the immutable execution receipt must match: ' + field);
    }
    const finished = await finishedRun(service, owner, runId);
    assert.equal(finished.status, 'Fail', 'the unchanged business oracle still fails in the newly executed task');
    assert.equal(finished.test_report.status, 'ready');
    assert.notEqual(finished.test_report.excel, kernel.run.test_report.excel);
    const after = await kernel.snapshot();
    assert.equal(after.tables.runs.length, before.tables.runs.length + 1);
    assert.equal(after.tables.execution_requests.length, before.tables.execution_requests.length + 1);
    assert.equal(after.tables.results.length, before.tables.results.length + 1);
    assert.equal(requests.filter((request) => request.method === 'POST' && request.path === '/api/execution-requests').length, 1);
  });

  test('real DSH CommandsService exposes only parameterless skill activation and queues full instructions without waking a model', { skip: sdkSkip }, async (t) => {
    const { kernel, ctx, owner, service } = await toolFixture(t);
    const before = await kernel.snapshot();
    const signal = new AbortController().signal;
    const { bundledSkills } = await import('../plugin/index.mjs');
    const skill = (await bundledSkills())[0];
    ctx.skills.register(skill);
    const commands = await ctx.commands.list(owner);
    const entries = commands.filter((entry) => entry.name.startsWith('autoskill'));
    assert.deepEqual(entries.map((entry) => entry.name), ['autoskill-use']);
    assert.equal(entries[0].input, undefined);
    const activated = await ctx.commands.execute(owner, '/autoskill-use', [], signal);
    assert.equal(activated.result.kind, 'success', activated.result.text);
    assert.equal(owner.inbox.nextStep.length, 1);
    assert.equal(owner.inbox.nextStep[0].source.kind, 'skill-invocation');
    assert.equal(owner.inbox.nextStep[0].source.name, 'autoskill');
    assert.equal(owner.inbox.nextStep[0].content[0].text, skill.content);
    assert.equal(owner.status, 'idle');
    assert.deepEqual(await kernel.snapshot(), before, 'skill activation must not create kernel work or reports');
    assert.equal((await service.context(owner)).status, 'selection_required');
    const invalid = await ctx.commands.execute(owner, '/autoskill-use status', [], signal);
    assert.equal(invalid.result.kind, 'error');
    assert.equal(owner.inbox.nextStep.length, 1, 'rejected arguments never enqueue another invocation');
    const events = owner.session.snapshotEvents();
    assert.equal(events.filter((event) => event.type === 'command/run').length, 2);
    assert.equal(events.filter((event) => event.type === 'command/done').length, 2);
    assert.equal(events.some((event) => ['turn/start', 'model/request', 'deliverables/presented', 'user/message'].includes(event.type)), false);
  });

  test('real DSH Connection Fetch registry routes an owner peer to this kernel and exact report', { skip: sdkSkip }, async (t) => {
    const { kernel, ctx, owner, requests } = await toolFixture(t);
    const credentials = await sdk('dsh-credentials-local');
    await ctx.plugin(credentials.default, { path: join(kernel.workspace, '.sdk-credentials.yaml'), dshHome: kernel.workspace, watch: false });
    await ctx.plugin(await sdk('dsh-client-connection'));
    const peer = ctx.connection.createPeer({ principal: { kind: 'owner' } });
    const handler = ctx.connection.createSharedFetchHandler('/api');
    const request = (path, init) => handler.fetch(new Request('http://127.0.0.1:9876' + path, init), peer);
    const before = await kernel.snapshot();
    const hostConfig = await request('/api/autoskill.host');
    assert.equal(hostConfig.status, 200);
    assert.equal((await hostConfig.json()).dashboard_origin, kernel.origin, 'the active custom dashboard port comes from this backend');
    const context = await request('/api/autoskill.context?session_id=' + owner.session.id);
    assert.equal(context.status, 200);
    assert.equal(context.headers.get('cache-control'), 'no-store');
    assert.equal((await context.json()).status, 'selection_required');
    const other = await request('/api/autoskill.context?session_id=dsh-sdk-B&binding_id=' + kernel.bindings.A.id);
    assert.equal(other.status, 400, 'the proxy preserves the kernel BindingError HTTP contract');
    assert.equal((await other.json()).code, 'not_found');
    const read = await request('/api/autoskill.read?session_id=' + owner.session.id + '&resource=run&run_id=' + kernel.run.id);
    assert.equal(read.status, 200);
    assert.equal((await read.json()).data.status, 'Fail');
    const download = await request('/api/autoskill.report?session_id=' + owner.session.id + '&format=excel&report_id=' + kernel.run.test_report.report_id);
    assert.equal(download.status, 200);
    assert.equal(download.headers.get('cache-control'), 'no-store');
    assert.equal(Buffer.compare(Buffer.from(await download.arrayBuffer()), await readFile(kernel.run.test_report.excel)), 0);
    const callsBeforeInvalid = requests.length;
    assert.equal((await request('/api/autoskill.context')).status, 400);
    assert.equal((await request('/api/autoskill.task')).status, 404, 'GET must not dispatch the task route');
    assert.equal((await request('/api/autoskill.report?session_id=' + owner.session.id + '&format=../../arbitrary&report_id=' + kernel.run.test_report.report_id)).status, 400);
    assert.equal(requests.length, callsBeforeInvalid);
    assert.equal(owner.session.snapshotEvents().some((event) => event.type === 'turn/start' || event.type === 'deliverables/presented'), false);
    assert.deepEqual(await kernel.snapshot(), before);
    const task = { session_id: owner.session.id, operation: 'binding.select', request_id: 'connection-select',
      payload: { binding_id: kernel.bindings.A.id, expected_revision: 0 } };
    const submit = () => request('/api/autoskill.task', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(task) });
    const selected = await submit();
    assert.equal(selected.status, 200);
    assert.equal((await selected.json()).data.selection.binding_id, kernel.bindings.A.id);
    const afterSelection = await kernel.snapshot();
    const repeated = await submit();
    assert.equal(repeated.status, 200);
    await repeated.json();
    assert.equal(requests.filter((entry) => entry.method === 'POST' && entry.path === '/api/host/bindings/select').length, 1);
    assert.deepEqual(await kernel.snapshot(), afterSelection);
  });

  test('real DSH rejects invalid arguments and pre-dispatch abort without touching the kernel', { skip: sdkSkip }, async (t) => {
    const { kernel, owner, call, ptc, requests } = await toolFixture(t);
    const before = await kernel.snapshot();
    for (const args of [null, [], {}, { operation: 'not-an-operation', request_id: 'invalid' },
      { operation: 'binding.put' }, { operation: 'binding.put', request_id: 'invalid', payload: [] },
      { operation: 'binding.put', request_id: 'invalid', extra: true }]) {
      const result = await call(owner, 'autoskill_task', args);
      assert.equal(result.isError, true, JSON.stringify(args));
    }
    const args = { operation: 'binding.put', request_id: 'aborted-before-dispatch', payload: { task_session_id: 'never-created' } };
    assert.equal((await call(null, 'autoskill_task', args)).isError, true, 'Agent ownership comes from exec.agent');
    const controller = new AbortController();
    controller.abort(new Error('caller stopped before dispatch'));
    const aborted = await call(owner, 'autoskill_task', args, { signal: controller.signal });
    assert.equal(aborted.isError, true);
    await ptc(owner);
    const invalidPtc = await call(owner, 'run_code', {
      code: 'return await tools.autoskill_task({operation:"binding.put"});', description: 'Reject an incomplete autoSkill task request',
    });
    assert.equal(invalidPtc.isError, true);
    const abortedPtc = await call(owner, 'run_code', {
      code: `return await tools.autoskill_task(${JSON.stringify(args)});`, description: 'Abort before a nested autoSkill request',
    }, { signal: controller.signal });
    assert.equal(abortedPtc.isError, true);
    assert.equal(requests.length, 0, 'invalid or pre-aborted work must never reach the HTTP transport');
    assert.deepEqual(await kernel.snapshot(), before);
  });

  test('real DSH abort after kernel acceptance retains an honest recovery receipt and does not repeat work', { skip: sdkSkip }, async (t) => {
    const controller = new AbortController();
    let committedResponse;
    const { kernel, owner, call, requests, service } = await toolFixture(t, {
      afterResponse: async (url, init, response) => {
        if (init.method === 'POST' && new URL(url).pathname === '/api/execution-requests') {
          assert.equal(response.ok, true);
          committedResponse = await response.clone().json();
          controller.abort(new Error('caller stopped after the kernel accepted the request'));
        }
      },
    });
    const before = await kernel.snapshot();
    const args = { operation: 'run.start', request_id: 'accepted-then-aborted', binding_id: kernel.bindings.report.id,
      payload: { request: { family: 'replay', case_ids: ['TC-SDK-001'], headed: false } } };
    const result = await call(owner, 'autoskill_task', args, { signal: controller.signal });
    assert.equal(committedResponse.state, 'accepted', 'the real kernel committed before cancellation');
    assert.ok(committedResponse.run_id);
    assert.equal(result.isError, true, 'the SDK still reports caller cancellation');
    assert.match(resultText(result), /不代表业务已取消|业务状态未被改变为取消/);
    assert.match(resultText(result), /accepted-then-aborted/);
    const finished = await finishedRun(service, owner, committedResponse.run_id);
    assert.equal(finished.status, 'Fail', 'cancelling the host wait must not cancel the accepted business run');
    const accepted = await kernel.snapshot();
    assert.equal(accepted.tables.runs.length, before.tables.runs.length + 1);
    assert.equal(accepted.tables.execution_requests.length, before.tables.execution_requests.length + 1);
    const lookup = await call(owner, 'autoskill_read', { resource: 'operation', query: { request_id: args.request_id } });
    assert.equal(lookup.isError, false, resultText(lookup));
    assert.equal(lookup.value.data.request_state, 'completed', 'a read must recover the actual accepted execution receipt');
    assert.equal(lookup.value.data.data.run_id, committedResponse.run_id);
    const replay = await call(owner, 'autoskill_task', args);
    assert.equal(replay.isError, false, resultText(replay));
    assert.deepEqual(replay.value, lookup.value.data);
    assert.equal(requests.filter((request) => request.method === 'POST').length, 1);
    assert.deepEqual((await kernel.snapshot()).tables, accepted.tables, 'an uncertain accepted request must never be re-POSTed automatically');
  });

  test('real DSH present commits only accessible reports in an owning Agent open turn', { skip: sdkSkip }, async (t) => {
    const fixture = await kernelFixture(t);
    const { ctx, agent, call } = await sdkHost(t, fixture.workspace);
    const owner = await agent('dsh-sdk-present');
    const report = fixture.run.test_report;
    assert.ok(report.bugs && report.bugs_download_url && report.bugs_sha256 && report.bugs_size);
    const files = [{ path: report.excel, description: '真实失败任务 Excel' },
      { path: report.markdown, description: '同一任务 Markdown' }, { path: report.bugs, description: '同一任务 Bug 清单' }];
    const events = () => owner.session.snapshotEvents().filter((event) => event.type === 'deliverables/presented');
    const before = await fixture.snapshot();
    const { reportFiles } = await import('../plugin/host-files.mjs');
    const legacy = { ...report, bugs: undefined, bugs_download_url: undefined, bugs_sha256: undefined, bugs_size: undefined };
    const missingBugs = { ...report, bugs: join(fixture.workspace, 'missing-bugs.md') };
    for (const receipt of [legacy, missingBugs]) {
      const unavailable = await reportFiles(receipt, { fs: ctx.fs, cwd: fixture.workspace, signal: new AbortController().signal });
      assert.equal(unavailable.status, 'unavailable');
      assert.equal(unavailable.presented, false);
      assert.equal(unavailable.present_input, undefined, 'an incomplete old receipt cannot invent a third deliverable');
    }
    assert.equal((await call(null, 'present', { files })).isError, true);
    assert.equal(events().length, 0);
    const closed = await call(owner, 'present', { files });
    assert.equal(closed.isError, true);
    assert.match(resultText(closed), /open turn/);
    assert.equal(events().length, 0);
    owner.session.append('turn/start', { turn: 1 });
    const incomplete = files.map((file, index) => index === 2 ? { ...file, path: missingBugs.bugs } : file);
    assert.equal((await call(owner, 'present', { files: incomplete })).isError, true);
    assert.equal(events().length, 0, 'two accessible files plus a missing Bug list must not publish a partial delivery');
    for (const path of [join(fixture.workspace, 'missing.xlsx'), fixture.workspace]) {
      assert.equal((await call(owner, 'present', { files: [{ path }] })).isError, true);
      assert.equal(events().length, 0);
    }
    const removeBlock = ctx.on('tools/post-execute', async (_exec, _result, next) => {
      await next();
      return { kind: 'block', feedback: [{ type: 'text', text: 'Acceptance policy rejects this result.' }] };
    });
    const blocked = await call(owner, 'present', { files });
    assert.equal(blocked.isError, true);
    assert.equal(events().length, 0);
    removeBlock();
    const accepted = await call(owner, 'present', { files });
    assert.equal(accepted.isError, false, resultText(accepted));
    assert.deepEqual(accepted.value.files, files);
    assert.equal(events().length, 1);
    assert.deepEqual(events()[0].data.files, files);
    owner.session.append('turn/end', { turn: 1, reason: { kind: 'completed' } });
    assert.equal((await call(owner, 'present', { files })).isError, true);
    assert.equal(events().length, 1);
    assert.deepEqual(await fixture.snapshot(), before, 'present declares files; it must not regenerate reports or alter kernel state');
  });
}
