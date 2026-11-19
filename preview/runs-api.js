// Local examples only. A run's simulated clock advances when the demo is open.
import { loadConfiguration, mockSchedule, samplesDone, sampleTiming, mockLoadReport, mockLoadMetrics, legacyLoadReport } from './load-simulation.js';
import { casesApi } from './cases-api.js';
import { simulateCase, pendingDetails, completedDetails, actualText } from './simulated-details.js';
import { schedulesApi } from './schedules-api.js';
import { restoreResult, verdictStatus as outcomeStatus } from './verdicts.js';

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

function apiLoadCases(context, rows) {
  const definitions = context.db.caseLibrary.definitions;
  const allowed = new Map();
  function check(project, id, visiting) {
    const key = project + '/' + id;
    if (allowed.has(key)) return allowed.get(key);
    const candidate = definitions[key]?.case;
    if (!candidate || candidate.kind !== 'api' || visiting.has(key)) return false;
    const links = candidate.extensions || {};
    visiting.add(key);
    const valid = [...(links.depends_on || []), ...(links.postconditions || [])].every((ref) => check(project, ref, visiting));
    visiting.delete(key);
    allowed.set(key, valid);
    return valid;
  }
  return rows.filter((row) => row.kind === 'api' && row.replayable && definitions[row.project_id + '/' + row.case_id]?.case.extensions?.traffic !== 'resource'
    && check(row.project_id, row.case_id, new Set()));
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

function plannedOutcome(run, row) {
  return run.simulation_cases?.[row.case_id]?.outcome || (row.simulateFailure ? 'fail' : 'pass');
}

export function compactCaseCounts(run, index) {
  const n = run.load_templates.length;
  const completed = run.done - Number(run.cancelled_index != null);
  const count = Math.floor(completed / n) + Number(index < completed % n);
  const outcome = plannedOutcome(run, run.load_templates[index]);
  const assigned = Math.floor(run.total / n) + Number(index < run.total % n);
  const counts = { pass: 0, fail: 0, not_run: assigned - count };
  counts[outcome] += count;
  if (run.cancelled_index != null && index === run.cancelled_index % n && outcome !== 'not_run') {
    counts.fail++;
    counts.not_run--;
  }
  return counts;
}

function compactCounts(run) {
  const counts = { pass: 0, fail: 0, not_run: 0 };
  let requests = 0;
  for (const [i, row] of run.load_templates.entries()) {
    const current = compactCaseCounts(run, i);
    for (const key of Object.keys(counts)) counts[key] += current[key];
    if (row.kind === 'api') requests += current.pass + current.fail;
  }
  return { counts, requests };
}

export function restoreRunVerdicts(run) {
  let changed = run.history_status === 'Unverified' || run.unverified > 0
    || !Object.hasOwn(outcomeStatus, run.state?.outcome) || !Object.hasOwn(run.state_counts || {}, 'not_run')
    || Object.keys(run.state_counts || {}).some((key) => !Object.hasOwn(outcomeStatus, key));
  for (const row of run.results) {
    if (restoreResult(row)) changed = true;
  }
  if (!changed) return false;
  const targets = run.results.filter((row) => !row.prerequisite);
  const counts = run.load_templates ? compactCounts(run).counts : outcomeCounts(targets);
  if (!run.load_templates) counts.not_run += Math.max(0, Math.max(run.total || 0, run.done || 0) - targets.length);
  const judged = counts.pass + counts.fail;
  let outcome = 'not_run';
  if (counts.fail || run.status === 'Fail') outcome = 'fail';
  else if (!active(run) && counts.pass) outcome = counts.pass === run.total ? 'pass' : 'fail';
  run.state = { ...run.state, outcome };
  run.recorded_counts ??= { passed: run.passed || 0, failed: run.failed || 0, blocked: run.blocked || 0 };
  Object.assign(run, { state_counts: counts, history_status: outcomeStatus[outcome], passed: counts.pass,
    failed: counts.fail, not_run: counts.not_run, judged, pass_rate: fraction(counts.pass, judged) });
  for (const key of ['blocked', 'unverified', 'cancelled', 'pending']) {
    if (Object.hasOwn(run, key)) run[key] = 0;
  }
  return true;
}

function aggregate(run, phase) {
  const compact = run.load_templates && compactCounts(run);
  const counts = compact?.counts || outcomeCounts(run.results);
  const outcome = counts.fail ? 'fail' : counts.pass === run.total ? 'pass' : 'not_run';
  const judged = counts.pass + counts.fail;
  run.passed = counts.pass;
  run.failed = counts.fail;
  run.not_run = counts.not_run;
  run.history_status = outcomeStatus[outcome];
  if (phase === 'settled') run.status = run.history_status;
  run.state_counts = counts;
  run.state = state(outcome, phase);
  run.test_report = receipt(run.id, !active(run));
  run.state.report = run.test_report.status;
  if (run.load_schedule) run.metrics = mockLoadMetrics(run);
  else if (run.load) {
    const apiCount = compact?.requests ?? run.results.filter((r) => r.kind === 'api' && r.started_at).length;
    const targets = run.load_templates || run.results;
    const aborted = run.cancelled_index != null && plannedOutcome(run, targets[run.cancelled_index % targets.length]) !== 'not_run';
    const percentile = (p) => {
      if (!judged) return null;
      return Math.ceil(p * judged) > Number(aborted) ? 180 : 0;
    };
    const elapsed = run.started_at && run.finished_at ? (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000 : 0;
    run.metrics = { latency_samples: judged, request_count: apiCount, throughput_per_second: fraction(judged, elapsed),
      error_rate: fraction(run.failed, judged), p50_ms: percentile(0.5), p95_ms: percentile(0.95), p99_ms: percentile(0.99) };
  }
}

function start(context, projectId, ids, body, batch, load, seed) {
  const { db, fixtures } = context;
  const project = casesApi({ ...context, method: 'GET', path: '/projects', params: new URLSearchParams() }).find((item) => item.id === projectId);
  if (!project) throw new Error('找不到所选项目');
  const cases = caseRows(context, projectId);
  const options = load ? apiLoadCases(context, cases) : cases;
  const all = new Map(options.map((item) => [item.case_id, item]));
  const chosen = ids?.length ? [...new Set(ids)] : [...all.keys()];
  if (!chosen.length || chosen.some((id) => !all.has(id))) {
    throw new Error(load ? '负载测试只能选择可回放的 API 用例' : '用例不存在或已删除');
  }
  const definitions = Object.fromEntries(chosen.map((caseId) => {
    const definition = db.caseLibrary.definitions[projectId + '/' + caseId]?.case;
    if (!definition) throw new Error('缺少用例定义：' + caseId);
    return [caseId, structuredClone(definition)];
  }));
  const seedResults = fixtures['/results']?.items || [];
  const plans = Object.fromEntries(chosen.map((caseId) => {
    const previous = seedResults.find((r) => r.project_id === projectId && r.case_id === caseId && !r.prerequisite);
    const detail = db.caseLibrary.details[projectId + '/' + caseId];
    return [caseId, simulateCase(definitions[caseId], detail, project, previous, fixtures)];
  }));
  const iterations = load?.iterations ?? 1;
  const schedule = load?.mode ? mockSchedule(load, chosen.map((caseId) => plans[caseId])) : null;
  const total = schedule?.total ?? chosen.length * iterations;
  if (!Number.isSafeInteger(total)) throw new Error('目标总条数超出浏览器可精确计数的整数范围');
  const now = seed?.created ?? Date.now();
  const id = seed?.id || 'demo-' + crypto.randomUUID();
  const run = { id, family: load ? 'load' : 'replay', simulated: true, trigger: body.trigger || 'manual', task_id: body.task_id || null,
    project_id: projectId, project_name: project.name + ' · 浏览器模拟', case_ids: chosen, batch_id: batch, engine: load?.mode ? 'locust' : body.engine || 'playwright',
    report_project: { id: projectId, name: project.name, base_url: project.base_url }, report_definitions: definitions, simulation_cases: plans,
    headed: body.headed, status: 'Queued', created_at: stamp(now), started_at: null, finished_at: null,
    current: '预览模拟：等待开始', reason: '', total, done: 0, results: [] };
  if (load) run.load = load;
  if (schedule) Object.assign(run, { mode: load.mode, load_schedule: schedule, mock_elapsed_ms: 0 });
  const compact = load && (schedule || total > LOAD_PAGE_SIZE);
  const storedIterations = compact ? 1 : iterations;
  for (let iteration = 1; iteration <= storedIterations; iteration++) {
    for (const caseId of chosen) {
      const item = all.get(caseId);
      const plan = plans[caseId];
      run.results.push({ id: id + '-' + (run.results.length + 1), run_id: id, project_id: projectId, project_name: run.project_name,
        case_id: caseId, case_title: item.title, kind: item.kind, case_revision: item.revision || 1, prerequisite: false,
        status: 'NotRun', history_status: 'NotRun', state: state('not_run', 'queued'), reason: '', actual: '', category: '',
        started_at: null, duration_ms: null, simulated: true,
        detail: { ...pendingDetails(plan), ...(load && !schedule ? { load_iteration: iteration, load_user: (iteration - 1) % load.concurrency + 1 } : {}) } });
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

function finishRow(run, row, time, cancelled = false, duration = 180) {
  const plan = run.simulation_cases?.[row.case_id];
  const planned = plannedOutcome(run, row);
  const outcome = cancelled && planned !== 'not_run' ? 'fail' : planned;
  row.state = state(outcome);
  row.history_status = row.status = outcomeStatus[outcome];
  row.duration_ms = outcome === 'not_run' ? null : cancelled ? 0 : duration;
  row.started_at = row.duration_ms == null ? null : stamp(time - row.duration_ms);
  row.category = outcome !== 'fail' ? '' : cancelled ? 'cancelled' : 'assertion';
  row.reason = cancelled && outcome === 'fail' ? '模拟执行已取消' : plan?.reason || '';
  row.actual = actualText(plan, outcome, row.reason, cancelled);
  const { load_iteration, load_user } = row.detail;
  row.detail = completedDetails(plan, outcome, row.duration_ms, row.reason, cancelled);
  if (load_iteration != null) Object.assign(row.detail, { load_iteration, load_user });
  for (const check of row.detail.assertions || []) {
    const checks = row.state.checks;
    if (checks[check.scope] !== 'fail') checks[check.scope] = check.passed ? 'pass' : 'fail';
  }
}

const durationOf = (run) => 1600 + Math.min(3200, run.total * 240);

export function runPage(run, requestedOffset = 0) {
  const { simulation_cases: _plans, load_schedule: schedule, load_templates: templates, ...view } = run;
  if (schedule) view.load_report = mockLoadReport(run);
  else if (run.family === 'load' && !view.load_report) view.load_report = legacyLoadReport(run);
  if (!templates) return view;
  const lastPage = Math.floor((run.total - 1) / LOAD_PAGE_SIZE);
  const page = Math.min(lastPage, Math.max(0, Math.floor(Number(requestedOffset) / LOAD_PAGE_SIZE) || 0));
  const offset = page * LOAD_PAGE_SIZE;
  const end = Math.min(run.total, offset + LOAD_PAGE_SIZE);
  const results = [];
  for (let i = offset; i < end; i++) {
    const original = templates[i % templates.length];
    const iteration = Math.floor(i / templates.length) + 1;
    const timing = schedule ? sampleTiming(run, i) : null;
    const position = timing ? { load_user: timing.user, load_stage: timing.stage } : { load_iteration: iteration, load_user: (iteration - 1) % run.load.concurrency + 1 };
    const row = { ...original, id: run.id + '-' + (i + 1), detail: { ...original.detail, ...position } };
    if (i < run.done) {
      const interrupted = run.cancelled_index === i;
      const at = interrupted ? Date.parse(run.finished_at) : Date.parse(run.started_at) + Math.ceil((i + 1) * durationOf(run) / run.total);
      if (timing) {
        finishRow(run, row, Date.parse(run.started_at) + timing.end, interrupted, timing.latency);
        Object.assign(row.detail, position);
      } else finishRow(run, row, at, interrupted);
    }
    results.push(row);
  }
  return { ...view, project_name: schedule ? run.project_name : run.project_name + `（明细 ${offset + 1}–${end} / ${run.total}）`, results,
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
    const virtualElapsed = run.load_schedule ? Math.min(run.load_schedule.duration, Math.floor(elapsed / duration * run.load_schedule.duration)) : null;
    const done = run.load_schedule ? samplesDone(run.load_schedule, virtualElapsed) : Math.min(run.total, Math.floor(elapsed / duration * run.total));
    if (run.status === 'Running' && run.done === done && (virtualElapsed == null || virtualElapsed === run.mock_elapsed_ms)) continue;
    if (virtualElapsed != null) run.mock_elapsed_ms = virtualElapsed;
    if (!run.load_templates) {
      for (let i = run.done; i < done; i++) {
        finishRow(run, run.results[i], startTime + Math.ceil((i + 1) * duration / run.total));
      }
    }
    run.done = done;
    run.started_at = stamp(startTime);
    const finished = done === run.total;
    run.status = finished ? 'Pass' : 'Running';
    const targets = run.load_templates || run.results;
    run.current = finished ? '' : '预览模拟：' + targets[done % targets.length].case_title;
    run.finished_at = finished ? stamp(startTime + (run.load_schedule?.duration ?? duration)) : null;
    aggregate(run, finished ? 'settled' : 'running');
    if (finished && run.load_schedule && !run.metrics.latency_samples) run.reason = '所选接口均未产生模拟 HTTP 交换，没有可统计的请求样本。';
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
  if (status === 'Running') {
    if (row.status !== 'Running') return false;
  } else if (status && status !== 'active') {
    const selected = { NotPassed: 'fail', NotRun: 'not_run' }[status] || status.toLowerCase();
    if (selected !== value) return false;
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
  return runs.map(({ load_report: _report, load_schedule: _schedule, results: _rows, report_definitions: _definitions, simulation_cases: _plans, load_templates: _templates, ...r }) => ({ ...r, history_status: outcomeStatus[r.state?.outcome || 'not_run'] }));
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
  const failed = batch.reduce((sum, r) => sum + r.failed, 0);
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
      latest_batch: { passed, failed, not_run: total - passed - failed, total, pass_rate: fraction(passed, total) } },
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
    const items = apiLoadCases(context, scoped(caseRows(context), params, null));
    const projects = casesApi({ ...context, path: '/projects', method: 'GET' }).filter((p) => items.some((c) => c.project_id === p.id));
    return { items, projects };
  }
  if (path === '/runs' || path === '/load-tests') {
    if (method === 'GET') {
      if (path === '/load-tests' && params.has('mode') && !['performance', 'stress'].includes(params.get('mode'))) return problem('测试模式无效');
      const runs = scoped(all, params, 'created_at').filter((r) => r.trigger !== 'login' && (path !== '/load-tests' || r.family === 'load') && matches(r, params));
      const mode = path === '/load-tests' && params.get('mode');
      return paginate(runList(mode ? runs.filter((run) => (run.load?.mode || 'performance') === mode) : runs), params);
    }
    if (method !== 'POST') return undefined;
    if (path === '/runs') return batchStart(context);
    try { return batchStart(context, loadConfiguration(body)); }
    catch (error) { return problem(error.message); }
  }
  const login = path.match(/^\/projects\/([^/]+)\/login$/);
  if (login && method === 'POST') {
    const project = casesApi({ ...context, path: '/projects/' + login[1], method: 'GET' });
    const ids = [project.login?.ui_case, project.login?.api_case].filter(Boolean);
    if (!ids.length) return problem('请先选择登录用例');
    return runPage(start(context, project.id, ids, { ...body, trigger: 'login' }, null, null));
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
      if (!run.load_schedule && run.status === 'Running' && run.done < run.total) {
        run.cancelled_index = run.done;
        if (!run.load_templates) finishRow(run, run.results[run.done], now, true);
        run.done++;
      }
      run.status = 'Cancelled';
      run.finished_at = stamp(run.load_schedule && run.started_at ? Date.parse(run.started_at) + run.mock_elapsed_ms : now);
      run.current = '';
      run.reason = '模拟运行已取消';
      aggregate(run, 'cancelled');
    }
    return runPage(run);
  }
  if (action === 'rerun') {
    const templates = run.load_templates;
    const targets = (templates || run.results).filter((r, i) => {
      if (r.prerequisite || (body.case_id && r.case_id !== body.case_id)) return false;
      if (!body.failed_only) return true;
      if (!templates) return r.state?.outcome === 'fail';
      return compactCaseCounts(run, i).fail > 0;
    });
    if (!targets.length) return problem('没有可重跑的目标用例');
    const relevant = caseRows(context, run.project_id).filter((c) => !run.load || c.kind === 'api' && c.replayable);
    const remaining = new Set(relevant.map((c) => c.case_id));
    const ids = [...new Set(targets.map((r) => r.case_id))];
    const kept = ids.filter((caseId) => remaining.has(caseId));
    if (!kept.length) return problem('这些用例已删除');
    const r = start(context, run.project_id, kept, { ...body, engine: body.engine || run.engine }, null, run.load);
    return { id: r.id, total: r.total, dropped: ids.length - kept.length };
  }
  return undefined;
}

export function seedLoadRuns(db, fixtures, now = Date.now()) {
  const context = { db, fixtures, params: new URLSearchParams() };
  const examples = [
    { mode: 'performance', concurrency: 3, duration_seconds: 20, target_requests: 54 },
    { mode: 'stress', initial_concurrency: 2, step_concurrency: 2, max_concurrency: 6, step_duration_seconds: 6 },
  ];
  for (const [index, load] of examples.entries()) {
    const seed = { id: 'load-demo-' + load.mode, created: now - (2 - index) * 3600000 };
    const run = start(context, 'stock-room', ['ST-API-001', 'ST-API-002'], {}, null, loadConfiguration(load), seed);
    advanceRuns({ runs: [run] }, now);
  }
  db.runs.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
}
