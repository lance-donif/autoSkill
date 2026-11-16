// The frame around every page: navigation, and the scope bar that says what the numbers below cover.
import { h, icon, replace, api, attempt, route, scope, setScope, isDefaultScope, rangeText, href, RANGES, DEFAULT_RANGE, ENGINE } from './core.js';
import { menuButton, menuItem, preferredEngine, setEngine, preferredHeaded, setHeaded } from './ui.js';
import { preferenceDataId, setTheme } from './preferences.js';
import { updateLiveView } from './live-refresh.js';

const PRIMARY = [
  ['case-plans', '测试用例表', 'table'],
  ['runs', '执行记录', 'runs'],
  ['cases', '可复用用例', 'cases'],
];
const OVERVIEW = ['dashboard', '总览', 'dashboard'];
const GROUPS = [
  ['prepare', '测试资料', [
    ['workflows', '需求与工作流', 'link'],
    ['project-materials', '项目资料', 'folder'],
    ['native-assets', '原生资产', 'api'],
    ['conversations', '多轮对话', 'api'],
  ]],
  ['execute', '执行工具', [
    ['load-tests', '压测', 'sliders'],
    ['schedules', '定时任务', 'clock'],
  ]],
  ['settings', '设置与维护', [
    ['data', '数据管理', 'folder'],
    ['diagnostics', '运行诊断', 'sliders'],
  ]],
];
const NAV = [...PRIMARY, OVERVIEW, ...GROUPS.flatMap(([, , entries]) => entries)];
let lastSidePage = null;
export const state = { projects: [], allProjects: [], status: null, active: 0, online: true };

export async function refreshShell(onProjects) {
  const reads = await Promise.allSettled([
    api('/projects').then(async (projects) => {
      state.allProjects = projects.filter((p) => !p.broken);
      state.projects = state.allProjects.filter((p) => p.cases?.total);
      renderScope();
      await onProjects?.();
    }),
    api('/status').then((status) => { state.status = status; renderSide(); }),
    api('/runs?limit=1&status=active').then((active) => { state.active = active.total; renderSide(); }),
  ]);
  state.online = reads.every((read) => read.status === 'fulfilled');
  renderSide();
  renderScope();
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
        h('span', {}, h('div', { class: 'brand-name' }, 'autoSkill'), h('div', { class: 'brand-tag' }, '用例表 · 执行 · 复用'))),
      toggle, h('span', { class: 'nav-location', 'aria-live': 'polite' })),
    h('div', { class: 'side-panel', id: 'side-panel' },
      h('nav', { class: 'nav', 'aria-label': '主导航' },
        h('div', { class: 'nav-primary' }, PRIMARY.map(navigationLink)),
        h('div', { class: 'nav-overview' }, navigationLink(OVERVIEW)),
        GROUPS.map(([key, label, entries]) => h('details', { class: 'nav-group', dataset: { group: key } },
          h('summary', {}, label, icon('chevron')), h('div', { class: 'nav-children' }, entries.map(navigationLink))))),
      h('div', { class: 'side-foot' },
        h('div', { class: 'line service-status' }, h('i', { class: 'dot' }), h('span', { class: 'text' })),
        h('div', { class: 'line runtime-status', title: '离线运行：不联网、不更新、不上报' }, icon('lock'), h('span', { class: 'text' })),
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
  const current = route().page === 'native-runs' ? 'runs' : route().page;
  for (const link of side.querySelectorAll('nav a')) {
    link.href = href(link.dataset.page);
    if (link.dataset.page === current) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  // Update the existing frame so live counts never replace focused links or disclosures.
  if (current !== lastSidePage) {
    const group = side.querySelector('a[aria-current="page"]')?.closest('details');
    if (group) group.open = true;
    lastSidePage = current;
  }
  for (const group of side.querySelectorAll('.nav-group')) group.classList.toggle('current-group', !!group.querySelector('[aria-current="page"]'));
  side.querySelector('.brand').href = href('dashboard');
  side.querySelector('.nav-location').textContent = '当前位置：' + (NAV.find(([page]) => page === current)?.[1] || '总览');
  const count = side.querySelector('.count');
  count.hidden = !state.active;
  count.textContent = String(state.active);
  const service = side.querySelector('.service-status');
  service.title = state.online ? '服务只监听本机 127.0.0.1' : '连不上本机服务';
  service.querySelector('.dot').className = 'dot ' + (state.online ? 'ok' : 'off');
  service.querySelector('.text').textContent = state.online ? '本机服务运行中' : '服务已断开';
  const runtime = state.status?.runtime;
  const runtimeLine = side.querySelector('.runtime-status');
  runtimeLine.hidden = !runtime;
  runtimeLine.querySelector('.text').textContent = runtime?.bundled ? '随包运行时 · 离线' : '开发环境运行时';
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
      h('div', { class: 'grow' }, h('div', { class: 'setting-title' }, title), h('div', { class: 'setting-note' }, note)), control);
    menu.classList.add('settings');
    menu.append(
      row('回放引擎', '只影响页面用例；接口用例始终走 HTTP', engines),
      h('div', { class: 'menu-sep' }),
      row('显示浏览器窗口', '关闭后在后台运行', toggle('显示浏览器窗口', preferredHeaded(), (on) => setHeaded(on))),
      row('使用本机 Chrome', browser.system ? '启动不了时改用随包浏览器；Selenium 始终用随包浏览器' : '没有找到本机 Chrome，正在使用随包浏览器',
        toggle('使用本机 Chrome', browser.mode !== 'bundled', async (on) => {
          const mode = on ? 'system' : 'bundled';
          const saved = await api('/settings', { method: 'PUT', body: { browser: mode, expected_data_id: preferenceDataId() } });
          state.status = { ...state.status, browser: saved.browser };
          return saved;
        })));
  }, true);
}

function replaceScope(bar, ...nodes) {
  updateLiveView(bar, h('div', {}, nodes));
}

function pageScopeNote(page, id) {
  if (id) return ['runs', 'native-runs', 'load-tests'].includes(page)
    ? '仅显示本次执行；返回列表可继续筛选。' : '使用当前页面选择的项目与内容。';
  const notes = {
    data: '设置保存在当前数据目录中',
    diagnostics: '检查当前机器的运行环境',
    'project-materials': '在页面中选择资料所属项目',
    'native-assets': '在页面中选择资产所属项目',
    conversations: '在页面中选择对话所属项目',
    'native-runs': '查看原生执行记录',
  };
  return notes[page];
}

export function renderScope() {
  const s = scope();
  const bar = document.getElementById('scope');
  const { page, id } = route();
  // The case sheet owns its single project/session selector and its secondary settings.
  bar.hidden = page === 'case-plans';
  if (bar.hidden) { replaceScope(bar); return; }
  // Background state changes must not dismiss an open scope picker or its date draft.
  if (bar.querySelector('.menu')) return;
  const note = pageScopeNote(page, id);
  if (note) {
    replaceScope(bar, h('span', { class: 'scope-label' }, NAV.find(([key]) => key === page)?.[1] || '执行记录'),
      h('span', { class: 'scope-text' }, note), h('span', { class: 'grow' }), runSettings());
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
        checked: on, meta: project.category || null,
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
    projectPicker, !timeless && rangePicker,
    h('span', { class: 'scope-text', 'aria-live': 'polite' },
      h('b', {}, projectText(s)), ' · ', h('b', {}, timeless ? '不限时间' : rangeText(s))),
    (timeless ? s.projects.length > 0 : !isDefaultScope(s)) && h('button', {
      class: 'btn ghost sm', title: timeless ? '回到全部项目' : '回到「全部项目 · 近 7 天」',
      onClick: () => setScope(timeless ? { projects: [] } : { projects: [], range: DEFAULT_RANGE, from: '', to: '' }),
    }, icon('reset'), '重置'),
    h('span', { class: 'grow' }),
    runSettings());
}
