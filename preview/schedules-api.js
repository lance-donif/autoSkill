// Schedule definitions and due times belong to the current browser sample session.
import { initCases } from './cases-api.js';
import { runsApi } from './runs-api.js';
import { workspaceConflict, workspaceIdentity } from './workspace.js';

const MINUTE = 60000;
const DAY = 86400000;
const dayNames = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

function check(condition, message) {
  if (!condition) throw new Error(message);
}

function validRule(rule) {
  check(rule && typeof rule === 'object', '请选择执行规则');
  if (rule.kind === 'interval') {
    check(Number.isSafeInteger(rule.minutes) && rule.minutes >= 1 && Number.isFinite(new Date(Date.now() + rule.minutes * MINUTE).getTime()), '间隔需要是有效的正整数分钟');
  } else if (rule.kind === 'at') {
    check(typeof rule.at === 'string' && Number.isFinite(Date.parse(rule.at)), '请选择有效的执行时间');
  } else {
    check(['daily', 'weekly'].includes(rule.kind), '不支持的执行规则');
    check(typeof rule.time === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(rule.time), '请填写有效的执行时间');
    check(typeof rule.timezone === 'string' && rule.timezone, '请选择执行时区');
    try { new Intl.DateTimeFormat('en', { timeZone: rule.timezone }); } catch { throw new Error('执行时区无效'); }
    if (rule.kind === 'weekly') check(Array.isArray(rule.days) && rule.days.length &&
      rule.days.every((day) => Number.isInteger(day) && day >= 0 && day < 7), '每周任务至少选择一个有效日期');
  }
}

function calendarDue(rule, now) {
  const clock = new Intl.DateTimeFormat('en-GB', { timeZone: rule.timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  function wallTime(stamp) {
    const local = Object.fromEntries(clock.formatToParts(stamp).map((part) => [part.type, part.value]));
    return Date.UTC(+local.year, +local.month - 1, +local.day, +local.hour, +local.minute);
  }
  const [hours, minutes] = rule.time.split(':').map(Number);
  const today = Math.floor(wallTime(now) / DAY) * DAY;
  for (let day = today; day <= today + 14 * DAY; day += DAY) {
    const weekday = (new Date(day).getUTCDay() + 6) % 7;
    if (rule.kind === 'weekly' && !rule.days.includes(weekday)) continue;
    const target = day + (hours * 60 + minutes) * MINUTE;
    // The offsets on either side handle DST gaps and repeated local times.
    const candidates = [target - DAY, target + DAY].map((sample) => target - (wallTime(sample) - sample));
    const first = Math.min(...candidates.filter((candidate) => wallTime(candidate) === target));
    if (Number.isFinite(first) && first > now) return first;
  }
  throw new Error('无法计算下次执行时间');
}

export function nextDue(rule, now = Date.now(), previous = now) {
  validRule(rule);
  let due;
  if (rule.kind === 'interval') {
    const step = rule.minutes * MINUTE;
    due = previous + (Math.max(0, Math.floor((now - previous) / step)) + 1) * step;
  } else if (rule.kind === 'at') {
    due = Date.parse(rule.at);
    check(due > now, '一次性任务需要一个将来的时间');
  } else due = calendarDue(rule, now);
  return new Date(due).toISOString();
}

function describe(rule) {
  if (rule.kind === 'daily') return '每天 ' + rule.time;
  if (rule.kind === 'weekly') return rule.days.map((day) => dayNames[day]).join('、') + ' ' + rule.time;
  if (rule.kind === 'at') return '仅一次';
  return rule.minutes % 60 === 0 ? `每 ${rule.minutes / 60} 小时` : `每 ${rule.minutes} 分钟`;
}

export function initSchedules(db, fixtures) {
  if (!db.tasks) {
    db.tasks = structuredClone(fixtures['/tasks'] || []);
    for (const task of db.tasks) {
      if (task.enabled && task.rule.kind !== 'at') task.next_due = nextDue(task.rule);
    }
  }
  return db.tasks;
}

function validateTask(task, projectIds) {
  check(task && typeof task.title === 'string' && task.title.trim(), '任务名称不能为空');
  check(projectIds.has(task.project_id), '项目不存在');
  check(task.kind === 'replay', '请选择预览中的用例回放任务');
  check(['playwright', 'selenium'].includes(task.engine), '请选择回放引擎');
  check(Array.isArray(task.case_ids) && task.case_ids.every((id) => typeof id === 'string' && id.length), '用例列表无效');
  check(task.headed == null || typeof task.headed === 'boolean', '浏览器窗口设置无效');
  check(['skip', 'queue'].includes(task.overlap) && typeof task.enabled === 'boolean', '定时任务设置无效');
  validRule(task.rule);
}

export function validateTasks(tasks, projectIds) {
  check(Array.isArray(tasks), '定时任务列表无效');
  const known = new Set(projectIds);
  const ids = new Set();
  for (const task of tasks) {
    validateTask(task, known);
    check(typeof task.id === 'string' && task.id.length && !ids.has(task.id), '定时任务编号无效');
    check(Number.isSafeInteger(task.revision) && task.revision >= 1, '定时任务版本无效');
    check(task.enabled ? typeof task.next_due === 'string' && Number.isFinite(Date.parse(task.next_due)) : task.next_due == null, '定时任务下次执行时间无效');
    ids.add(task.id);
  }
  return true;
}

function view(task, db) {
  const run = db.runs?.find((item) => item.id === task.last_run_id);
  const project = db.caseLibrary?.projects.find((item) => item.id === task.project_id);
  return { ...task, project_name: project?.name || task.project_name || task.project_id, rule_text: describe(task.rule),
    last_status: run?.status || null, last_run_state: run?.state || null, last_finished: run?.finished_at || null };
}

function launch(ctx, task) {
  const result = runsApi({ ...ctx, path: '/runs', method: 'POST', params: new URLSearchParams(), body: {
    items: [{ project_id: task.project_id, case_ids: task.case_ids }], engine: task.engine, headed: task.headed,
    trigger: 'schedule', task_id: task.id,
  } });
  if (result instanceof Response) return result;
  const run = result?.runs?.[0];
  check(run, '当前任务没有可运行的用例');
  task.last_run_id = run.id;
  return { id: run.id, status: 'Queued' };
}

export function advanceTasks(db, now = Date.now(), fixtures = {}) {
  let changed = false;
  for (const task of initSchedules(db, fixtures)) {
    if (!task.enabled || !task.next_due || Date.parse(task.next_due) > now) continue;
    const previous = Date.parse(task.next_due);
    task.enabled = task.rule.kind !== 'at';
    task.next_due = task.enabled ? nextDue(task.rule, now, previous) : null;
    changed = true;
    const busy = task.overlap === 'skip' && db.runs?.some((run) => run.task_id === task.id && ['Running', 'Queued'].includes(run.status));
    if (!busy) launch({ db, fixtures }, task);
  }
  return changed;
}

function save(ctx, tasks, current) {
  const { body, db, fixtures } = ctx;
  const library = initCases(db, fixtures);
  const now = Date.now();
  const task = current ? { ...current } : { id: 'task-' + crypto.randomUUID(), case_ids: [], kind: 'replay', engine: 'playwright',
    headed: null, enabled: true, overlap: 'skip', revision: 0, last_run_id: null, created_at: new Date(now).toISOString() };
  for (const field of ['title', 'case_ids', 'kind', 'engine', 'headed', 'enabled', 'overlap', 'rule', 'project_id']) {
    if (Object.hasOwn(body, field)) task[field] = body[field];
  }
  if (current) {
    check(task.project_id === current.project_id, '不能更换已有任务的项目');
    if (body.expected_revision != null && body.expected_revision !== current.revision) return Response.json({ detail: '任务已变更，请重新打开后重试' }, { status: 409 });
  }
  validateTask(task, new Set(library.projects.map((item) => item.id)));
  if (Object.hasOwn(body, 'case_ids') || body.enabled === true) {
    const available = new Set(library.items.filter((row) => row.project_id === task.project_id && row.replayable && row.lifecycle !== 'archived').map((row) => row.case_id));
    check(task.case_ids.every((id) => available.has(id)), '所选用例已删除或不可回放');
  }
  task.title = task.title.trim();
  task.case_ids = [...new Set(task.case_ids)];
  if (!current || current.enabled !== task.enabled || JSON.stringify(task.rule) !== JSON.stringify(current.rule)) {
    task.next_due = task.enabled ? nextDue(task.rule, now) : null;
  }
  task.updated_at = new Date(now).toISOString();
  task.revision++;
  if (current) tasks[tasks.indexOf(current)] = task;
  else tasks.push(task);
  return view(task, db);
}

export function schedulesApi(ctx) {
  const { path, method = 'GET', params = new URLSearchParams(), db, fixtures } = ctx;
  const match = /^\/tasks(?:\/([^/]+)(\/run)?)?$/.exec(path);
  if (!match) return;
  try {
    const tasks = initSchedules(db, fixtures);
    const [, id, runAction] = match;
    const task = tasks.find((entry) => entry.id === id);
    if (id && !task) return Response.json({ detail: '定时任务不存在' }, { status: 404 });
    if (runAction && method === 'POST') return launch(ctx, task);
    if (runAction) return;
    if (method === 'GET') {
      if (task) return view(task, db);
      const projects = params.get('projects')?.split(',');
      const items = tasks.filter((row) => !projects || projects.includes(row.project_id)).map((row) => view(row, db));
      items.sort((a, b) => Number(b.enabled) - Number(a.enabled) || String(a.next_due || '').localeCompare(String(b.next_due || '')));
      const identity = workspaceIdentity(ctx);
      if (params.get('paged') === '1') {
        const offset = Math.max(0, Number(params.get('offset')) || 0);
        const limit = Number(params.get('limit')) || 50;
        return { items: params.get('all') === '1' ? items : items.slice(offset, offset + limit), total: items.length, workspace_identity: identity };
      }
      return items.map((item) => ({ ...item, workspace_identity: identity }));
    }
    if ((method === 'POST' && !id) || (method === 'PATCH' && task)) return save(ctx, tasks, task);
    if (method === 'DELETE' && task) {
      const conflict = workspaceConflict(ctx);
      if (conflict) return conflict;
      tasks.splice(tasks.indexOf(task), 1);
      return { deleted: id };
    }
  } catch (error) {
    return Response.json({ detail: error.message }, { status: 400 });
  }
}
