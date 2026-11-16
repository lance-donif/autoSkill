// Coalesce only matching in-flight requests in this loaded page, never later attempts.
const pending = new Map();

export function startOperation(key, fn) {
  if (pending.has(key)) return pending.get(key);
  const request = Promise.resolve().then(fn).finally(() => pending.delete(key));
  pending.set(key, request);
  return request;
}

export function classifyStartError(error) {
  return [400, 404, 409, 422].includes(error?.status) && !error?.uncertain ? 'rejected' : 'unknown';
}

export async function operationRequest(path, body, validate = (value) => value && typeof value === 'object') {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch('/api' + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: controller.signal,
    });
    let data;
    try { data = await response.json(); } catch { throw new Error('服务响应不完整，暂时无法确认是否已受理'); }
    if (!response.ok) {
      const error = new Error(typeof data?.detail === 'string' ? data.detail : `请求没有成功（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    if (!validate(data)) throw new Error('服务响应缺少有效回执，暂时无法确认是否已受理');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('等待响应超时，暂时无法确认是否已受理');
    throw error;
  } finally { clearTimeout(timeout); }
}
