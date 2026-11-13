// DeepSeek Harness client entry, loaded directly without a build step.
window.__ModuleLoader__.load({ id: 'dsh-plugin-autoskill', factory: (require) => {
  const module = { exports: {} };
  const exports = module.exports;
  const React = require('react');
  const h = React.createElement;
  let activationCount = 0;
  let requestCount = 0;
  const ACTIVE = new Set(['Queued', 'Running', 'Cancelling', 'ready', 'running', 'active', 'leased', 'waiting_execution', 'recovering']);
  const LABELS = {
    Queued: '排队中', Running: '执行中', Cancelling: '正在取消', Pass: '通过', Fail: '失败', Blocked: '受阻', Cancelled: '已取消',
    pending: '待处理', ready: '就绪', running: '进行中', active: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消',
    satisfied: '符合预期', violated: '不符合预期', incomplete: '验证未完成', unknown: '尚未确认', leased: '处理中',
    waiting_execution: '等待执行', recovering: '恢复中', paused: '已暂停', waiting: '等待中', skipped: '已跳过',
    sources: '读取需求', requirements: '整理需求', design: '设计用例', coverage: '检查覆盖', completion: '补全遗漏',
    materialize: '生成可执行用例', execute: '执行测试', diagnose: '分析结果', maintain: '维护用例', knowledge: '沉淀知识', investigate: '核实操作结果',
    publishing: '发布中', done: '已完成', not_generated: '尚未生成', stale_cycle: '当前查看历史周期', reference_missing: '任务引用已不存在',
    mixed: '部分不符合预期',
  };
  const STYLE = {
    card: { border: '1px solid currentColor', borderRadius: 12, padding: 12, margin: '8px 0', fontSize: 13, overflowWrap: 'anywhere' },
    row: { display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' },
    button: { font: 'inherit', color: 'inherit', cursor: 'pointer', border: '1px solid currentColor', borderRadius: 7, padding: '4px 9px', background: 'transparent' },
    muted: { opacity: 0.75 },
    pre: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 320, overflow: 'auto', fontSize: 12 },
  };
  const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const label = (value) => LABELS[value] || value || '未记录';
  const list = (value) => Array.isArray(value) ? value : [];
  const message = (error) => error?.message || String(error);
  const button = (text, onClick, disabled = false) => h('button', { type: 'button', style: STYLE.button, onClick, disabled }, text);

  function routeUrl(name, query = {}) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    return 'api/autoskill.' + name + (params.size ? '?' + params.toString() : '');
  }

  function requestId() {
    requestCount += 1;
    return 'ui-' + Date.now().toString(36) + '-' + requestCount.toString(36) + '-' + Math.random().toString(36).slice(2);
  }

  function actionKey(operation, payload) {
    return operation + ':' + (payload.run_id || payload.report_id || payload.binding_id || 'session');
  }

  async function responseJson(response) {
    try { return await response.json(); }
    catch {
      throw new Error(response.ok ? 'autoSkill 返回了无法识别的响应' : 'autoSkill 服务暂不可用（HTTP ' + response.status + '），请检查插件是否已启用。');
    }
  }

  function dashboardOrigin(value, ok) {
    if (!ok) throw new Error(value?.error?.message || value?.error || '无法读取 autoSkill 服务地址');
    const origin = new URL(value.dashboard_origin);
    if (!['http:', 'https:'].includes(origin.protocol) || origin.origin !== value.dashboard_origin) throw new Error('autoSkill 服务地址无效');
    return origin.origin;
  }

  function navigation(ctx) {
    const routes = new Map([['autoskill-dashboard', 'dashboard'], ['autoskill-schedules', 'schedules']]);
    const listeners = new Set();
    let host = { origin: null, loading: false, error: null };
    let request = null;
    let disposed = false;
    const emit = () => { for (const fn of listeners) fn(); };
    async function loadHost(current) {
      try {
        const response = await fetch(routeUrl('host'), { credentials: 'same-origin', cache: 'no-store', signal: current.controller.signal });
        const value = await responseJson(response);
        const origin = dashboardOrigin(value, response.ok);
        if (!disposed && !current.controller.signal.aborted) host = { origin, loading: false, error: null };
      } catch (error) {
        if (!disposed && !current.controller.signal.aborted) host = { origin: null, loading: false, error: message(error) };
      } finally {
        if (request === current) { request = null; emit(); }
      }
    }
    function refreshHost() {
      if (disposed) return Promise.resolve();
      if (request) return request.promise;
      const current = { controller: new AbortController(), promise: null };
      request = current;
      host = { origin: null, loading: true, error: null };
      emit();
      current.promise = loadHost(current);
      return current.promise;
    }
    return {
      subscribe: (fn) => {
        listeners.add(fn);
        if (!host.origin && !host.error) void refreshHost();
        return () => listeners.delete(fn);
      },
      route: (key) => routes.get(key),
      getHostSnapshot: () => host,
      refreshHost,
      open: (page) => {
        routes.set('autoskill-dashboard', page);
        emit();
        ctx.layout.selectPanel('autoskill-dashboard');
        if (!host.origin) void refreshHost();
      },
      reset: () => {
        request?.controller.abort();
        request = null;
        host = { origin: null, loading: false, error: null };
        emit();
        if (listeners.size > 0) void refreshHost();
      },
      dispose: () => { disposed = true; request?.controller.abort(); request = null; listeners.clear(); },
    };
  }

  function sessionStore(sessionId) {
    let snapshot = { actions: {}, reports: {} };
    const listeners = new Set();
    const requests = new Set();
    let generation = 0;
    let disposed = false;
    const publish = (patch) => {
      if (disposed) return;
      snapshot = { ...snapshot, ...patch };
      for (const fn of listeners) fn();
    };
    const currentGeneration = (value) => !disposed && value === generation;
    async function json(name, query, body, signal) {
      const response = await fetch(routeUrl(name, query), { method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
        headers: body ? { 'content-type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined, signal });
      const value = await responseJson(response);
      if (!response.ok) throw new Error(value?.error?.message || value?.error || value?.detail || 'autoSkill 请求失败（' + response.status + '）');
      if (!object(value)) throw new Error('autoSkill 返回了无法识别的响应');
      return value;
    }
    async function read(resource, query = {}) {
      const controller = new AbortController();
      requests.add(controller);
      try { return await json('read', { session_id: sessionId, resource, ...query }, undefined, controller.signal); }
      finally { requests.delete(controller); }
    }
    function finishAction(key, action, result) {
      const state = result.request_state || 'unknown';
      const next = { ...action, state, result, error: result.error?.message || null };
      publish({ actions: { ...snapshot.actions, [key]: next } });
      if (state === 'completed' && action.operation === 'report.retry') publish({ reports: { ...snapshot.reports, [action.payload.report_id]: undefined } });
      return next;
    }
    async function command(operation, payload, bindingId) {
      if (disposed) return;
      const key = actionKey(operation, payload);
      const existing = snapshot.actions[key];
      if (existing && ['pending', 'unknown'].includes(existing.state)) return existing;
      const action = { key, operation, payload, binding_id: bindingId, request_id: requestId(), state: 'pending' };
      const controller = new AbortController();
      const startedGeneration = generation;
      requests.add(controller);
      publish({ actions: { ...snapshot.actions, [key]: action } });
      try {
        const result = await json('task', {}, { session_id: sessionId, binding_id: bindingId, operation, payload, request_id: action.request_id }, controller.signal);
        if (!currentGeneration(startedGeneration)) return action;
        return finishAction(key, action, result);
      } catch (error) {
        if (!currentGeneration(startedGeneration)) return action;
        return finishAction(key, action, { request_state: 'unknown', error: { message: message(error) } });
      } finally { requests.delete(controller); }
    }
    async function queryAction(key) {
      if (disposed) return;
      const action = snapshot.actions[key];
      if (action?.state !== 'unknown') return;
      const startedGeneration = generation;
      try {
        const result = await read('operation', { request_id: action.request_id });
        if (!currentGeneration(startedGeneration)) return;
        if (result.request_state === 'completed' && object(result.data) && result.data.request_state) finishAction(key, action, result.data);
        else publish({ actions: { ...snapshot.actions, [key]: { ...action, error: result.error?.message || '仍未查到操作结果，请稍后再查询。' } } });
      } catch (error) {
        if (currentGeneration(startedGeneration)) publish({ actions: { ...snapshot.actions,
          [key]: { ...action, error: '查询失败：' + message(error) } } });
      }
    }
    async function verifyReport(reportId) {
      if (disposed) return;
      const startedGeneration = generation;
      publish({ reports: { ...snapshot.reports, [reportId]: { state: 'checking' } } });
      try {
        const result = await read('report', { report_id: reportId });
        if (!currentGeneration(startedGeneration)) return;
        const receipt = result.data;
        const ready = result.request_state === 'completed' && receipt?.status === 'ready' && receipt.report_id === reportId
          && receipt.excel && receipt.markdown && receipt.sha256 && receipt.markdown_sha256;
        publish({ reports: { ...snapshot.reports, [reportId]: ready ? { state: 'ready', receipt }
          : { state: 'failed', error: receipt?.error || result.error?.message || '报告尚不可下载，请查看任务状态或重试导出。' } } });
      } catch (error) {
        if (currentGeneration(startedGeneration)) publish({ reports: { ...snapshot.reports, [reportId]: { state: 'failed', error: message(error) } } });
      }
    }
    return {
      sessionId, getSnapshot: () => snapshot,
      subscribe: (fn) => {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
      command, queryAction, verifyReport,
      queryRecorded: (record) => {
        const key = actionKey(record.operation, record);
        if (!snapshot.actions[key]) publish({ actions: { ...snapshot.actions, [key]: {
          key, operation: record.operation, request_id: record.request_id, payload: record, state: 'unknown',
        } } });
        return queryAction(key);
      },
      reset: () => {
        generation += 1;
        for (const request of requests) request.abort();
        requests.clear();
        const actions = {};
        for (const [key, action] of Object.entries(snapshot.actions)) actions[key] = action.state === 'pending' ? { ...action, state: 'unknown' } : action;
        publish({ reports: {}, actions });
      },
      dispose: () => {
        disposed = true;
        generation += 1;
        for (const request of requests) request.abort();
        requests.clear();
        listeners.clear();
      },
    };
  }

  function useSnapshot(store) {
    return React.useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  }

  function actionNotice(store, action) {
    if (!action) return null;
    return h('div', { role: action.state === 'failed' || action.state === 'unknown' ? 'status' : undefined },
      action.state === 'pending' ? '正在处理…' : action.error || (action.state === 'completed' ? '已确认操作完成。' : '操作结果尚未确认。'),
      action.state === 'unknown' ? button('查询操作结果', () => { void store.queryAction(action.key); }) : null);
  }

  function reportLinks(store, reportId, receipt) {
    const formats = [['excel', '下载 Excel'], ['markdown', '下载 Markdown 汇总']];
    if (receipt.bugs && receipt.bugs_sha256) formats.push(['bugs', '下载 Bug 清单']);
    return formats.map(([format, text]) => h('a', {
      key: format, href: routeUrl('report', { session_id: store.sessionId, report_id: reportId, format }), download: '', style: STYLE.button,
    }, text));
  }

  function reportControls(store, snapshot, report, bindingId) {
    const reportId = report.report_id;
    if (!reportId) return null;
    const verified = snapshot.reports[reportId];
    const action = snapshot.actions[actionKey('report.retry', report)];
    const missingBugs = verified?.state === 'ready' && !verified.receipt.bugs;
    return h('div', { key: reportId, style: { marginTop: 8 } },
      h('div', { style: STYLE.row }, h('span', {}, '测试报告：' + reportId),
        verified?.state === 'ready' ? reportLinks(store, reportId, verified.receipt)
          : button(verified?.state === 'checking' ? '正在校验报告…' : '校验并获取报告', () => { void store.verifyReport(reportId); }, verified?.state === 'checking'),
        report.receipt_status === 'failed' || verified?.state === 'failed' || missingBugs
          ? button('重试导出报告', () => { void store.command('report.retry', { report_id: reportId }, bindingId); }, ['pending', 'unknown'].includes(action?.state)) : null),
      missingBugs ? h('p', { style: STYLE.muted }, '旧报告缺少 Bug 清单，可重试导出补齐。') : null,
      verified?.error ? h('p', { role: 'status' }, verified.error) : null, actionNotice(store, action));
  }

  function identityControls(autoskill, snapshot, identity, run) {
    if (identity.host_session_id && identity.host_session_id !== autoskill.store.sessionId) return null;
    const action = snapshot.actions[actionKey('run.cancel', identity)];
    const canCancel = run ? run.can_cancel === true : Boolean(identity.run_id);
    return h('div', {}, h('div', { style: STYLE.row },
      identity.project_id ? button('打开项目', () => autoskill.navigation.open('cases?projects=' + encodeURIComponent(identity.project_id) + '&range=all')) : null,
      identity.run_id ? button('打开执行报告', () => autoskill.navigation.open('runs/' + encodeURIComponent(identity.run_id))) : null,
      identity.run_id && canCancel ? button('取消执行', () => { void autoskill.store.command('run.cancel', { run_id: identity.run_id }, identity.binding_id); },
        ['pending', 'unknown'].includes(action?.state)) : null), actionNotice(autoskill.store, action));
  }

  function parseResult(block) {
    for (const part of list(block?.content)) {
      if (part.type !== 'text' || typeof part.text !== 'string') continue;
      try {
        const value = JSON.parse(part.text);
        if (object(value) && value.version === 1 && object(value.identity)) return value;
      } catch { /* Text tool output need not be JSON. */ }
    }
    return null;
  }

  function toolRecord(phase, block) {
    const result = phase === 'result' ? parseResult(block) : null;
    const recorded = object(block?.meta?.autoskill) ? block.meta.autoskill : {};
    const identity = result?.identity || recorded;
    return { identity, state: result?.request_state || recorded.request_state,
      data: result?.data,
      operation: result?.operation || recorded.operation, request_id: result?.request_id || recorded.request_id,
      raw: phase === 'result' ? list(block.content).map((part) => part.type === 'text' ? part.text : JSON.stringify(part)).join('\n') : block.argsRaw };
  }

  function toolState(phase, state) {
    if (phase === 'preparing') return '正在准备参数…';
    if (phase === 'start') return '工具操作进行中…';
    if (state === 'completed') return '工具操作已完成';
    return state === 'unknown' ? '操作结果尚未确认' : '工具操作未完成';
  }

  function recordedFacts(record) {
    const { identity, data } = record;
    const facts = [];
    if (identity.task_session_id) facts.push('任务：' + identity.task_session_id);
    if (identity.workflow_id) facts.push('工作流：' + identity.workflow_id + (identity.cycle_id ? ' · ' + identity.cycle_id : ''));
    if (identity.run_id) facts.push('执行：' + identity.run_id);
    if (data?.conclusion) facts.push('业务验证：' + label(data.conclusion));
    return facts.map((fact) => h('p', { key: fact, style: STYLE.muted }, fact));
  }

  function recordedRun(record) {
    const runId = record.identity.run_id;
    if (record.data?.id === runId && record.data?.status) {
      return { ...record.data, can_cancel: ACTIVE.has(record.data.status) };
    }
    return list(record.data?.runs?.items).find((run) => run.id === runId);
  }

  function ToolCard({ phase, block, toolName, autoskill }) {
    const snapshot = useSnapshot(autoskill.store);
    const record = toolRecord(phase, block);
    const { identity, state, raw } = record;
    return h('details', { style: STYLE.card, 'aria-label': 'autoSkill 工具结果' },
      h('summary', {}, toolName === 'autoskill_read' ? '查看 autoSkill 查询结果' : '查看 autoSkill 操作结果'),
      h('p', {}, toolState(phase, state)),
      identity.project_id ? h('p', { style: STYLE.muted }, '项目：' + identity.project_id) : null,
      recordedFacts(record),
      phase === 'result' ? identityControls(autoskill, snapshot, identity, recordedRun(record)) : null,
      state === 'unknown' && record.request_id ? button('查询操作结果', () => {
        void autoskill.store.queryRecorded({ ...identity, operation: record.operation, request_id: record.request_id });
      }) : null,
      identity.report_id ? reportControls(autoskill.store, snapshot, { report_id: identity.report_id }, identity.binding_id) : null,
      raw ? h('details', {}, h('summary', {}, '查看工具输入与结果'), h('pre', { style: STYLE.pre }, raw)) : null);
  }

  exports.inject = ['slots', 'layout'];
  exports.apply = (ctx, config = {}) => {
    const port = config.port ?? 8787;
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid autoSkill port');
    activationCount += 1;
    // Keep the public cache key stable during one activation, without clearing user storage.
    const loadId = Date.now().toString(36) + '-' + activationCount.toString(36) + '-' + Math.random().toString(36).slice(2);
    const nav = navigation(ctx);
    const stores = new Map();
    const inject = (sessionId) => {
      if (!stores.has(sessionId)) stores.set(sessionId, sessionStore(sessionId));
      return { autoskill: { store: stores.get(sessionId), navigation: nav } };
    };
    const panels = [['autoskill-dashboard', 'autoSkill 仪表盘', 'dashboard'], ['autoskill-schedules', 'autoSkill 定时任务', 'schedules']];
    for (const [key, title, page] of panels) {
      const Panel = () => {
        const destination = React.useSyncExternalStore(nav.subscribe, () => nav.route(key), () => nav.route(key));
        const host = React.useSyncExternalStore(nav.subscribe, nav.getHostSnapshot, nav.getHostSnapshot);
        if (!host.origin) return h('div', { style: STYLE.card, role: 'status' }, host.error || '正在连接 autoSkill 仪表盘…',
          host.error ? button('重试连接', () => { void nav.refreshHost(); }) : null);
        return h('iframe', { src: host.origin + '/?embed=1&activation=' + loadId + '#/' + destination, title,
          style: { width: '100%', height: '100%', border: 0, display: 'block' } });
      };
      const Icon = () => h('span', { 'aria-hidden': true, style: { fontSize: 15 } }, page === 'dashboard' ? '▦' : '◷');
      ctx.effect(() => ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key }, Panel)), 'autoskill: ' + key);
      ctx.effect(() => ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: key, order: 30, label: () => title }, Icon)), 'autoskill: nav ' + key);
    }
    for (const key of ['autoskill_read', 'autoskill_task']) ctx.effect(() => ctx.slots.inject('tool.call.toolview', () =>
      ctx.slots.register({ name: 'tool.call.toolview', key, inject }, ToolCard)), 'autoskill: ' + key);
    ctx.on('connection/reset', () => { nav.reset(); for (const store of stores.values()) store.reset(); });
    ctx.effect(() => () => { for (const store of stores.values()) store.dispose(); stores.clear(); nav.dispose(); }, 'autoskill: client lifetime');
  };
  return module.exports;
} });
