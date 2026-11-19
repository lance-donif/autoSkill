// Local examples only. A run's simulated clock advances when the demo is open.
import { casesApi } from './cases-api.js';
import { schedulesApi } from './schedules-api.js';

const outcomeStatus = { pass: 'Pass', fail: 'Fail', not_run: 'NotRun' };
export const LOAD_PAGE_SIZE = 200;
const active = (run) => run.status === 'Queued' || run.status === 'Running';
const stamp = (time) => new Date(time).toISOString();
const fraction = (count, total) => total ? count / total : null;
const problem = (detail, status = 422) => Response.json({ detail }, { status });

function caseRows(context, projectId) {
  const params = new URLSearchParams({ limit: '100000' });
  if (projectId) params.set('projects', projectId);
  return casesApi({ ...context, path: '/cases', method: 'GET', params }).items;
}

function receipt(id, ready) {
  const reportId = 'run_' + id;
  const base = '/api/task-reports/' + encodeURIComponent(reportId);
  return { report_id: reportId, status: ready ? 'ready' : 'pending', download_url: base + '/excel',
    markdown_download_url: base + '/markdown', bugs_download_url: base + '/bugs' };
}

function state(outcome, phase = 'settled') {
  return {
    outcome, phase, lifecycle: ({ queued: 'pending', running: 'active', cancelled: 'cancelled' })[phase] || 'completed',
    checks: { business: 'not_applicable', performance: 'not_applicable', quality: 'not_applicable' },
    evidence: 'not_applicable', delivery: 'not_applicable', report: 'not_generated', scope: {},
  };
}

function outcomeCounts(rows) {
  const counts = { pass: 0, fail: 0, not_run: 0 };
  for (const row of rows) counts[row.state?.outcome || 'not_run']++;
  return counts;
}

function compactCounts(run) {
  const counts = { pass: 0, fail: 0, not_run: run.total - run.done };
  const templates = run.load_templates;
  const complete = run.done - (run.cancelled_index != null ? 1 : 0);
  const rounds = Math.floor(complete / templates.length);
  const remainder = complete % templates.length;
  let requests = 0;
  for (const [i, row] of templates.entries()) {
    const count = rounds + Number(i < remainder);
    counts[row.simulateFailure ? 'fail' : 'pass'] += count;
    if (row.kind === 'api') requests += count;
  }
  if (run.cancelled_index != null) {
    counts.fail++;
    if (templates[run.cancelled_index % templates.length].kind === 'api') requests++;
  }
  return { counts, requests };
}

function aggregate(run, phase) {
  const compact = run.load_templates && compactCounts(run);
  const counts = compact?.counts || outcomeCounts(run.results);
  const outcome = counts.fail ? 'fail' : counts.pass === run.total ? 'pass' : 'not_run';
  run.done = counts.pass + counts.fail;
  run.passed = counts.pass;
  run.failed = counts.fail;
  run.not_run = counts.not_run;
  run.history_status = outcomeStatus[outcome];
  if (phase === 'settled') run.status = run.history_status;
  run.state_counts = counts;
  run.state = state(outcome, phase);
  run.test_report = receipt(run.id, !active(run));
  run.state.report = run.test_report.status;
  if (run.load) {
    const apiCount = compact?.requests ?? run.results.filter((r) => r.kind === 'api' && r.started_at).length;
    const percentile = (p) => Math.ceil(p * run.done) > (run.cancelled_index != null ? 1 : 0) ? 180 : 0;
    const elapsed = run.started_at && run.finished_at ? (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000 : 0;
    run.metrics = { latency_samples: run.done, request_count: apiCount, throughput_per_second: fraction(run.done, elapsed),
      error_rate: fraction(run.failed, run.done), p50_ms: percentile(0.5), p95_ms: percentile(0.95), p99_ms: percentile(0.99) };
  }
}

function start(context, projectId, ids, body, batch, load) {
  const { db, fixtures } = context;
  const project = casesApi({ ...context, method: 'GET', path: '/projects', params: new URLSearchParams() }).find((item) => item.id === projectId);
  if (!project) throw new Error('找不到所选项目');
  const cases = caseRows(context, projectId);
  const all = new Map(cases.map((item) => [item.case_id, item]));
  const chosen = ids?.length ? [...new Set(ids)] : [...all.keys()];
  if (!chosen.length || chosen.some((id) => !all.has(id))) throw new Error('用例不存在或已删除');
  const iterations = load?.iterations ?? 1;
  const total = chosen.length * iterations;
  if (!Number.isSafeInteger(total)) throw new Error('目标总条数超出浏览器可精确计数的整数范围');
  const now = Date.now();
  const id = 'demo-' + crypto.randomUUID();
  const run = { id, family: load ? 'load' : 'replay', simulated: true, trigger: body.trigger || 'manual', task_id: body.task_id || null,
    project_id: projectId, project_name: project.name + ' · 浏览器模拟', case_ids: chosen, batch_id: batch, engine: body.engine || 'playwright',
    headed: body.headed, status: 'Queued', created_at: stamp(now), started_at: null, finished_at: null,
    current: '预览模拟：等待开始', reason: '', total, done: 0, results: [] };
  if (load) run.load = load;
  const seedResults = fixtures['/results']?.items || [];
  const compact = load && total > LOAD_PAGE_SIZE;
  const storedIterations = compact ? 1 : iterations;
  for (let iteration = 1; iteration <= storedIterations; iteration++) {
    for (const caseId of chosen) {
      const item = all.get(caseId);
      const expected = seedResults.find((r) => r.project_id === projectId && r.case_id === caseId && !r.prerequisite);
      run.results.push({ id: id + '-' + (run.results.length + 1), run_id: id, project_id: projectId, project_name: run.project_name,
        case_id: caseId, case_title: item.title, kind: item.kind, case_revision: item.revision || 1, prerequisite: false,
        status: 'NotRun', history_status: 'NotRun', state: state('not_run', 'queued'), reason: '', actual: '', category: '',
        started_at: null, duration_ms: null, simulated: true, simulateFailure: expected?.state?.outcome === 'fail',
        detail: load ? { load_iteration: iteration, load_user: (iteration - 1) % load.concurrency + 1 } : {} });
    }
  }
  if (compact) {
    run.load_templates = run.results;
    run.results = [];
  }
  aggregate(run, 'queued');
  db.runs.unshift(run);
  return run;
}

function finishRow(row, outcome, time, cancelled = false) {
  row.state = state(outcome);
  row.history_status = row.status = outcomeStatus[outcome];
  row.duration_ms = cancelled ? 0 : 180;
  row.started_at = stamp(time - row.duration_ms);
  row.category = outcome === 'fail' ? 'assertion' : '';
  row.reason = cancelled ? '模拟执行已取消' : outcome === 'fail' ? '模拟检查的实际值与预期不同' : '';
  row.actual = outcome === 'pass' ? '模拟步骤已完成' : row.reason;
  row.detail.steps = [{ text: '执行示例步骤', status: outcome, ms: row.duration_ms, error: row.reason }];
}

const durationOf = (run) => 1600 + Math.min(3200, run.total * 240);

export function runPage(run, requestedOffset = 0) {
  if (!run.load_templates) return run;
  const templates = run.load_templates;
  const lastPage = Math.floor((run.total - 1) / LOAD_PAGE_SIZE);
  const page = Math.min(lastPage, Math.max(0, Math.floor(Number(requestedOffset) / LOAD_PAGE_SIZE) || 0));
  const offset = page * LOAD_PAGE_SIZE;
  const end = Math.min(run.total, offset + LOAD_PAGE_SIZE);
  const results = [];
  for (let i = offset; i < end; i++) {
    const original = templates[i % templates.length];
    const iteration = Math.floor(i / templates.length) + 1;
    const row = { ...original, id: run.id + '-' + (i + 1), detail: {
      load_iteration: iteration, load_user: (iteration - 1) % run.load.concurrency + 1,
    } };
    if (i < run.done) {
      const interrupted = run.cancelled_index === i;
      const at = interrupted ? Date.parse(run.finished_at) : Date.parse(run.started_at) + Math.ceil((i + 1) * durationOf(run) / run.total);
      finishRow(row, original.simulateFailure || interrupted ? 'fail' : 'pass', at, interrupted);
    }
    results.push(row);
  }
  return { ...run, project_name: run.project_name + `（明细 ${offset + 1}–${end} / ${run.total}）`, results,
    result_page: { offset, end, total: run.total } };
}

export function advanceRuns(db, now = Date.now()) {
  let changed = false;
  for (const run of db.runs || []) {
    if (!run.simulated || !active(run)) continue;
    const startTime = Date.parse(run.created_at) + 550;
    const elapsed = now - startTime;
    if (elapsed < 0) continue;
    const duration = durationOf(run);
    const done = Math.min(run.total, Math.floor(elapsed / duration * run.total));
    if (run.status === 'Running' && run.done === done) continue;
    if (run.load_templates) run.done = done;
    else {
      for (let i = run.done; i < done; i++) {
        finishRow(run.results[i], run.results[i].simulateFailure ? 'fail' : 'pass', startTime + Math.ceil((i + 1) * duration / run.total));
      }
    }
    run.started_at = stamp(startTime);
    const finished = done === run.total;
    run.status = finished ? 'Pass' : 'Running';
    const targets = run.load_templates || run.results;
    run.current = finished ? '' : '预览模拟：' + targets[done % targets.length].case_title;
    run.finished_at = finished ? stamp(startTime + duration) : null;
    aggregate(run, finished ? 'settled' : 'running');
    if (finished && run.trigger === 'login' && run.status === 'Pass') {
      const project = db.caseLibrary?.projects.find((p) => p.id === run.project_id);
      if (project) project.session = { saved: true, saved_at: run.finished_at };
    }
    changed = true;
  }
  return changed;
}

function scoped(rows, params, timeKey) {
  const projects = params.get('projects')?.split(',');
  const start = Date.parse(params.get('start'));
  const end = Date.parse(params.get('end'));
  return rows.filter((r) => {
    if (projects && !projects.includes(r.project_id)) return false;
    if (!timeKey) return true;
    const time = Date.parse(r[timeKey] || r.created_at);
    return !(time < start || time >= end);
  });
}

function matches(row, params) {
  const status = params.get('status');
  const value = row.state?.outcome || 'not_run';
  if (status === 'active' && !active(row)) return false;
  if (status && status !== 'active') {
    const selected = status === 'NotPassed' ? 'Fail' : status;
    const expected = ['Pass', 'Fail', 'NotRun'].includes(selected) ? outcomeStatus[value] : row.status;
    if (selected !== expected) return false;
  }
  if (params.has('trigger') && row.trigger !== params.get('trigger')) return false;
  for (const axis of ['phase', 'outcome', 'business', 'performance', 'quality', 'evidence', 'report', 'delivery']) {
    if (params.has(axis) && (row.state?.[axis] ?? row.state?.checks?.[axis]) !== params.get(axis)) return false;
  }
  return true;
}

function paginate(rows, params) {
  const offset = Math.max(0, Number(params.get('offset')) || 0);
  const limit = Math.max(1, Number(params.get('limit')) || 50);
  return { items: rows.slice(offset, offset + limit), total: rows.length, state_counts: outcomeCounts(rows) };
}

function historyRows(runs, params) {
  const items = [];
  for (const run of runs) {
    if (run.family === 'load' || run.trigger === 'login') continue;
    for (const r of run.results || []) {
      if (!r.prerequisite) items.push({ ...r, run_id: run.id, project_name: run.project_name, project_id: run.project_id,
        created_at: run.created_at, trigger: run.trigger, history_status: outcomeStatus[r.state?.outcome || 'not_run'] });
    }
  }
  return scoped(items, params, 'started_at').sort((a, b) => Date.parse(b.started_at || b.created_at) - Date.parse(a.started_at || a.created_at));
}

function runList(runs) {
  return runs.map(({ results, ...r }) => ({ ...r, history_status: outcomeStatus[r.state?.outcome || 'not_run'] }));
}

function trend(rows, params) {
  const span = Date.parse(params.get('end')) - Date.parse(params.get('start'));
  const unit = span <= 36 * 3600000 ? 'hour' : 'day';
  const shift = Number(params.get('tz')) * 60000;
  const buckets = new Map();
  for (const r of rows) {
    const iso = stamp(Date.parse(r.started_at || r.created_at) + shift);
    const key = unit === 'day' ? iso.slice(0, 10) : iso.slice(0, 13).replace('T', ' ') + ':00';
    const point = buckets.get(key) || { bucket: key, passed: 0, failed: 0, not_run: 0 };
    point[{ pass: 'passed', fail: 'failed', not_run: 'not_run' }[r.state?.outcome || 'not_run']]++;
    point.pass_rate = fraction(point.passed, point.passed + point.failed);
    buckets.set(key, point);
  }
  return { unit, points: [...buckets.values()].sort((a, b) => a.bucket.localeCompare(b.bucket)) };
}

function dashboard(context) {
  const { params, db } = context;
  const runs = scoped(db.runs, params, 'created_at').filter((r) => r.family !== 'load' && r.trigger !== 'login' && matches(r, params));
  const rows = historyRows(db.runs, params).filter((r) => matches(r, params));
  const counts = outcomeCounts(rows);
  const latest = runs[0];
  const batch = latest?.batch_id ? runs.filter((r) => r.batch_id === latest.batch_id) : runs.slice(0, 1);
  const passed = batch.reduce((sum, r) => sum + r.passed, 0);
  const total = batch.reduce((sum, r) => sum + r.total, 0);
  const seen = new Set();
  const latestFailures = rows.filter((r) => {
    const key = JSON.stringify([r.project_id, r.case_id]);
    if (seen.has(key)) return false;
    seen.add(key);
    return r.state?.outcome === 'fail';
  });
  const failures = params.get('current') === 'true' ? latestFailures : rows.filter((r) => r.state?.outcome === 'fail');
  const tasks = schedulesApi({ ...context, path: '/tasks', method: 'GET' });
  const cases = scoped(caseRows(context), params, null).filter((c) => !(Date.parse(c.created_at) >= Date.parse(params.get('end'))));
  const enabled = tasks.filter((t) => t.enabled).length;
  const summary = {
    cases: { total: cases.length, ui: cases.filter((c) => c.kind === 'ui').length, api: cases.filter((c) => c.kind === 'api').length,
      added: cases.filter((c) => !params.has('start') || Date.parse(c.created_at) >= Date.parse(params.get('start'))).length },
    runs: { total: runs.length, scheduled: runs.filter((r) => r.trigger === 'schedule').length, active: runs.filter(active).length,
      latest_batch: { passed, total, pass_rate: fraction(passed, total) } },
    results: { executions: rows.length, passed: counts.pass, failed: counts.fail, not_run: counts.not_run, state_counts: counts,
      pass_rate: fraction(counts.pass, counts.pass + counts.fail), still_failing: latestFailures.length },
    trend: trend(rows, params),
    tasks: { total: tasks.length, enabled, paused: tasks.length - enabled, fired_in_range: runs.filter((r) => r.trigger === 'schedule').length, tasks: tasks.slice(0, 5) },
  };
  return { summary, recent: paginate(runList(runs), params), failures: paginate(failures, params) };
}

function batchStart(context, load = null) {
  const body = context.body;
  const items = body.items || [body];
  if (!Array.isArray(items) || !items.length) return problem('请选择用例');
  const batch = items.length > 1 ? 'batch-' + crypto.randomUUID() : null;
  const runs = [], skipped = [];
  for (const item of items) {
    try {
      const r = start(context, item.project_id, item.case_ids, body, batch, load);
      runs.push({ id: r.id, total: r.total, project_id: r.project_id });
    } catch (error) { skipped.push({ project_id: item.project_id, reason: error.message }); }
  }
  return runs.length ? { batch_id: batch, runs, skipped } : problem(skipped[0].reason);
}

function removeRuns(db, ids) {
  if (!Array.isArray(ids)) return problem('运行编号无效');
  const deleted = [], errors = [];
  for (const id of new Set(ids)) {
    const index = db.runs.findIndex((r) => r.id === id);
    if (index < 0) errors.push({ run_id: id, message: '运行不存在' });
    else { db.runs.splice(index, 1); deleted.push({ run_id: id }); }
  }
  return { deleted, pending: [], errors };
}

export function runsApi(context) {
  const { path, method, params, db, body } = context;
  const all = db.runs;
  if (path === '/dashboard' || path === '/summary') {
    const value = dashboard(context);
    return path === '/summary' ? value.summary : value;
  }
  if (method === 'GET' && path === '/results') return paginate(historyRows(all, params).filter((r) => matches(r, params)), params);
  if (method === 'POST' && path === '/runs/delete') return removeRuns(db, body.run_ids);
  if (path === '/load-tests/options') {
    const items = scoped(caseRows(context), params, null);
    const projects = casesApi({ ...context, path: '/projects', method: 'GET' }).filter((p) => items.some((c) => c.project_id === p.id));
    return { items, projects };
  }
  if (path === '/runs' || path === '/load-tests') {
    if (method === 'GET') {
      const runs = scoped(all, params, 'created_at').filter((r) => r.trigger !== 'login' && (path !== '/load-tests' || r.family === 'load') && matches(r, params));
      return paginate(runList(runs), params);
    }
    if (method !== 'POST') return undefined;
    if (path === '/runs') return batchStart(context);
    const { concurrency, iterations, ramp_up_seconds } = body;
    if (![concurrency, iterations].every((v) => Number.isSafeInteger(v) && v > 0) || !Number.isFinite(ramp_up_seconds) || ramp_up_seconds < 0) return problem('压测参数无效');
    return batchStart(context, { concurrency, iterations, ramp_up_seconds });
  }
  const login = path.match(/^\/projects\/([^/]+)\/login$/);
  if (login && method === 'POST') {
    const project = casesApi({ ...context, path: '/projects/' + login[1], method: 'GET' });
    const ids = [project.login?.ui_case, project.login?.api_case].filter(Boolean);
    if (!ids.length) return problem('请先选择登录用例');
    return start(context, project.id, ids, { ...body, trigger: 'login' }, null, null);
  }
  const detail = path.match(/^\/(runs|load-tests)\/([^/]+)(?:\/(cancel|rerun))?$/);
  if (!detail) return undefined;
  const [, family, id, action] = detail;
  const run = all.find((r) => r.id === id);
  if (!run || (family === 'load-tests' && run.family !== 'load')) return problem('运行不存在', 404);
  if (method === 'GET' && !action) return runPage(run, params.get('detail_offset'));
  if (method === 'DELETE' && !action) return removeRuns(db, [id]);
  if (method !== 'POST') return undefined;
  if (action === 'cancel') {
    if (active(run)) {
      const now = Date.now();
      if (run.status === 'Running' && run.done < run.total) {
        run.cancelled_index = run.done;
        if (run.load_templates) run.done++;
        else finishRow(run.results[run.done], 'fail', now, true);
      }
      run.status = 'Cancelled';
      run.finished_at = stamp(now);
      run.current = '';
      run.reason = '模拟运行已取消';
      aggregate(run, 'cancelled');
    }
    return run;
  }
  if (action === 'rerun') {
    const templates = run.load_templates;
    const targets = (templates || run.results).filter((r, i) => {
      if (r.prerequisite || (body.case_id && r.case_id !== body.case_id)) return false;
      if (!body.failed_only) return true;
      if (!templates) return r.state?.outcome === 'fail';
      return (i < run.done && r.simulateFailure) || (run.cancelled_index != null && i === run.cancelled_index % templates.length);
    });
    if (!targets.length) return problem('没有可重跑的目标用例');
    const remaining = new Set(caseRows(context, run.project_id).map((c) => c.case_id));
    const ids = [...new Set(targets.map((r) => r.case_id))];
    const kept = ids.filter((caseId) => remaining.has(caseId));
    if (!kept.length) return problem('这些用例已删除');
    const r = start(context, run.project_id, kept, { ...body, engine: body.engine || run.engine }, null, run.load);
    return { id: r.id, total: r.total, dropped: ids.length - kept.length };
  }
  return undefined;
}
