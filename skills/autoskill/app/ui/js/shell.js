// The frame around every page: navigation, and the scope bar that says what the numbers below cover.
import { h, icon, replace, api, attempt, route, scope, setScope, isDefaultScope, rangeText, href, RANGES, DEFAULT_RANGE, ENGINE } from './core.js';
import { menuButton, menuItem, preferredEngine, setEngine, preferredHeaded, setHeaded } from './ui.js';
import { preferenceDataId, setTheme } from './preferences.js';
import { updateLiveView } from './live-refresh.js';

const NAV = [
  ['dashboard', '总览', 'dashboard'],
  ['cases', '测试用例', 'cases'],
  ['runs', '执行记录', 'runs'],
  ['schedules', '定时任务', 'clock'],
  ['data', '数据管理', 'folder'],
];
const PAGE_LABELS = {
  ...Object.fromEntries(NAV),
  workflows: '需求与工作流',
  'project-materials': '项目资料',
  'native-assets': '原生资产',
  conversations: '多轮对话',
  'load-tests': '压测',
  diagnostics: '运行诊断',
  'case-plans': '测试用例表',
};
export const state = { projects: [], allProjects: [], status: null, active: 0, online: true };

let frameVersion = 0;
let projectRead = null;
let projectsLoaded = false;
let runtimeRead = null;

function readProjects() {
  const request = api('/projects').then((projects) => {
    if (projectRead === request) {
      state.allProjects = projects.filter((p) => !p.broken);
      state.projects = state.allProjects.filter((p) => p.cases?.total);
      projectsLoaded = true;
      renderScope();
    }
    return projects;
  }).finally(() => { if (projectRead === request) projectRead = null; });
  projectsLoaded = false;
  projectRead = request;
  return request;
}

// Share an in-flight read. An explicit workspace refresh starts a newer snapshot.
export function projectIndex() { return projectRead ?? readProjects(); }
export function caseProjects() {
  if (projectsLoaded) return Promise.resolve(state.projects);
  return projectIndex().then(() => projectsLoaded ? state.projects : caseProjects());
}

export async function refreshShell() {
  const version = ++frameVersion;
  const projects = readProjects();
  runtimeRead ??= api('/status').finally(() => { runtimeRead = null; });
  const reads = await Promise.allSettled([
    projects,
    runtimeRead.then((status) => { if (version === frameVersion) { state.status = status; renderSide(); } }),
    api('/runs?limit=1&status=active').then((active) => { if (version === frameVersion) { state.active = active.total; renderSide(); } }),
  ]);
  if (version === frameVersion) {
    state.online = reads.every((item) => item.status === 'fulfilled');
    renderSide();
    renderScope();
  }
}

function theme() { return document.documentElement.dataset.theme || 'light'; }
export function applyTheme(name) {
  document.documentElement.dataset.theme = name;
}

function closeNavigation(restoreFocus = false) {
  const side = document.getElementById('side');
  side.dataset.navOpen = 'false';
  const toggle = side.querySelector('.nav-toggle');
  toggle?.setAttribute('aria-expanded', 'false');
  if (restoreFocus && toggle?.getClientRects().length) toggle.focus({ preventScroll: true });
}

function navigationLink([page, label, glyph]) {
  return h('a', { href: href(page), title: label, 'aria-label': label, dataset: { page },
    onClick: () => closeNavigation(true) }, icon(glyph), h('span', { class: 'label' }, label),
  page === 'runs' && h('span', { class: 'count', title: '正在执行', hidden: true }));
}

function buildSide(side) {
  const toggle = h('button', { class: 'nav-toggle btn', 'aria-expanded': 'false', 'aria-controls': 'side-panel',
    onClick: () => {
      const open = side.dataset.navOpen !== 'true';
      side.dataset.navOpen = String(open);
      toggle.setAttribute('aria-expanded', String(open));
    } }, '功能导航', icon('chevron'));
  replace(side,
    h('div', { class: 'side-heading' },
      h('a', { class: 'brand', href: href('dashboard'), 'aria-label': 'autoSkill 总览', onClick: () => closeNavigation(true) },
        h('span', { class: 'brand-mark' }, icon('spark')),
        h('span', { class: 'brand-name' }, 'autoSkill')),
      toggle, h('span', { class: 'nav-location', 'aria-live': 'polite' })),
    h('div', { class: 'side-panel', id: 'side-panel' },
      h('nav', { class: 'nav', 'aria-label': '主导航' }, NAV.map(navigationLink)),
      h('div', { class: 'side-foot' },
        h('div', { class: 'line service-status', title: '连不上本机服务', hidden: true }, h('i', { class: 'dot off' }), h('span', { class: 'text' }, '服务已断开')),
        h('button', { class: 'theme-toggle', title: '切换深浅色', onClick: async (event) => {
          await attempt(() => setTheme(theme() === 'dark' ? 'light' : 'dark'), event.currentTarget);
        } }))));
  side.dataset.navOpen = 'false';
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && side.dataset.navOpen === 'true') {
      event.preventDefault();
      closeNavigation(true);
    }
  });
  document.addEventListener('pointerdown', (event) => {
    if (side.dataset.navOpen === 'true' && !side.contains(event.target)) closeNavigation();
  }, true);
}

export function renderSide() {
  const side = document.getElementById('side');
  if (!side.childElementCount) buildSide(side);
  const { page } = route();
  const current = ['native-runs', 'load-tests'].includes(page) ? 'runs' : page;
  for (const link of side.querySelectorAll('nav a')) {
    link.href = href(link.dataset.page);
    if (link.dataset.page === current) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  side.querySelector('.brand').href = href('dashboard');
  side.querySelector('.nav-location').textContent = '当前位置：' + (PAGE_LABELS[page === 'load-tests' ? page : current] || '总览');
  const count = side.querySelector('.count');
  count.hidden = !state.active;
  count.textContent = String(state.active);
  side.querySelector('.service-status').hidden = state.online;
  const themeButton = side.querySelector('.theme-toggle');
  const label = theme() === 'dark' ? '浅色' : '深色';
  if (themeButton.getAttribute('aria-label') !== label) {
    themeButton.setAttribute('aria-label', label);
    replace(themeButton, icon(theme() === 'dark' ? 'sun' : 'moon'), h('span', { class: 'text' }, label));
  }
}

function projectText(s) {
  if (!s.projects.length) return '全部项目';
  const names = s.projects.map((id) => (state.allProjects.find((p) => p.id === id) || { name: id }).name);
  return names.length === 1 ? names[0] : `${names[0]} 等 ${names.length} 个项目`;
}

/** One place for how cases are replayed: engine, visible window, which browser. */
export function runSettings() {
  const trigger = h('button', { class: 'btn', title: '回放设置：引擎、浏览器窗口、使用哪个浏览器' }, icon('sliders'), '回放设置');
  return menuButton(trigger, (menu) => {
    const browser = state.status?.browser ?? {};
    const engines = h('div', { class: 'seg', role: 'group', 'aria-label': '页面用例的回放引擎' },
      ['playwright', 'selenium'].map((engine) => h('button', {
        'aria-pressed': String(preferredEngine() === engine),
        onClick: async (event) => {
          const button = event.currentTarget;
          if (await attempt(() => setEngine(engine), button)) {
            for (const other of engines.children) other.setAttribute('aria-pressed', String(other === button));
          }
        },
      }, ENGINE[engine])));
    const toggle = (label, on, onChange) => {
      const button = h('button', { class: 'switch', role: 'switch', 'aria-checked': String(on), 'aria-label': label });
      button.addEventListener('click', async () => {
        const next = button.getAttribute('aria-checked') !== 'true';
        if (await attempt(() => onChange(next), button)) button.setAttribute('aria-checked', String(next));
      });
      return button;
    };
    const row = (title, note, control) => h('div', { class: 'setting-row' },
      h('div', { class: 'grow' }, h('div', { class: 'setting-title' }, title), note && h('div', { class: 'setting-note' }, note)), control);
    menu.classList.add('settings');
    menu.append(
      row('回放引擎', '只影响页面用例；接口用例始终走 HTTP', engines),
      h('div', { class: 'menu-sep' }),
      row('显示浏览器窗口', null, toggle('显示浏览器窗口', preferredHeaded(), (on) => setHeaded(on))),
      row('使用本机 Chrome', browser.system ? '启动不了时改用随包浏览器；Selenium 始终用随包浏览器' : '没有找到本机 Chrome，正在使用随包浏览器',
        toggle('使用本机 Chrome', browser.mode !== 'bundled', async (on) => {
          const mode = on ? 'system' : 'bundled';
          const saved = await api('/settings', { method: 'PUT', body: { browser: mode, expected_data_id: preferenceDataId() } });
          state.status = { ...state.status, browser: saved.browser };
          return saved;
        })));
  }, true);
}

function environmentPicker(s) {
  if (s.projects.length !== 1) return null;
  const project = state.allProjects.find((item) => item.id === s.projects[0]);
  if (!project?.environments?.length) return null;
  const button = h('button', { class: 'btn', 'aria-label': '切换运行环境' }, icon('globe'), project.environment?.name || '选择环境', icon('chevron'));
  return menuButton(button, (menu, close) => {
    for (const item of project.environments) {
      menu.append(menuItem(item.name + (item.is_default ? '（默认）' : ''), {
        checked: project.environment?.id === item.id,
        meta: item.base_url || '',
        onPick: async () => {
          close();
          const saved = await attempt(() => api(`/projects/${encodeURIComponent(project.id)}/environments`, { method: 'PUT',
            body: { environments: project.environments, active_environment_id: item.id } }), button);
          if (saved) await refreshShell();
        },
      }));
    }
  });
}

function replaceScope(bar, ...nodes) {
  updateLiveView(bar, h('div', {}, nodes));
}

function pageScopeNote(page, id) {
  if (id) return ['runs', 'native-runs'].includes(page)
    ? '仅显示本次执行；返回列表可继续筛选。' : '使用当前页面选择的项目与内容。';
  const notes = {
    data: '设置保存在当前数据目录中',
    diagnostics: '检查当前机器的运行环境',
    // These pages render their own project picker, so the bar carries only the replay settings.
    'project-materials': '',
    'native-assets': '',
    conversations: '',
    'native-runs': '查看原生执行记录',
  };
  return notes[page];
}

export function renderScope() {
  const s = scope();
  const bar = document.getElementById('scope');
  const { page, id } = route();
  // The case sheet and load reports render their own controls.
  bar.hidden = page === 'case-plans' || (page === 'load-tests' && !!id);
  if (bar.hidden) { replaceScope(bar); return; }
  // Background state changes must not dismiss an open scope picker or its date draft.
  if (bar.querySelector('.menu')) return;
  const note = pageScopeNote(page, id);
  if (note !== undefined) {
    replaceScope(bar, note && h('span', { class: 'scope-text' }, note), h('span', { class: 'grow' }), runSettings());
    return;
  }
  const timeless = ['schedules', 'workflows', 'case-plans'].includes(page);
  const projects = ['workflows', 'case-plans'].includes(page) ? state.allProjects : state.projects;

  const projectButton = h('button', { class: 'btn', 'aria-label': '筛选项目' }, icon('folder'), projectText(s), icon('chevron'));
  const projectPicker = menuButton(projectButton, (menu, close) => {
    menu.append(menuItem('全部项目', { checked: !s.projects.length, meta: projects.length, onPick: () => { close(); setScope({ projects: [] }); } }));
    if (projects.length) menu.append(h('div', { class: 'menu-sep' }));
    for (const project of projects) {
      const on = s.projects.includes(project.id);
      menu.append(menuItem(project.name, {
        checked: on,
        onPick: () => { close(); setScope({ projects: on ? s.projects.filter((id) => id !== project.id) : [...s.projects, project.id] }); },
      }));
    }
    if (!projects.length) menu.append(h('div', { class: 'menu-title' }, '还没有项目'));
  });

  const rangeButton = h('button', { class: 'btn', 'aria-label': '筛选时间范围' },
    icon('clock'), timeless ? '不限时间' : rangeText(s), icon('chevron'));
  const rangePicker = menuButton(rangeButton, (menu, close) => {
    for (const range of RANGES) {
      menu.append(menuItem(range.label, { checked: s.range === range.id, onPick: () => { close(); setScope({ range: range.id, from: '', to: '' }); } }));
    }
    const today = new Date().toISOString().slice(0, 10);
    const from = h('input', { class: 'input', type: 'date', value: s.from || today, max: today, 'aria-label': '开始日期' });
    const to = h('input', { class: 'input', type: 'date', value: s.to || today, max: today, 'aria-label': '结束日期' });
    menu.append(h('div', { class: 'menu-sep' }), h('div', { class: 'menu-foot' },
      h('div', { class: 'menu-title', style: 'padding:0' }, '自定义范围'),
      h('div', { class: 'row' }, from, h('span', { class: 'muted' }, '至'), to),
      h('button', {
        class: 'btn primary sm',
        onClick: () => { if (from.value && to.value && from.value <= to.value) { close(); setScope({ range: 'custom', from: from.value, to: to.value }); } },
      }, '应用')));
  });

  replaceScope(bar,
    h('span', { class: 'scope-label' }, '当前范围'),
    projectPicker, environmentPicker(s), !timeless && rangePicker,
    (timeless ? s.projects.length > 0 : !isDefaultScope(s)) && h('button', {
      class: 'btn ghost sm', title: timeless ? '回到全部项目' : '回到「全部项目 · 近 7 天」',
      onClick: () => setScope(timeless ? { projects: [] } : { projects: [], range: DEFAULT_RANGE, from: '', to: '' }),
    }, icon('reset'), '重置'),
    h('span', { class: 'grow' }),
    page !== 'load-tests' && runSettings());
}
