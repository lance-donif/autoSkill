// Projects and cases: see what exists, run one case or a batch, open a case to read its steps.
import { ensurePreferences } from '../preferences.js';
import { h, icon, replace, api, scope, scopeQuery, go, attempt, toast, fmtTime, route, refresh, onChange } from '../core.js';
import { empty, errorState, runButton, startRun, drawer, closeOverlay, confirm, preferredHeaded, preferredEngine } from '../ui.js';
import { runtimeFields, credentialControls } from '../project-settings.js';
import { state, caseProjects, refreshShell } from '../shell.js';
import { loadTestButton } from './load-tests.js';
import { ALL_FOLDERS, folderSelection, folderTree, folderPath, folderEditor, moveCases } from '../folders.js';
import { updateLiveView } from '../live-refresh.js';
import { editCase } from '../case-editor.js';
import { stateResults } from '../status-view.js';


const selected = new Map();  // "project/case" -> {project_id, case_id}
let kind = '';
let query = '';
let deletionErrors = [];
let deletionWarnings = [];
let handledEntry = '';
onChange(() => { if (route().page !== 'cases') handledEntry = ''; });

const keyOf = (item) => item.project_id + '/' + item.case_id;
const kindTag = (value) => h('span', { class: 'tag' }, icon(value === 'ui' ? 'globe' : 'api'), value === 'ui' ? '页面' : '接口');
const caseFolder = (tree, id) => tree && Object.hasOwn(tree.assignments, id) ? folderPath(tree, tree.assignments[id]) : '';
const searchText = (value) => value.trim().toLowerCase().replace(/\s*\/\s*/g, '/');

// The scope bar sticks on desktop. Compact and embedded layouts keep the navigation bar on top instead.
const scopeBar = document.getElementById('scope');
const sideBar = document.getElementById('side');
const headerSizes = new ResizeObserver(() => {
  const header = getComputedStyle(scopeBar).position === 'sticky' ? scopeBar : sideBar;
  document.getElementById('view').style.setProperty('--case-filter-top', header.getBoundingClientRect().height + 'px');
});
headerSizes.observe(scopeBar, { box: 'border-box' });
headerSizes.observe(sideBar, { box: 'border-box' });

export async function render(view, { isCurrent }) {
  const scopeIds = scope().projects;
  const trees = new Map();
  const cards = new Map();
  const byProject = new Map();
  let data, projects;

  async function loadTree(project) {
    trees.set(project.id, {});
    if (cards.has(project.id)) paintProject(project);
    let result;
    try { result = { data: await api(`/projects/${encodeURIComponent(project.id)}/folders`) }; }
    catch (error) { result = { error }; }
    if (!isCurrent()) return;
    const selectedFolder = folderSelection.get(project.id);
    if (result.data && selectedFolder && selectedFolder !== ALL_FOLDERS && !result.data.folders.some((folder) => folder.id === selectedFolder)) {
      folderSelection.delete(project.id);
    }
    trees.set(project.id, result);
    if (cards.has(project.id)) {
      paintProject(project);
      paintBar();
    }
  }

  try {
    [data, projects] = await Promise.all([api('/cases' + scopeQuery()), caseProjects().then((all) => {
      const scoped = scopeIds.length ? all.filter((project) => scopeIds.includes(project.id)) : all;
      if (isCurrent()) for (const project of scoped) void loadTree(project);
      return scoped;
    })]);
  } catch (error) {
    replace(view, h('div', { class: 'card' }, errorState(error, () => refresh())));
    return;
  }
  if (!isCurrent()) return;
  for (const item of data.items) {
    if (!byProject.has(item.project_id)) byProject.set(item.project_id, []);
    byProject.get(item.project_id).push(item);
  }
  const list = h('div', { class: 'stack' });
  const bar = h('div', { class: 'select-bar', hidden: true });
  const search = h('input', { class: 'input', type: 'search', placeholder: '搜索编号、标题、菜单名或目录', value: query, 'aria-label': '搜索用例', style: 'width:280px',
    onInput: (event) => { query = event.currentTarget.value; paint(); } });
  const kinds = h('div', { class: 'seg', role: 'group', 'aria-label': '用例类型' },
    [['', '全部'], ['ui', '页面'], ['api', '接口']].map(([value, label]) => h('button', {
      'aria-pressed': String(kind === value),
      onClick: (event) => { kind = value; for (const b of kinds.children) b.setAttribute('aria-pressed', String(b === event.currentTarget)); paint(); },
    }, label)));

  function visible(project) {
    const text = searchText(query);
    return (byProject.get(project.id) || []).filter((item) => (!kind || item.kind === kind)
      && (!text || searchText([item.case_id, item.title, item.module, item.folder_path].join(' ')).includes(text)));
  }

  function displayed(project) {
    const folder = folderSelection.get(project.id) ?? ALL_FOLDERS;
    const tree = trees.get(project.id)?.data;
    if (folder === ALL_FOLDERS) return visible(project);
    return tree ? visible(project).filter((item) => (tree.assignments[item.case_id] || '') === folder) : [];
  }

  function byProjectIds(items) {
    const groups = new Map();
    for (const item of items) {
      if (!groups.has(item.project_id)) groups.set(item.project_id, []);
      groups.get(item.project_id).push(item.case_id);
    }
    return groups;
  }

  function paintBar() {
    const previousFocus = bar.contains(document.activeElement) ? document.activeElement.textContent : null;
    const items = [...selected.values()];
    bar.hidden = !items.length;
    if (!items.length) {
      if (previousFocus !== null) kinds.querySelector('[aria-pressed="true"]').focus({ preventScroll: true });
      return;
    }
    const byProject = byProjectIds(items);
    const apiCases = byProjectIds(items.filter((item) => item.kind === 'api'));
    const visibleKeys = new Set(projects.flatMap(displayed).map(keyOf));
    const hidden = items.filter((item) => !visibleKeys.has(keyOf(item)));
    const scopeReady = items.every((item) => !trees.has(item.project_id) || trees.get(item.project_id).data);
    const clear = h('button', { class: 'btn ghost', onClick: () => {
      selected.clear();
      for (const box of list.querySelectorAll('tbody .check:checked')) markCaseRow(box, false);
      for (const master of list.querySelectorAll('thead .check')) {
        master.checked = false;
        master.indeterminate = false;
      }
      paintBar();
    } }, '清除');
    replace(bar,
      h('span', { class: 'grow' }, h('b', {}, `已选 ${items.length} 条用例`), byProject.size > 1 ? ` · ${byProject.size} 个项目` : '',
        hidden.length > 0 && h('span', { class: 'muted' }, `　其中 ${hidden.length} 条不在当前显示范围，运行所选仍会包含它们。`)),
      hidden.length > 0 && h('button', { class: 'btn', onClick: () => { for (const item of hidden) selected.delete(keyOf(item)); paintBar(); } }, `清除隐藏的 ${hidden.length} 条`),
      clear,
      caseDeleteButton(items, { bulk: true }),
      h('button', { class: 'btn', disabled: !scopeReady, onClick: () => moveCases(items) }, icon('folder'), '移动到文件夹'),
      apiCases.size > 0 && loadTestButton('压测所选', () => [...apiCases].map(([project_id, case_ids]) => ({ project_id, case_ids }))),
      runButton(`运行所选 ${items.length} 条`, () => [...byProject].map(([project_id, case_ids]) => ({ project_id, case_ids,
        name: state.projects.find((project) => project.id === project_id)?.name || project_id })),
      { disabled: !scopeReady, title: scopeReady ? '运行所有勾选的用例，包含当前筛选隐藏的用例' : '文件夹范围尚未加载，请刷新后再运行' }));
    if (previousFocus !== null) {
      const matching = [...bar.querySelectorAll('button:not(:disabled)')].find((button) => button.textContent === previousFocus);
      (matching || clear).focus({ preventScroll: true });
    }
  }

  function paintProject(project) {
    const items = byProject.get(project.id) || [];
    const result = trees.get(project.id);
    for (const item of items) item.folder_path = caseFolder(result?.data, item.case_id);
    const card = projectCard(project, displayed(project), result, {
      retry: () => loadTree(project),
      chooseFolder: (id) => { folderSelection.set(project.id, id); paintProject(project); paintBar(); },
      selectionChanged: paintBar,
    });
    const current = cards.get(project.id);
    if (current) updateLiveView(current, card);
    else { cards.set(project.id, card); list.append(card); }
  }

  function paint() {
    for (const project of projects) paintProject(project);
    if (!projects.length) {
      replace(list, h('div', { class: 'card' }, empty({ glyph: 'folder', title: '没有符合筛选的项目', text: '换一个项目筛选查看已有用例。' })));
    }
    paintBar();
  }

  replace(view,
    h('div', { class: 'page-head' },
      h('div', { class: 'grow' }, h('h1', {}, '测试用例'),
        h('p', { class: 'sub' }, `${projects.length} 个项目 · ${data.total} 条用例`))),
    state.projects.length ? h('div', { class: 'toolbar case-filters' }, kinds, search) : null,
    caseDeletionNotice(), list, bar);
  if (!state.projects.length) {
    replace(list, h('div', { class: 'card' }, empty({
      glyph: 'spark', title: '还没有测试用例', text: '在 DSH 对话中告诉 AI 测试地址与目标，验证通过的用例会自动显示在这里。',
    })));
  } else {
    paint();
  }
  const params = route().params;
  const entry = params.get('settings') ? 'settings:' + params.get('settings') : '';
  if (entry !== handledEntry) {
    handledEntry = entry;
    if (!document.querySelector('#layer [role="dialog"]')) {
      if (params.get('settings')) await projectSettings(params.get('settings'));
    }
  }
}

function markCaseRow(box, checked) {
  box.checked = checked;
  box.closest('tr').classList.toggle('selected', checked);
}

function projectCard(project, items, folderResult, { retry, chooseFolder, selectionChanged }) {
  const tree = folderResult?.data;
  const folderId = folderSelection.get(project.id) ?? ALL_FOLDERS;
  const allKeys = items.map(keyOf);
  const master = h('input', { class: 'check', type: 'checkbox', 'aria-label': `选择「${project.name}」当前显示的 ${items.length} 条用例` });
  function updateMaster() {
    master.checked = allKeys.length > 0 && allKeys.every((key) => selected.has(key));
    master.indeterminate = !master.checked && allKeys.some((key) => selected.has(key));
  }
  updateMaster();
  master.addEventListener('change', () => {
    for (const item of items) { if (master.checked) selected.set(keyOf(item), item); else selected.delete(keyOf(item)); }
    for (const box of master.closest('table').querySelectorAll('tbody .check')) markCaseRow(box, master.checked);
    selectionChanged();
  });
  const missing = project.missing_secrets || [];
  const head = h('div', { class: 'card-head project-head' },
    h('div', { class: 'grow' },
      h('h2', {}, project.name),
      h('p', { class: 'sub ellipsis' }, `${project.base_url} · 页面 ${project.cases.ui} · 接口 ${project.cases.api}`)),
    project.cases.api > 0 && loadTestButton('压测', () => [{ project_id: project.id }], { small: true }),
    h('a', { class: 'btn sm', href: `/api/projects/${encodeURIComponent(project.id)}/folders/export`, download: '' }, icon('table'), '导出测试用例'),
    h('button', { class: 'btn sm icon', 'aria-label': `「${project.name}」设置`, title: '项目设置', onClick: () => projectSettings(project.id) }, icon('edit')));

  const banner = missing.length ? h('div', { class: 'card-body', style: 'padding-bottom:0' }, h('div', { class: 'banner' }, icon('lock'),
    h('span', {}, `缺少凭据 ${missing.join('、')}：依赖它的用例会被阻塞。`),
    h('button', { class: 'btn sm', onClick: () => projectSettings(project.id) }, '去填写'))) : null;

  // sub-projects: the areas of one site, taken from each case's module
  const modules = [...new Set(items.map((item) => item.module))];
  const groupRow = (module) => modules.length > 1 && h('tr', { class: 'group' }, h('td', { colSpan: 7 },
    `菜单名：${module}`, h('span', { class: 'muted' }, `　${items.filter((item) => item.module === module).length} 条`)));
  let body;
  if (!items.length) {
    body = empty({ glyph: 'cases', title: tree ? '没有符合条件的用例' : '用例范围尚未加载', text: tree ? '换一个类型、搜索词或文件夹试试。' : '读取文件夹后显示当前范围的用例。' });
  } else {
    body = h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, h('th', { style: 'width:36px' }, master), h('th', {}, '用例'), h('th', {}, '类型'), h('th', {}, '菜单名'),
        h('th', {}, '当前结果'), h('th', {}, '最近执行'), h('th', { class: 'actions' }, ''))),
      h('tbody', {}, modules.map((module) => [groupRow(module), items.filter((item) => item.module === module).map((item) => {
        const key = keyOf(item);
        const box = h('input', { class: 'check', type: 'checkbox', checked: selected.has(key), 'aria-label': `选择 ${item.case_id}` });
        box.addEventListener('click', (event) => event.stopPropagation());
        box.addEventListener('change', () => {
          if (box.checked) selected.set(key, item); else selected.delete(key);
          markCaseRow(box, box.checked);
          updateMaster();
          selectionChanged();
        });
        const deps = JSON.parse(item.depends_on || '[]');
        const run = h('button', { class: 'btn sm', title: '只运行这一条', onClick: (event) => { event.stopPropagation(); startRun([{ project_id: item.project_id, case_ids: [item.case_id], name: project.name }], run); } }, icon('play'), '运行');
        const openDetail = () => caseDrawer(item.project_id, item.case_id);
        return h('tr', { class: 'click' + (selected.has(key) ? ' selected' : ''), onClick: openDetail },
          h('td', {}, box),
          h('td', {}, h('button', { class: 'title case-title', type: 'button', 'aria-haspopup': 'dialog',
            onClick: (event) => { event.stopPropagation(); openDetail(); } }, item.title),
            h('div', { class: 'sub mono' }, item.case_id, deps.length ? `　前置 ${deps.join('、')}` : '')),
          h('td', {}, kindTag(item.kind)),
          h('td', { class: 'sub' }, item.module),
          h('td', {}, caseStatus(item)),
          h('td', { class: 'sub num' }, fmtTime(item.last_run_at)),
          h('td', { class: 'actions' }, h('button', { class: 'btn sm icon', title: '编辑用例', 'aria-label': `编辑用例 ${item.case_id}`,
            onClick: (event) => { event.stopPropagation(); editCase(item.project_id, item.case_id); } }, icon('edit')),
            h('button', { class: 'btn sm icon', title: '移动到文件夹', 'aria-label': `移动用例 ${item.case_id}`, disabled: !tree, onClick: (event) => { event.stopPropagation(); moveCases([item]); } }, icon('folder')),
            item.kind === 'api' && loadTestButton('压测', () => [{ project_id: item.project_id, case_ids: [item.case_id] }], { small: true }),
            run, caseDeleteButton([item], { small: true })));
      })])));
  }
  const error = folderResult?.error;
  return h('section', { class: 'card' }, head, banner,
    error && h('div', { class: 'card-body' }, h('div', { class: 'banner bad', role: 'alert' }, icon('warn'), `文件夹加载失败：${error.message}`,
      h('button', { class: 'btn sm', onClick: retry }, '重试文件夹'))),
    h('div', { class: error ? '' : 'project-folders' },
      tree ? folderTree(project, tree, folderId, chooseFolder)
        : !error && h('div', { class: 'folder-tree' }, h('p', { class: 'sub', role: 'status' }, '正在加载文件夹…')),
      h('div', { class: 'folder-content' }, !error && (tree || folderId === ALL_FOLDERS) && projectFolderTools(project, tree, folderId, items.length),
        h('div', { class: 'table-scroll', role: 'region', tabindex: '0', 'aria-label': `${project.name}用例表格` }, body))));
}

function projectFolderTools(project, tree, folderId, count) {
  const folder = tree?.folders.find((item) => item.id === folderId);
  return h('div', { class: 'folder-toolbar row wrap' }, h('div', { class: 'grow' },
    h('h3', {}, folderId === ALL_FOLDERS ? '全部用例' : folderPath(tree, folderId)),
    h('p', { class: 'sub' }, `${count} 条用例`)),
  folder && h('button', { class: 'btn sm', onClick: () => folderEditor(project, tree, folder.parent_id, folder) }, icon('edit'), '编辑文件夹'),
  folderId !== ALL_FOLDERS && h('a', { class: 'btn sm', download: '', title: '包含当前文件夹及全部子文件夹的用例',
    href: `/api/projects/${encodeURIComponent(project.id)}/folders/export?folder_id=${encodeURIComponent(folderId || 'root')}&recursive=true&expected_revision=${encodeURIComponent(tree.revision)}` },
  icon('table'), '导出文件夹 Excel'),
  h('button', { class: 'btn sm', disabled: !tree, onClick: () => folderEditor(project, tree, folderId === ALL_FOLDERS ? null : folderId) }, icon('plus'), '新建文件夹'));
}

function caseDeleteButton(items, { bulk = false, small = false } = {}) {
  const label = bulk ? '删除所选' : '删除用例';
  const button = h('button', { class: 'btn danger' + (small ? ' sm icon' : ''),
    title: label, 'aria-label': small ? `删除用例 ${items[0].case_id}` : label,
    onClick: (event) => { event.stopPropagation(); deleteCases(items, button, bulk); },
  }, icon('trash'), !small && label);
  return button;
}

async function deleteCases(items, button, bulk) {
  const first = items[0];
  const agreed = await confirm({ title: bulk ? `删除所选的 ${items.length} 条用例？` : '删除这条用例？',
    text: bulk ? `将删除所选的 ${items.length} 条用例定义。已有执行记录会保留。`
      : `将删除「${first.title || first.case_id}」（${first.case_id}）的用例定义。已有执行记录会保留。`,
    action: bulk ? '删除所选' : '删除用例', danger: true });
  if (!agreed) return;
  const result = await attempt(() => bulk
    ? api('/cases/delete', { method: 'POST', body: { items: items.map(({ project_id, case_id }) => ({ project_id, case_id })) } })
    : api(`/projects/${encodeURIComponent(first.project_id)}/cases/${encodeURIComponent(first.case_id)}`, { method: 'DELETE' }), button);
  if (!result) return;
  selected.clear();
  deletionErrors = result.errors || [];
  deletionWarnings = (result.deleted || []).flatMap((item) => (item.warnings || []).map((message) => ({ ...item, message })));
  const count = (result.deleted || []).length;
  toast(`已删除 ${count} 条用例` + (deletionErrors.length ? `，${deletionErrors.length} 条未删除` : ''), { bad: deletionErrors.length > 0 });
  await refreshShell();
  refresh();
}

function caseDeletionNotice() {
  return [deletionWarnings.length > 0 && h('div', { class: 'banner info', role: 'status' }, icon('warn'),
    h('div', { class: 'grow' }, h('b', {}, '用例已删除，部分关联展示尚未刷新'),
      deletionWarnings.map((item) => h('p', {}, `${item.project_id} / ${item.case_id}：${item.message}`))),
    h('button', { class: 'btn sm icon', 'aria-label': '关闭刷新提示', onClick: () => { deletionWarnings = []; refresh(); } }, icon('x'))),
  deletionErrors.length > 0 && h('div', { class: 'banner bad', role: 'alert' }, icon('warn'),
    h('div', { class: 'grow' }, h('b', {}, '部分用例未删除'),
      deletionErrors.map((error) => h('p', {}, `${error.project_id || ''} / ${error.case_id || ''}：${error.message}`))),
    h('button', { class: 'btn sm icon', 'aria-label': '关闭删除提示', onClick: () => { deletionErrors = []; refresh(); } }, icon('x')))];
}

async function projectSettings(projectId) {
  const source = location.hash;
  let project;
  try { project = await api('/projects/' + projectId); } catch (error) { toast(error.message, { bad: true }); return; }
  if (location.hash !== source) return;
  const name = h('input', { class: 'input', value: project.name, id: 'ps-name' });
  const runtime = runtimeFields(project);

  const save = h('button', { class: 'btn primary' }, '保存');
  save.addEventListener('click', async () => {
    const done = await attempt(() => api('/projects/' + projectId, { method: 'PATCH', body: {
      name: name.value, ...runtime.read(),
    } }), save);
    if (done) { await refreshShell(); closeOverlay(); toast('已保存'); refresh(); }
  });

  const credentials = credentialControls(project, async () => { await refreshShell(); refresh(); });
  const loginButton = (force) => {
    const button = h('button', { class: 'btn', disabled: !project.login.ui_case && !project.login.api_case, onClick: async () => {
      const run = await attempt(async () => {
        await ensurePreferences();
        return api(`/projects/${encodeURIComponent(projectId)}/login`, { method: 'POST', body: {
        force, engine: preferredEngine(), headed: preferredHeaded(),
        } });
      }, button);
      if (run) { closeOverlay(); go('runs/' + run.id); }
    } }, icon('play'), force ? '强制重新登录' : '立即登录');
    return button;
  };
  const session = project.session;
  const forget = h('button', { class: 'btn' }, '清除登录态');
  forget.addEventListener('click', async () => {
    const done = await attempt(() => api(`/projects/${projectId}/session`, { method: 'DELETE' }), forget);
    if (done) { toast('已清除，下次运行会重新登录'); closeOverlay(); }
  });

  drawer({
    title: '项目设置', subtitle: project.base_url,
    body: [
      h('div', { class: 'field' }, h('label', { for: 'ps-name' }, '项目名称'), name), runtime.node,
      h('div', { class: 'stack' }, h('h3', {}, '登录'),
        h('p', { class: 'sub' }, project.login.ui_case || project.login.api_case
          ? `登录用例：${[project.login.ui_case, project.login.api_case].filter(Boolean).join('、')}。运行依赖登录的用例时会自动先登录，并在本机复用登录态。`
          : '还没有登录用例。让 AI 用 autoskill record start … --as-login 录制一次登录即可。'),
        h('div', { class: 'row wrap' }, loginButton(false), loginButton(true)),
        h('p', { class: 'hint' }, '登录异步执行，窗口设置沿用顶部回放偏好；执行记录保存进度和结果。'),
        session.saved && h('div', { class: 'row' }, h('span', { class: 'sub grow' }, `登录态保存于 ${fmtTime(session.saved_at)}（只保存在本机）`), forget)),
      credentials,
      h('details', { class: 'more' }, h('summary', {}, '导出'),
        h('div', { class: 'row wrap', style: 'margin-top:12px' },
          h('a', { class: 'btn', href: `/api/projects/${projectId}/report`, download: '' }, icon('table'), 'Excel 测试报告'),
          h('a', { class: 'btn', href: `/api/projects/${projectId}/postman?part=collection`, download: '' }, icon('api'), 'Postman 集合'),
          h('a', { class: 'btn', href: `/api/projects/${projectId}/postman?part=environment`, download: '' }, 'Postman 环境'),
          h('span', { class: 'hint sub' }, '凭据值不会导出，在 Postman 环境里自行填写。'))),
    ],
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save],
  });
}

async function caseDrawer(projectId, caseId) {
  const source = location.hash;
  const caseUrl = `/projects/${encodeURIComponent(projectId)}/cases/${encodeURIComponent(caseId)}`;
  let item;
  try { item = await api(caseUrl); } catch (error) { toast(error.message, { bad: true }); return; }
  if (location.hash !== source) return;
  const body = [];
  if (item.depends_on?.length) {
    body.push(h('div', { class: 'banner info' }, icon('link'), h('span', {}, `前置用例：${item.depends_on.join('、')}。运行这条用例时会自动先执行。`)));
  }
  if (item.kind === 'ui') {
    body.push(h('div', { class: 'stack', style: 'gap:8px' }, h('h3', {}, '回放步骤'),
      h('ol', { class: 'steps' },
        h('li', {}, h('span', { class: 'step-text' }, '打开 ' + item.start_url)),
        (item.steps || []).map((step) => h('li', { class: step.check ? 'is-check' : '' },
          h('span', { class: 'step-text' }, step.check && h('span', { class: 'tag accent' }, '断言'), step.text),
          step.locators?.length > 1 && h('span', { class: 'step-sub' }, `备选定位：${step.locators.slice(1).join(' / ')}`))))));
    if (!item.replayable) body.push(h('div', { class: 'banner' }, icon('warn'), '这条用例没有可回放的步骤。'));
    else body.push(h('div', { class: 'row wrap' }, h('span', { class: 'sub' }, '独立脚本（不依赖 autoSkill）：'),
      ['playwright', 'selenium'].map((engine) => h('a', { class: 'btn sm', download: '', href: `/api${caseUrl}/script?engine=${engine}` },
        engine === 'playwright' ? 'Playwright .py' : 'Selenium .py'))));
  } else {
    const request = item.request;
    body.push(h('div', { class: 'stack', style: 'gap:8px' }, h('h3', {}, '请求'),
      h('div', { class: 'req-line' }, h('span', { class: 'method ' + request.method.toLowerCase() }, request.method), h('span', { class: 'mono' }, request.path)),
      ['headers', 'params', 'json', 'form'].filter((part) => hasRequestValue(request[part])).map((part) => h('div', {},
        h('div', { class: 'sub', style: 'margin:6px 0 4px' }, { headers: '请求头', params: '查询参数', json: '请求体', form: '表单' }[part]),
        h('pre', { class: 'code' }, JSON.stringify(request[part], null, 2))))));
    const rules = item.assertions || {};
    const rows = apiAssertionRows(rules);
    if (rows.length) body.push(h('div', { class: 'stack', style: 'gap:8px' }, h('h3', {}, '断言'),
      h('table', { class: 'table kv' }, h('tbody', {}, rows.map(([label, value]) => h('tr', {}, h('td', { class: 'sub' }, label), h('td', { class: 'mono' }, String(value))))))));
    const extract = Object.entries(item.extract || {});
    if (extract.length) body.push(h('p', { class: 'sub' }, '为后续用例提供变量：' + extract.map(([name, pointer]) => `{{${name}}} ← ${pointer}`).join('，')));
    if (item.traffic === 'resource' || item.traffic === 'static') body.push(h('div', { class: 'banner info' }, icon('info'), '这是「页面资源或静态数据文件可访问」检查，不是业务接口测试。'));
  }
  const run = h('button', { class: 'btn primary' }, icon('play'), '运行这条用例');
  run.addEventListener('click', async () => { const started = await startRun([{ project_id: projectId, case_ids: [caseId], name: state.projects.find((project) => project.id === projectId)?.name }], run); if (started) closeOverlay(); });
  const subtitle = `${item.id} · ${item.kind === 'ui' ? '页面用例' : '接口用例'} · 菜单名 ${item.module} · 第 ${item.revision} 版`;
  drawer({ title: item.title, subtitle, body, wide: true,
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '关闭'), item.replayable && run] });
}

function caseStatus(item) {
  return stateResults(item);
}

function hasRequestValue(value) {
  return value != null && value !== '' && (typeof value !== 'object' || Object.keys(value).length > 0);
}

function apiAssertionRows(rules) {
  const rows = rules.status == null ? [] : [['状态码', rules.status]];
  for (const [pointer, value] of Object.entries(rules.json_equals || {})) rows.push([`字段 ${pointer}`, JSON.stringify(value)]);
  for (const [name, value] of Object.entries(rules.header_contains || {})) rows.push([`响应头 ${name} 包含`, value]);
  for (const value of rules.body_contains || []) rows.push(['响应内容包含', value]);
  if (rules.schema) rows.push(['响应结构', '与录制时一致（字段和类型）']);
  if (rules.max_ms) rows.push(['响应时间不超过', rules.max_ms + ' ms']);
  return rows;
}
