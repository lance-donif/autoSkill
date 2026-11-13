// Map host gestures to existing application services; business state remains in Python.
export const READ_RESOURCES = ['context', 'workflow', 'workflow.read', 'judge', 'judge.read', 'run', 'report', 'operation'];
export const TASK_OPERATIONS = ['binding.put', 'binding.select', 'workflow.start', 'workflow.revise', 'workflow.next',
  'workflow.submit', 'workflow.heartbeat', 'workflow.release', 'workflow.recover', 'workflow.cancel', 'workflow.resume',
  'judge.next', 'judge.submit', 'judge.heartbeat', 'judge.release', 'run.start', 'run.cancel', 'report.retry'];

export function problem(code, message) {
  return Object.assign(new Error(message), { code });
}

export function object(value, label = '参数') {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw problem('validation_error', label + '需要是对象');
  return value;
}

export function identifier(value, label) {
  if (typeof value !== 'string' || !value) throw problem('validation_error', '请明确提供' + label);
  return value;
}

export function target(query, binding, field) {
  return identifier(query[field] ?? binding?.[field], field);
}

export function queryPath(path, query = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, typeof value === 'boolean' ? (value ? '1' : '0') : String(value));
  }
  return path + (params.size ? '?' + params : '');
}

const encoded = (query, binding, field) => encodeURIComponent(target(query, binding, field));
const pick = (query, fields) => Object.fromEntries(fields.filter((field) => query[field] !== undefined).map((field) => [field, query[field]]));

export function readRoute(resource, query, binding) {
  const pages = pick(query, ['limit', 'cursor', 'all', 'case']);
  const routes = {
    workflow: () => queryPath('/api/workflows/' + encoded(query, binding, 'workflow_id'), pages),
    'workflow.read': () => queryPath('/api/workflows/' + encoded(query, binding, 'workflow_id') + '/read',
      pick(query, ['kind', 'offset', 'limit', 'head', 'text_offset', 'text_length'])),
    judge: () => '/api/judgements/' + encoded(query, binding, 'judge_id'),
    'judge.read': () => queryPath('/api/judgements/' + encoded(query, binding, 'judge_id') + '/read', pick(query, ['offset', 'length'])),
    run: () => queryPath('/api/native-runs/' + encoded(query, binding, 'run_id'), pages),
    report: () => '/api/task-reports/' + encoded(query, binding, 'report_id'),
  };
  if (!routes[resource]) throw problem('validation_error', '没有这个读取资源');
  return routes[resource]();
}

export function taskRoute(operation, payload, identity, requestId) {
  const body = { ...payload, idempotency_key: requestId };
  if (operation.startsWith('binding.')) {
    return { path: '/api/host/bindings' + (operation === 'binding.select' ? '/select' : ''),
      body: { ...body, host: 'dsh', host_session_id: identity.host_session_id } };
  }
  if (operation === 'run.start') {
    const request = Object.fromEntries(Object.entries(object(payload.request ?? payload)).filter(([key]) => !['task_session_id', 'binding_token', 'binding_id'].includes(key)));
    const host = request.family === 'native' ? { host: 'dsh', host_session_id: identity.host_session_id } : {};
    return { path: '/api/execution-requests', body: { scope: JSON.stringify(['dsh', identity.host_session_id]), idempotency_key: requestId,
      request: { ...request, project_id: request.project_id ?? identity.project_id, session_id: identity.task_session_id, ...host } } };
  }
  if (operation === 'run.cancel') return { path: '/api/runs/' + encoded(payload, identity, 'run_id') + '/cancel', body: {} };
  if (operation === 'report.retry') return { path: '/api/task-reports/' + encoded(payload, identity, 'report_id') + '/retry', body: {} };
  if (operation === 'workflow.start') return { path: '/api/workflows', body: { ...body, project_id: target(payload, identity, 'project_id') } };
  if (operation.startsWith('workflow.')) {
    return { path: '/api/workflows/' + encoded(payload, identity, 'workflow_id') + '/' + operation.split('.')[1], body };
  }
  if (operation === 'judge.next') return { path: '/api/judgements/next', body };
  if (operation.startsWith('judge.')) return { path: '/api/judgements/' + encoded(payload, identity, 'judge_id') + '/' + operation.split('.')[1], body };
  throw problem('validation_error', '没有这个任务操作');
}

export function identityOf(hostSessionId, binding, payload = {}) {
  const identity = { host: 'dsh', host_session_id: hostSessionId };
  for (const field of ['task_session_id', 'project_id', 'workflow_id', 'cycle_id', 'run_id', 'report_id']) {
    const value = payload[field] ?? binding?.[field];
    if (field === 'task_session_id' && value !== undefined && value !== null && typeof value !== 'string') throw problem('validation_error', 'task_session_id 需要是文字');
    if (value !== undefined && value !== null) identity[field] = value;
  }
  if (binding) identity.binding_id = binding.id ?? binding.binding_id;
  return identity;
}

export function resultIdentity(identity, data) {
  const result = { ...identity };
  if (data?.workflow_id) result.workflow_id = data.workflow_id;
  if (data?.run_id) result.run_id = data.run_id;
  if (data?.report_id) result.report_id = data.report_id;
  if (data?.binding) return { ...result, ...identityOf(identity.host_session_id, data.binding) };
  return result;
}

export function receiptQuery(saved) {
  const { operation, identity, payload, request_id: key } = saved;
  const base = { host: 'dsh', host_session_id: identity.host_session_id, key };
  if (operation.startsWith('binding.')) return { ...base, kind: 'binding' };
  if (operation.startsWith('workflow.')) return { ...base, kind: 'workflow',
    scope: operation === 'workflow.start' ? 'workflow-create:' + identity.project_id : identity.workflow_id };
  if (operation === 'judge.next') return { ...base, kind: 'judge', scope: 'judge-claim:' + (payload.owner || identity.task_session_id || 'local') };
  if (operation.startsWith('judge.')) return { ...base, kind: 'judge',
    scope: (operation === 'judge.submit' ? 'judge-submit:' : 'judge-control:') + target(payload, identity, 'judge_id') };
  return null;
}
