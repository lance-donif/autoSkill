// The frame around every page: navigation, and the scope bar that says what the numbers below cover.
import { h, icon, replace, api, attempt, route, scope, setScope, isDefaultScope, rangeText, href, RANGES, DEFAULT_RANGE, ENGINE } from './core.js';
import { menuButton, menuItem, preferredEngine, setEngine, preferredHeaded, setHeaded } from './ui.js';

const NAV = [
  ['dashboard', '仪表盘', 'dashboard'],
  ['cases', '项目与用例', 'cases'],
  ['runs', '执行记录', 'runs'],
  ['schedules', '定时任务', 'clock'],
];
export const state = { projects: [], status: null, active: 0, online: true };

export async function refreshShell() {
  try {
    const [projects, status, active] = await Promise.all([
      api('/projects'), api('/status'), api('/runs?limit=1&status=active'),
    ]);
    state.projects = projects.filter((p) => !p.broken && p.cases && p.cases.total);  // a project without cases is not shown
    state.status = status;
    state.active = active.total;
    state.online = true;
  } catch {
    state.online = false;
  }
  renderSide();
  renderScope();
}

function theme() { return document.documentElement.dataset.theme || 'light'; }
export function applyTheme(name) {
  document.documentElement.dataset.theme = name;
  localStorage.setItem('autoskill.theme', name);
}

export function renderSide() {
  const current = route().page;
  const runtime = state.status?.runtime;
  replace(document.getElementById('side'),
    h('a', { class: 'brand', href: href('dashboard') },
      h('span', { class: 'brand-mark' }, icon('spark')),
      h('span', {}, h('div', { class: 'brand-name' }, 'autoSkill'), h('div', { class: 'brand-tag' }, '录一次，到处回放'))),
    h('nav', { class: 'nav', 'aria-label': '主导航' }, NAV.map(([page, label, glyph]) => h('a', {
      href: href(page), 'aria-current': current === page ? 'page' : null, title: label, 'aria-label': label,
    }, icon(glyph), h('span', { class: 'label' }, label),
    page === 'runs' && state.active > 0 && h('span', { class: 'count', title: '正在执行' }, String(state.active))))),
    h('div', { class: 'side-foot' },
      h('div', { class: 'line', title: state.online ? '服务只监听本机 127.0.0.1' : '连不上本机服务' },
        h('i', { class: 'dot ' + (state.online ? 'ok' : 'off') }),
        h('span', { class: 'text' }, state.online ? '本机服务运行中' : '服务已断开')),
      runtime && h('div', { class: 'line', title: '离线运行：不联网、不更新、不上报' },
        icon('lock'), h('span', { class: 'text' }, runtime.bundled ? '随包运行时 · 离线' : '开发环境运行时')),
      h('button', { 'aria-label': theme() === 'dark' ? '浅色' : '深色', title: '切换深浅色', onClick: () => { applyTheme(theme() === 'dark' ? 'light' : 'dark'); renderSide(); } },
        icon(theme() === 'dark' ? 'sun' : 'moon'), h('span', { class: 'text' }, theme() === 'dark' ? '浅色' : '深色'))));
}

function projectText(s) {
  if (!s.projects.length) return '全部项目';
  const names = s.projects.map((id) => (state.projects.find((p) => p.id === id) || { name: id }).name);
  return names.length === 1 ? names[0] : `${names[0]} 等 ${names.length} 个项目`;
}

/** One place for how cases are replayed: engine, visible window, which browser. */
function runSettings() {
  const trigger = h('button', { class: 'btn', title: '回放设置：引擎、浏览器窗口、使用哪个浏览器' }, icon('sliders'), '回放设置');
  return menuButton(trigger, (menu) => {
    const browser = state.status?.browser ?? {};
    const engines = h('div', { class: 'seg', role: 'group', 'aria-label': '页面用例的回放引擎' },
      ['playwright', 'selenium'].map((engine) => h('button', {
        'aria-pressed': String(preferredEngine() === engine),
        onClick: (event) => { setEngine(engine); for (const other of engines.children) other.setAttribute('aria-pressed', String(other === event.currentTarget)); },
      }, ENGINE[engine])));
    const toggle = (label, on, onChange) => {
      const button = h('button', { class: 'switch', role: 'switch', 'aria-checked': String(on), 'aria-label': label });
      button.addEventListener('click', async () => {
        const next = button.getAttribute('aria-checked') !== 'true';
        button.setAttribute('aria-checked', String(next));
        await onChange(next);
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
          const saved = await attempt(() => api('/settings', { method: 'PUT', body: { browser: mode } }));
          if (saved) state.status = { ...state.status, browser: saved.browser };
        })));
  }, true);
}

export function renderScope() {
  const s = scope();
  const bar = document.getElementById('scope');
  const { page } = route();
  const timeless = page === 'schedules';

  const projectButton = h('button', { class: 'btn' }, icon('folder'), projectText(s), icon('chevron'));
  const projectPicker = menuButton(projectButton, (menu, close) => {
    menu.append(menuItem('全部项目', { checked: !s.projects.length, meta: state.projects.length, onPick: () => { close(); setScope({ projects: [] }); } }));
    if (state.projects.length) menu.append(h('div', { class: 'menu-sep' }));
    for (const project of state.projects) {
      const on = s.projects.includes(project.id);
      menu.append(menuItem(project.name, {
        checked: on, meta: project.category || null,
        onPick: () => { close(); setScope({ projects: on ? s.projects.filter((id) => id !== project.id) : [...s.projects, project.id] }); },
      }));
    }
    if (!state.projects.length) menu.append(h('div', { class: 'menu-title' }, '还没有项目'));
  });

  const rangeButton = h('button', { class: 'btn', disabled: timeless, title: timeless ? '定时任务不按时间范围筛选' : null },
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

  replace(bar,
    h('span', { class: 'scope-label' }, '当前范围'),
    projectPicker, rangePicker,
    h('span', { class: 'scope-text', 'aria-live': 'polite' },
      h('b', {}, projectText(s)), ' · ', h('b', {}, timeless ? '不限时间' : rangeText(s))),
    !isDefaultScope(s) && h('button', {
      class: 'btn ghost sm', title: '回到「全部项目 · 近 7 天」',
      onClick: () => setScope({ projects: [], range: DEFAULT_RANGE, from: '', to: '' }),
    }, icon('reset'), '重置'),
    h('span', { class: 'grow' }),
    runSettings());
}
