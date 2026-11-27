// Schedules: run chosen cases at fixed times. Create, edit, pause, delete, run now.
import { projectIndex } from '../shell.js';
import { ensurePreferences } from '../preferences.js';
import { h, icon, replace, api, deleteSelection, scope, scopeQuery, go, href, route, attempt, toast, fmtTime, fmtUntil, ENGINE, refresh } from '../core.js';
import { empty, errorState, drawer, closeOverlay, confirm, preferredHeaded } from '../ui.js';
import { targetEditor } from '../schedule-target.js';
import { activeState, stateResults } from '../status-view.js';

const DAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';

function calendarZone(rule) {
  return ['daily', 'weekly'].includes(rule.kind) ? rule.timezone || 'Asia/Shanghai' : null;
}

export async function render(view) {
  let tasks, projects;
  try { [tasks, projects] = await Promise.all([api('/tasks' + scopeQuery()), projectIndex()]); } catch (error) {
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  const create = h('button', { class: 'btn primary', disabled: !projects.length, onClick: () => editor(null, refresh, projects) }, icon('plus'), '新建定时任务');
  let body;
  if (!tasks.length) {
    body = empty({ glyph: 'clock', title: scope().projects.length ? '所选项目还没有定时任务' : '还没有定时任务',
      text: '安排用例回放、原生资产或 AI 维护工作流。自动回放和原生执行需要本机队列运行，AI 维护还需要在线宿主。',
      actions: projects.length ? h('button', { class: 'btn primary', onClick: () => editor(null, refresh, projects) }, icon('plus'), '新建定时任务')
        : h('a', { class: 'btn', href: href('cases') }, '先创建一个项目') });
  } else {
    body = h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['启用', '任务', '执行规则', '下次执行', '上次结果', ''].map((t, i) => h('th', { class: i === 5 ? 'actions' : '' }, t)))),
      h('tbody', {}, tasks.map((task) => {
        const zone = calendarZone(task.rule);
        const toggle = h('button', { class: 'switch', role: 'switch', 'aria-checked': String(task.enabled), 'aria-label': `${task.enabled ? '暂停' : '启用'}「${task.title}」` });
        toggle.addEventListener('click', async (event) => {
          event.stopPropagation();
          const done = await attempt(() => api('/tasks/' + task.id, { method: 'PATCH', body: { enabled: !task.enabled, expected_revision: task.revision } }));
          if (done) { toast(done.enabled ? `已启用，下次执行：${fmtUntil(done.next_due)}` : '已暂停'); refresh(); }
        });
        const now = h('button', { class: 'btn sm', title: '不等到计划时间，现在就执行一次' }, icon('play'), '立即运行');
        now.addEventListener('click', async (event) => {
          event.stopPropagation();
          const run = await attempt(() => api(`/tasks/${task.id}/run`, { method: 'POST', body: {} }), now);
          if (!run) return;
          if (run.status === 'awaiting_host') {
            toast('维护任务已入队，等待宿主接续；送达不表示维护完成');
            refresh();
          } else go((task.kind === 'native' ? 'native-runs/' : 'runs/') + run.id);
        });
        const remove = h('button', { class: 'btn sm icon danger', 'aria-label': `删除「${task.title}」`, title: '删除' }, icon('trash'));
        remove.addEventListener('click', async (event) => {
          event.stopPropagation();
          if (!(await confirm({ title: `删除「${task.title}」？`, text: '任务会被删除，已经产生的执行记录会保留。', action: '删除', danger: true }))) return;
          if (await attempt(() => deleteSelection('/tasks/' + task.id, [task]))) { toast('已删除'); refresh(); }
        });
        return h('tr', { class: 'click', onClick: () => editor(task, refresh, projects) },
          h('td', { style: 'width:56px' }, toggle),
          h('td', {}, h('div', { class: 'title' }, task.title),
            h('div', { class: 'sub' }, taskDescription(task))),
          h('td', {}, task.rule_text, zone && zone !== ZONE && h('div', { class: 'sub' }, zone)),
          h('td', { class: 'sub num' }, task.enabled ? [fmtUntil(task.next_due), h('div', { class: 'muted' }, fmtTime(task.next_due))] : '已暂停'),
          h('td', {}, task.last_status
            ? h('a', { href: href('runs/' + task.last_run_id), onClick: (event) => event.stopPropagation(), class: 'stack' },
              activeState({ state: task.last_run_state }), stateResults({ state: task.last_run_state }), h('span', { class: 'sub num' }, fmtTime(task.last_finished)))
            : h('span', { class: 'sub' }, '未执行')),
          h('td', { class: 'actions' }, h('div', { class: 'row', style: 'justify-content:flex-end' }, now, remove)));
      })));
  }
  replace(view,
    h('div', { class: 'page-head' },
      h('div', { class: 'grow' }, h('h1', {}, '定时任务'),
        tasks.length > 0 && h('p', { class: 'sub' }, `${tasks.filter((t) => t.enabled).length} 个启用 · ${tasks.filter((t) => !t.enabled).length} 个暂停`)),
      tasks.length > 0 && create),
    h('section', { class: 'card' }, body));
  if (route().params.get('new') && projects.length) { editor(null, refresh, projects); }
}

function taskDescription(task) {
  if (task.kind === 'native') return `${task.project_name} · 原生资产 ${task.asset_id}`;
  if (task.kind === 'maintenance') return `${task.project_name} · AI 维护 ${task.workflow_id} · 等待在线宿主接续`;
  const headed = task.headed ?? task.effective_headed ?? preferredHeaded();
  const context = [task.environment_name, task.account_label].filter(Boolean);
  return [task.project_name, ...context, task.case_ids.length ? task.case_ids.length + ' 条用例' : '全部用例',
    ENGINE[task.engine], headed ? '显示浏览器窗口' : '隐藏浏览器窗口'].join(' · ');
}

function projectEnvs(project) {
  return Array.isArray(project?.environments) ? project.environments : [];
}

function envAccounts(project, env) {
  if (!env) return [];
  if (Array.isArray(env.accounts)) return env.accounts;
  const all = Array.isArray(project?.accounts) ? project.accounts : [];
  if (Array.isArray(env.account_ids) && env.account_ids.length) {
    return env.account_ids.map((id) => all.find((acc) => acc.id === id)).filter(Boolean);
  }
  return all;
}

function scheduleContextEditor(task, projectSelect, projects) {
  const envSelect = h('select', { class: 'select', id: 'tk-env', 'aria-label': '运行环境' });
  const envField = h('div', { class: 'field', hidden: true }, h('label', { for: 'tk-env' }, '运行环境'), envSelect);
  const accSelect = h('select', { class: 'select', id: 'tk-account', 'aria-label': '运行账号' });
  const accField = h('div', { class: 'field', hidden: true }, h('label', { for: 'tk-account' }, '运行账号'), accSelect);

  const selectedProject = () => projects.find((item) => item.id === projectSelect.value) || null;
  const selectedEnv = () => projectEnvs(selectedProject()).find((item) => item.id === envSelect.value) || null;

  function paintAccounts(preferredAccountId) {
    const proj = selectedProject();
    const env = selectedEnv();
    if (!env?.login_required) {
      accField.hidden = true;
      replace(accSelect);
      return;
    }
    accField.hidden = false;
    const accounts = envAccounts(proj, env);
    if (!accounts.length) {
      replace(accSelect, h('option', { value: '', disabled: true, selected: true }, '未配置账号'));
      return;
    }
    const wanted = preferredAccountId || env.default_account_id || accounts[0].id;
    replace(accSelect, accounts.map((acc) => h('option', { value: acc.id, selected: acc.id === wanted }, acc.label || acc.username || acc.id)));
  }

  function paintEnvs(initial = false) {
    const proj = selectedProject();
    const envs = projectEnvs(proj);
    if (!envs.length) {
      envField.hidden = true;
      accField.hidden = true;
      replace(envSelect);
      replace(accSelect);
      return;
    }
    envField.hidden = false;
    const fallback = envs.find((item) => item.is_default)?.id || proj.active_environment_id || envs[0].id;
    const wantedEnv = initial && task?.environment_id && envs.some((item) => item.id === task.environment_id) ? task.environment_id : fallback;
    replace(envSelect, envs.map((env) => h('option', { value: env.id, selected: env.id === wantedEnv }, env.name + (env.is_default ? '（默认）' : ''))));
    paintAccounts(initial && task?.environment_id === wantedEnv ? task.account_id : '');
  }

  envSelect.addEventListener('change', () => paintAccounts(''));
  paintEnvs(true);

  return {
    node: h('div', { class: 'stack' }, envField, accField),
    onProjectChange: () => paintEnvs(false),
    values() {
      const envs = projectEnvs(selectedProject());
      if (!envs.length && !task?.environment_id && !task?.account_id) return {};
      const env = selectedEnv();
      return {
        environment_id: env?.id || '',
        account_id: env?.login_required ? (accSelect.value || '') : '',
      };
    },
  };
}

async function editor(task, done, projects) {
  if (!await attempt(ensurePreferences)) return;
  if (!projects.length) { toast('先创建项目，再安排定时任务', { bad: true }); return; }
  const rule = task ? task.rule : { kind: 'daily', time: '09:00' };
  const picked = new Set(task ? task.case_ids : []);
  let cases = [];

  const title = h('input', { class: 'input', id: 'tk-title', value: task ? task.title : '', placeholder: '例如：每日回归', autofocus: !task });
  const project = h('select', { class: 'select', id: 'tk-project', disabled: !!task },
    projects.map((p) => h('option', { value: p.id, selected: task ? task.project_id === p.id : scope().projects[0] === p.id }, p.name)));
  const engine = h('select', { class: 'select', id: 'tk-engine' },
    ['playwright', 'selenium'].map((e) => h('option', { value: e, selected: (task ? task.engine : 'playwright') === e }, ENGINE[e])));
  const headed = h('input', { class: 'check', type: 'checkbox', id: 'tk-headed', checked: task ? (task.headed ?? task.effective_headed ?? preferredHeaded()) : preferredHeaded() });
  const kind = h('div', { class: 'seg', role: 'group', 'aria-label': '执行规则' });
  const detail = h('div', { class: 'stack', style: 'gap:12px' });
  const caseBox = h('div', { class: 'case-pick' });
  const allCases = h('input', { class: 'check', type: 'checkbox', checked: !picked.size, id: 'tk-all' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const current = { kind: rule.kind, time: rule.time || '09:00', days: new Set(rule.days || [0, 1, 2, 3, 4]), minutes: rule.minutes || 60,
    timezone: (task && calendarZone(rule)) || ZONE,
    at: rule.at ? new Date(rule.at) : new Date(Date.now() + 3600000) };
  const initialRule = task ? JSON.stringify(scheduleRule(current)) : null;
  const contextEditor = scheduleContextEditor(task, project, projects);
  const replayFields = h('div', { class: 'stack' },
    contextEditor.node,
    h('div', { class: 'field' }, h('span', { class: 'label' }, '执行哪些用例'),
      h('label', { class: 'row', for: 'tk-all' }, allCases, '全部用例（以后新增的用例也会包含）'), caseBox,
      h('span', { class: 'hint' }, '前置用例（如登录）会自动先执行，不需要单独选。')),
    h('div', { class: 'field' }, h('label', { for: 'tk-engine' }, '回放引擎'), engine),
    h('div', { class: 'field' }, h('label', { class: 'row', for: 'tk-headed' }, headed, '显示浏览器窗口'),
      h('span', { class: 'hint' }, '此任务单独保存这个选择，立即运行、定时执行和重启后都沿用。')));
  const target = targetEditor(task, project, (value) => { replayFields.hidden = value !== 'replay'; });
  replayFields.hidden = target.kind() !== 'replay';

  function paintRule() {
    replace(kind, [['daily', '每天'], ['weekly', '每周'], ['interval', '固定间隔'], ['at', '仅一次']].map(([value, label]) => h('button', {
      'aria-pressed': String(current.kind === value), onClick: () => { current.kind = value; paintRule(); },
    }, label)));
    const time = h('input', { class: 'input', type: 'time', value: current.time, style: 'width:140px', 'aria-label': '执行时间', onChange: (event) => { current.time = event.target.value; } });
    if (current.kind === 'daily') replace(detail, h('div', { class: 'row' }, '每天', time, '执行'));
    if (current.kind === 'weekly') {
      replace(detail, h('div', { class: 'row wrap' }, DAYS.map((label, index) => h('button', {
        class: 'btn sm' + (current.days.has(index) ? ' primary' : ''), 'aria-pressed': String(current.days.has(index)),
        onClick: () => { if (current.days.has(index)) current.days.delete(index); else current.days.add(index); paintRule(); },
      }, label))), h('div', { class: 'row' }, '在', time, '执行'));
    }
    if (current.kind === 'interval') {
      const options = [[15, '15 分钟'], [30, '30 分钟'], [60, '1 小时'], [120, '2 小时'], [360, '6 小时'], [720, '12 小时']];
      replace(detail, h('div', { class: 'row' }, '每隔', h('select', { class: 'select', style: 'width:160px', 'aria-label': '间隔', onChange: (event) => { current.minutes = Number(event.target.value); } },
        options.map(([value, label]) => h('option', { value, selected: current.minutes === value }, label))), '执行一次'));
    }
    if (current.kind === 'at') {
      const local = Number.isFinite(current.at.getTime())
        ? new Date(current.at.getTime() - current.at.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
      replace(detail, h('div', { class: 'row' }, '在', h('input', { class: 'input', type: 'datetime-local', value: local, style: 'width:230px', 'aria-label': '执行时间',
        onChange: (event) => { current.at = new Date(event.target.value); } }), '执行一次'));
    }
    if (current.kind !== 'interval') detail.append(h('span', { class: 'hint' }, `执行时区为 ${current.kind === 'at' ? ZONE : current.timezone}`));
  }
  function paintCases() {
    caseBox.hidden = allCases.checked;
    replace(caseBox, cases.map((item) => {
      const box = h('input', { class: 'check', type: 'checkbox', checked: picked.has(item.case_id) });
      box.addEventListener('change', () => { if (box.checked) picked.add(item.case_id); else picked.delete(item.case_id); });
      return h('label', { class: 'row' }, box, h('span', { class: 'grow ellipsis' }, item.title), h('span', { class: 'sub mono' }, item.case_id));
    }));
  }
  async function loadCases() {
    const data = await api('/cases?projects=' + encodeURIComponent(project.value));
    cases = data.items.filter((item) => item.replayable);
    paintCases();
  }
  project.addEventListener('change', () => { picked.clear(); contextEditor.onProjectChange(); loadCases(); });
  allCases.addEventListener('change', paintCases);

  function buildTaskBody() {
    const selected = target.read();
    const isReplay = selected.kind === 'replay';
    return {
      title: title.value, project_id: project.value, ...selected,
      engine: isReplay ? engine.value : 'native',
      case_ids: isReplay && !allCases.checked ? [...picked] : [],
      headed: headed.checked,
      ...(isReplay ? contextEditor.values() : {}),
      ...ruleChange(current, initialRule),
      ...(task ? { expected_revision: task.revision } : {}),
    };
  }

  const save = h('button', { class: 'btn primary' }, task ? '保存' : '创建任务');
  save.addEventListener('click', async () => {
    error.hidden = true;
    if (target.kind() === 'replay' && !allCases.checked && !picked.size) { error.textContent = '请至少选择一条用例，或勾选「全部用例」'; error.hidden = false; return; }
    try {
      const body = buildTaskBody();
      save.disabled = true;
      const saved = await api(task ? '/tasks/' + task.id : '/tasks', { method: task ? 'PATCH' : 'POST', body });
      closeOverlay();
      toast(`已保存，下次执行：${fmtUntil(saved.next_due)}`);
      if (route().params.get('new')) go('schedules', { new: null }); else done();
    } catch (problem) { error.textContent = problem.message; error.hidden = false; } finally { save.disabled = false; }
  });

  paintRule();
  drawer({
    title: task ? '编辑定时任务' : '新建定时任务',
    body: [
      h('div', { class: 'field' }, h('label', { for: 'tk-title' }, '任务名称'), title),
      h('div', { class: 'field' }, h('label', { for: 'tk-project' }, '项目'), project),
      target.node,
      h('div', { class: 'field' }, h('span', { class: 'label' }, '什么时候执行'), kind, detail),
      replayFields,
      error,
    ],
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save],
  });
  loadCases().catch((problem) => toast(problem.message, { bad: true }));
}

function scheduleRule(current) {
  return current.kind === 'daily' ? { kind: 'daily', time: current.time, timezone: current.timezone }
    : current.kind === 'weekly' ? { kind: 'weekly', time: current.time, days: [...current.days].sort(), timezone: current.timezone }
      : current.kind === 'interval' ? { kind: 'interval', minutes: current.minutes }
        : { kind: 'at', at: current.at.toJSON() };
}

function ruleChange(current, initialRule) {
  const rule = scheduleRule(current);
  if (rule.kind === 'at' && !rule.at) throw new Error('请选择有效的执行时间');
  return JSON.stringify(rule) === initialRule ? {} : { rule };
}
