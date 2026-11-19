const sensitive = /authorization|cookie|token|secret|password|passwd|api[-_]?key|credential|session/i;
const labels = { pass: '通过', fail: '失败', not_run: '未执行' };

function redact(value, key = '') {
  if (sensitive.test(key) && value != null && value !== '') return '<已隐藏>';
  if (Array.isArray(value)) return value.map((item) => redact(item, key));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([field, item]) => [field, redact(item, field)]));
  return value;
}

function shownUrl(path, base) {
  const url = new URL(path, base);
  url.username = '';
  url.password = '';
  for (const key of new Set(url.searchParams.keys())) if (sensitive.test(key)) url.searchParams.set(key, '<已隐藏>');
  return url;
}

function actionText(step) {
  if (step.name) return step.name;
  const locator = step.locators?.[0] || step.locator || {};
  return [step.action || step.assertion, locator.name || locator.value || locator.role || step.selector,
    step.url || step.key || step.expected].filter((v) => v != null && v !== '').join(' ');
}

function uiPlan(definition, detail, project, previous) {
  const auto = definition.extensions?.automation || {};
  const actions = [...(auto.actions || []), ...(auto.assertions || [])];
  const steps = [];
  if (auto.start_url) {
    try { steps.push({ text: '打开 ' + shownUrl(auto.start_url, project.base_url).href, check: false }); }
    catch { return { outcome: 'not_run', reason: '冻结的页面地址无效' }; }
  }
  for (const [index, step] of actions.entries()) {
    const saved = detail?.steps?.[index];
    steps.push({ text: step.name || saved?.text || actionText(step), check: Boolean(step.assertion) });
  }
  if (!steps.length) {
    const source = definition.extensions?.test_case_document?.steps || definition.steps || [];
    for (const line of (Array.isArray(source) ? source : String(source).split('\n'))) {
      if (typeof line === 'string' && line.trim() && !line.includes('【待确认')) steps.push({ text: line, check: false });
    }
  }
  if (!steps.length) return { steps, outcome: 'not_run', reason: '此页面用例没有可模拟的步骤' };
  const outcome = previous?.state?.outcome === 'fail' ? 'fail' : 'pass';
  return { steps, outcome, reason: outcome === 'fail' ? '页面检查在浏览器模拟中失败' : '' };
}

function sampleResponse(fixtures, method, url) {
  if (!['tickets.example.invalid', 'stock.example.invalid'].includes(url.hostname) || url.protocol !== 'https:' || url.port) return null;
  for (const { id } of fixtures['/runs']?.items || []) {
    const rows = fixtures['/runs/' + id]?.results || [];
    const sample = rows.find((row) => {
      const detail = row.detail;
      return detail?.response && detail.request?.method === method && detail.request.url?.split('?')[0] === url.origin + url.pathname;
    });
    if (sample) return structuredClone(sample.detail.response);
  }
  if (method === 'POST' && url.hostname === 'stock.example.invalid' && url.pathname === '/api/login') {
    const body = '{"role":"viewer"}';
    return { status: 200, bytes: body.length, body, headers: { 'content-type': 'application/json' } };
  }
  return null;
}

function pointerValue(body, pointer) {
  if (pointer === '') return body;
  return pointer.slice(1).split('/').reduce((value, key) => {
    const field = key.replaceAll('~1', '/').replaceAll('~0', '~');
    return value && typeof value === 'object' && Object.hasOwn(value, field) ? value[field] : undefined;
  }, body);
}

function identical(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((k) => Object.hasOwn(b, k) && identical(a[k], b[k]));
}

function apiChecks(rules, response) {
  const checks = [];
  function add(name, expected, actual, passed, field) {
    checks.push({ name, expected: redact(expected, field), actual: redact(actual ?? null, field), passed, scope: 'business' });
  }
  if (rules.status != null) add('状态码', rules.status, response.status, rules.status === response.status);
  const headers = new Headers(response.headers);
  for (const [key, expected] of Object.entries(rules.header_contains || {})) {
    const actual = headers.get(key);
    add('响应头 ' + key, expected, actual, actual?.toLowerCase().includes(String(expected).toLowerCase()) || false, key);
  }
  const body = response.body ?? '';
  for (const expected of rules.body_contains || []) {
    const found = body.includes(expected);
    add('响应内容包含', expected, found ? '已包含' : '未包含', found);
  }
  if (rules.json_equals || rules.json_has) {
    let parsed;
    try { parsed = JSON.parse(body); } catch { parsed = undefined; }
    for (const [key, expected] of Object.entries(rules.json_equals || {})) {
      const actual = pointerValue(parsed, key);
      add('字段 ' + (key || '/'), expected, actual, identical(expected, actual), key);
    }
    for (const key of rules.json_has || []) {
      const found = pointerValue(parsed, key) !== undefined;
      add('包含字段 ' + key, true, found, found);
    }
  }
  if (rules.max_ms != null) checks.push({ name: '模拟响应耗时', expected: rules.max_ms, actual: 180, passed: 180 <= rules.max_ms, scope: 'performance' });
  return checks;
}

function apiPlan(definition, project, fixtures) {
  const method = definition.method || 'GET';
  const request = { method, url: definition.path };
  for (const field of ['headers', 'params', 'json', 'form']) if (definition[field] != null) request[field] = redact(definition[field]);
  let url;
  try { url = shownUrl(definition.path, project.base_url); }
  catch { return { request, outcome: 'not_run', reason: '冻结的接口地址无效' }; }
  const rules = definition.assertions || {};
  if (/\{\{[^}]+\}\}|\$\{ENV:[^}]+\}/.test(JSON.stringify([definition.path, definition.headers, definition.params, definition.json, definition.form, rules]))) {
    return { request, outcome: 'not_run', reason: '接口定义包含未解析变量，未生成模拟交换' };
  }
  request.url = url.href;
  const response = sampleResponse(fixtures, method, url);
  if (!response) return { request, outcome: 'not_run', reason: '该方法和地址没有可用的模拟响应样例' };
  const supported = ['status', 'header_contains', 'body_contains', 'json_equals', 'json_has', 'max_ms'];
  if (Object.keys(rules).some((key) => !supported.includes(key))) return { request, outcome: 'not_run', reason: '此断言没有对应的浏览器模拟实现' };
  const assertions = apiChecks(rules, response);
  const failed = assertions.find((check) => !check.passed);
  return { request, response, assertions, outcome: failed ? 'fail' : 'pass', reason: failed ? failed.name + '：模拟结果与期望不同' : '' };
}

export function simulateCase(definition, detail, project, previous, fixtures) {
  if (definition.kind === 'api') return apiPlan(definition, project, fixtures);
  if (definition.kind === 'ui') return uiPlan(definition, detail, project, previous);
  return { outcome: 'not_run', reason: '此类用例没有对应的浏览器模拟实现' };
}

export function pendingDetails(plan) {
  const detail = { simulated: true, target_started: false };
  if (plan?.steps) detail.steps = plan.steps.map((step) => ({ ...step, status: 'not_run' }));
  if (plan?.request) detail.defined_request = plan.request;
  return detail;
}

export function completedDetails(plan, outcome, duration, reason, cancelled) {
  if (!plan) return { simulated: true };
  if (outcome === 'not_run') return pendingDetails(plan);
  const detail = { simulated: true, target_started: true };
  if (plan.steps?.length) {
    const stop = cancelled ? 0 : plan.steps.length - 1;
    const ms = Math.floor(duration / (stop + 1));
    detail.steps = plan.steps.map((step, i) => {
      if (i > stop) return { ...step, status: 'not_run' };
      const failed = i === stop && outcome === 'fail';
      return { ...step, ms: i === stop ? duration - ms * i : ms, status: failed ? 'failed' : 'done', ...(failed ? { error: reason } : {}) };
    });
  }
  if (plan.request) {
    detail.request = plan.request;
    if (!cancelled) {
      detail.response = plan.response;
      detail.assertions = plan.assertions;
    }
    detail.attempts = [{ kind: 'case', simulated: true, outcome, duration_ms: duration, request: plan.request,
      ...(!cancelled ? { response: plan.response } : {}) }];
  }
  return detail;
}

export function actualText(plan, outcome, reason, cancelled) {
  if (cancelled || outcome === 'not_run') return reason;
  if (plan?.response) return '浏览器模拟 · HTTP ' + plan.response.status + ' · ' + (plan.assertions || []).length + ' 条检查，' + labels[outcome];
  if (plan?.steps?.length) return '浏览器模拟 · ' + plan.steps.length + ' 步，' + labels[outcome];
  return reason || '浏览器模拟已完成';
}
