// Share one transport adapter between native tools, human commands and authenticated UI routes.
import { join } from 'node:path';
import { workspaceFor } from './data-root.mjs';
import { HostJournal, hash } from './host-journal.mjs';
import { prepareSources, reportFiles, REPORT_FORMATS } from './host-files.mjs';
import { identityOf, identifier, object, problem, queryPath, readRoute, receiptQuery, resultIdentity, taskRoute, TASK_OPERATIONS } from './host-operations.mjs';

export function hostSessionId(agent) {
  return identifier(typeof agent === 'string' ? agent : agent?.session?.id, '当前 DSH 会话身份');
}

function envelope(operation, requestId, identity, data, requestState = 'completed') {
  return { version: 1, operation, request_id: requestId, request_state: requestState,
    identity: resultIdentity(identity, data), data };
}

function failed(operation, requestId, identity, error, state) {
  return { ...envelope(operation, requestId, identity, null, state), error: { code: error.code ?? 'transport_error', message: String(error.message ?? error) },
    recovery: { resource: 'operation', query: { request_id: requestId }, note: '中断等待不代表业务已取消；先查询原操作，不要换键重复执行。' } };
}

function identityHeader(name, value = '') {
  const encoded = { [name + '-B64']: Buffer.from(value, 'utf8').toString('base64url') };
  return /^[\x20-\x7e]*$/.test(value) ? { ...encoded, [name]: value } : encoded;
}

function replayPage(value, query) {
  const rows = value.results ?? [];
  const revision = hash(rows);
  let offset = 0;
  if (query.cursor) {
    let cursor;
    try { cursor = JSON.parse(Buffer.from(query.cursor, 'base64url').toString()); } catch { throw problem('cursor_expired', '运行结果游标无效'); }
    if (cursor.revision !== revision || !Number.isInteger(cursor.offset) || cursor.offset < 0) throw problem('cursor_expired', '运行结果已变化，请从第一页读取');
    offset = cursor.offset;
  }
  const limit = query.limit ?? 50;
  if (!Number.isInteger(limit) || limit < 1) throw problem('validation_error', 'limit 需要是正整数');
  const selected = query.all ? rows : rows.slice(offset, offset + limit);
  const end = query.all ? rows.length : offset + selected.length;
  const next = end < rows.length ? Buffer.from(JSON.stringify({ revision, offset: end })).toString('base64url') : null;
  return { ...value, results: query.all ? selected : selected.map(({ detail, ...row }) => ({ ...row, detail_loaded: false, detail_ref: { run_id: value.id, result_id: row.id },
    business_status: detail?.business_status, assertion_count: detail?.assertions?.length ?? 0 })),
  page: { total: rows.length, returned: selected.length, has_more: next !== null, next_cursor: next, content_revision: revision } };
}

export class HostService {
  constructor(spec, options = {}) {
    this.spec = spec;
    this.workspace = workspaceFor(spec);
    this.fetch = options.fetch ?? globalThis.fetch;
    this.fs = options.fs;
    this.workingDirectory = options.workingDirectory;
    this.runtimeContext = options.runtimeContext;
    this.journal = new HostJournal(this.workspace);
  }

  async request(path, { body, signal, identity, bindingToken } = {}) {
    const response = await this.fetch(this.spec.origin + path, {
      method: body === undefined ? 'GET' : 'POST', signal,
      headers: { 'Content-Type': 'application/json', 'X-Autoskill-Host': 'dsh',
        ...identityHeader('X-Autoskill-Host-Session', identity?.host_session_id), ...identityHeader('X-Autoskill-Session', identity?.task_session_id),
        'X-Autoskill-Workflow-Binding': bindingToken ?? '' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const value = await response.json();
    if (!response.ok) throw Object.assign(problem(value.code ?? 'http_error', value.error ?? value.detail ?? `autoSkill HTTP ${response.status}`), { status: response.status, details: value.details });
    return value;
  }

  async context(agent, query = {}, signal = undefined) {
    const id = hostSessionId(agent);
    const allowed = Object.fromEntries(['binding_id', 'limit', 'cursor', 'runs_limit', 'runs_cursor'].filter((key) => query[key] !== undefined).map((key) => [key, query[key]]));
    const data = await this.request(queryPath('/api/host/context', { ...allowed, host: 'dsh', host_session_id: id }), { signal });
    return { version: 1, host_session_id: id, ...data };
  }

  async filesContext(agent, signal) {
    const active = typeof agent === 'object' ? agent : undefined;
    const fs = this.fs;
    const directory = this.workingDirectory;
    const cwd = active && directory ? await directory.ensure(active, signal) : undefined;
    return { fs: active ? fs : undefined, cwd, signal };
  }

  async read(agent, args, signal) {
    object(args);
    const query = object(args.query ?? {});
    if (args.resource === 'operation') {
      const result = await this.operation(agent, identifier(query.request_id, '原 request_id'), signal);
      return envelope('operation', null, identityOf(hostSessionId(agent), null), result);
    }
    const context = await this.context(agent, { ...query, binding_id: args.binding_id ?? query.binding_id }, signal);
    const identity = identityOf(hostSessionId(agent), context.binding, query);
    if (args.resource === 'context') return envelope('context', null, identity, context);
    const route = readRoute(args.resource, query, context.binding);
    let data;
    if (args.resource === 'run') {
      const known = context.runs?.items?.find((row) => (row.id ?? row.run_id) === identity.run_id);
      data = await this.readRun(identity, query, signal, query.family ?? known?.family);
    }
    else data = await this.request(route, { signal, identity });
    if (args.resource === 'report') data = { ...data, host_files: await reportFiles(data, await this.filesContext(agent, signal)) };
    return envelope(args.resource, null, identity, data);
  }

  async readRun(identity, query, signal, family = undefined) {
    const id = identifier(identity.run_id, 'run_id');
    if (family === 'native') return this.request(readRoute('run', query, identity), { signal, identity });
    const run = await this.request('/api/runs/' + encodeURIComponent(id), { signal, identity });
    if (run.family === 'native') return this.request(readRoute('run', query, identity), { signal, identity });
    if (query.case) {
      const found = (run.results ?? []).filter((row) => String(row.id) === String(query.case).replace(/^result:/, '') || row.case_id === query.case);
      if (found.length !== 1) throw problem('validation_error', '请用本次运行中唯一的 result:<结果 id> 读取详情');
      return { ...run, results: found };
    }
    return replayPage(run, query);
  }

  async task(agent, args, signal) {
    object(args);
    const id = hostSessionId(agent);
    const requestId = identifier(args.request_id, '稳定的 request_id；重试沿用原值');
    if (!TASK_OPERATIONS.includes(args.operation)) throw problem('validation_error', '没有这个任务操作');
    const payload = object(args.payload ?? {});
    const fingerprint = hash({ operation: args.operation, binding_id: args.binding_id ?? null, payload });
    return await this.journal.serial(id, requestId, async () => {
      const previous = await this.journal.get(id, requestId);
      if (previous) {
        if (previous.fingerprint !== fingerprint) throw problem('idempotency_conflict', '同一 request_id 已用于不同操作或参数');
        return this.operation(agent, requestId, signal);
      }
      signal?.throwIfAborted();
      return this.dispatch(agent, args, fingerprint, signal);
    });
  }

  async dispatch(agent, args, fingerprint, signal) {
    const id = hostSessionId(agent);
    const context = await this.context(agent, { binding_id: args.binding_id }, signal);
    const payload = object(args.payload ?? {});
    const supplied = args.operation === 'run.start' && payload.request ? { ...payload, ...payload.request, task_session_id: payload.request.session_id ?? payload.task_session_id } : payload;
    const identity = identityOf(id, context.binding, supplied);
    if (!args.operation.startsWith('binding.') && !['run.cancel', 'report.retry'].includes(args.operation) && identity.task_session_id === undefined) {
      throw problem('selection_required', '请先绑定并选中任务，或明确提供 task_session_id；不会按最近任务猜测');
    }
    const journal = { fingerprint, operation: args.operation, payload, identity, request_id: args.request_id,
      result: envelope(args.operation, args.request_id, identity, null, 'unknown') };
    await this.journal.save(id, args.request_id, journal);
    let dispatched = false;
    try {
      const prepared = args.operation === 'workflow.start' || args.operation === 'workflow.revise'
        ? await prepareSources(payload, { ...await this.filesContext(agent, signal), folder: join(this.journal.folder, 'inputs', hash([id, args.request_id])) }) : payload;
      const route = taskRoute(args.operation, prepared, identity, args.request_id);
      signal?.throwIfAborted();
      dispatched = true;
      const data = await this.request(route.path, { body: route.body, signal, identity, bindingToken: payload.binding_token });
      const result = envelope(args.operation, args.request_id, identity, data);
      await this.journal.save(id, args.request_id, { ...journal, result });
      return result;
    } catch (error) {
      const uncertain = dispatched && (!error.status || error.status >= 500);
      const result = failed(args.operation, args.request_id, identity, error, uncertain ? 'unknown' : 'failed');
      await this.journal.save(id, args.request_id, { ...journal, result });
      return result;
    }
  }

  async operation(agent, requestId, signal) {
    const id = hostSessionId(agent);
    const saved = await this.journal.get(id, requestId);
    if (!saved) return failed('operation', requestId, identityOf(id, null), problem('receipt_missing', '本宿主没有这个操作回执'), 'unknown');
    if (saved.result.request_state !== 'unknown' && saved.operation !== 'run.start') return saved.result;
    const data = await this.reconcile(saved, signal);
    if (!data) return saved.result;
    const result = envelope(saved.operation, requestId, saved.identity, data);
    await this.journal.save(id, requestId, { ...saved, result });
    return result;
  }

  async reconcile(saved, signal) {
    try {
      if (saved.operation === 'run.start') {
        const data = await this.request(queryPath('/api/execution-requests', { scope: JSON.stringify(['dsh', saved.identity.host_session_id]), idempotency_key: saved.request_id }), { signal });
        return data;
      }
      const query = receiptQuery(saved);
      if (!query) return null;
      const receipt = await this.request(queryPath('/api/host/requests', query), { signal });
      return receipt.state === 'completed' ? receipt.response : null;
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  }

  async download(agent, reportId, format, signal) {
    hostSessionId(agent);
    identifier(reportId, 'report_id');
    if (!REPORT_FORMATS.includes(format)) throw problem('validation_error', '报告格式需要是 excel、markdown 或 bugs');
    const response = await this.fetch(this.spec.origin + '/api/task-reports/' + encodeURIComponent(reportId) + '/' + format, { signal });
    const headers = new Headers({ 'Cache-Control': 'no-store' });
    for (const key of ['content-type', 'content-disposition', 'content-length']) {
      const value = response.headers.get(key);
      if (value) headers.set(key, value);
    }
    return new Response(response.body, { status: response.status, headers });
  }
}
