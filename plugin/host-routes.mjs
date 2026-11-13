// Authenticated DSH routes proxy the configured local instance, never arbitrary URLs or paths.
import { hostSessionId } from './host-service.mjs';
import { object } from './host-operations.mjs';

function queryOf(request) {
  const query = Object.fromEntries(new URL(request.url).searchParams);
  for (const key of ['limit', 'runs_limit', 'offset', 'length', 'text_offset', 'text_length']) {
    if (query[key] !== undefined) query[key] = Number(query[key]);
  }
  if (query.all !== undefined) query.all = query.all === '1' || query.all === 'true';
  if (query.revision_only !== undefined) query.revision_only = query.revision_only === '1' || query.revision_only === 'true';
  return query;
}

function json(value, status = 200) {
  return Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
}

export function registerRoutes(ctx, service) {
  const lifetime = new AbortController();
  const pending = new Set();
  const handlers = {
    '/api/autoskill.host': ['GET', () => Promise.resolve(json({ version: 1, dashboard_origin: service.spec.origin }))],
    '/api/autoskill.context': ['GET', async (request) => {
      const { session_id: sessionId, ...query } = queryOf(request);
      return json(await service.context(hostSessionId(sessionId), query, request.signal));
    }],
    '/api/autoskill.read': ['GET', async (request) => {
      const { session_id: sessionId, resource, binding_id: bindingId, ...query } = queryOf(request);
      return json(await service.read(hostSessionId(sessionId), { resource, binding_id: bindingId, query }, request.signal));
    }],
    '/api/autoskill.task': ['POST', async (request) => {
      const { session_id: sessionId, ...args } = object(await request.json());
      return json(await service.task(hostSessionId(sessionId), args, request.signal));
    }],
    '/api/autoskill.report': ['GET', async (request) => {
      const { session_id: sessionId, report_id: reportId, format } = queryOf(request);
      return await service.download(hostSessionId(sessionId), reportId, format, request.signal);
    }],
  };
  for (const [path, [method, handler]] of Object.entries(handlers)) {
    ctx.effect(() => ctx.connection.fetch.register({ path, methods: [method], requestBody: 'buffered', fetch(request) {
      const scoped = new Request(request, { signal: AbortSignal.any([request.signal, lifetime.signal]) });
      const task = handler(scoped).catch((error) => json({ error: String(error.message ?? error), code: error.code ?? 'host_error' }, error.status ?? 400));
      pending.add(task);
      void task.then(() => pending.delete(task), () => pending.delete(task));
      return task;
    } }), 'autoskill: ' + path);
  }
  ctx.effect(() => async () => {
    lifetime.abort();
    await Promise.allSettled(pending);
  }, 'autoskill: host requests');
}
