// Project and case operations for the browser-local dashboard demo.
import { resultOutcome, verdictStatus } from './verdicts.js';

const copy = structuredClone;
const stamp = () => Array.from(crypto.getRandomValues(new Uint32Array(4)), (part) => part.toString(16).padStart(8, '0')).join('');
const keyOf = (projectId, caseId) => projectId + '/' + caseId;
const problem = (message, status = 400) => Response.json({ detail: message }, { status });

export function initCases(db, fixtures) {
  if (db.caseLibrary) return db.caseLibrary;
  const items = copy(fixtures['/cases']?.items || []);
  const folders = {};
  const details = {};
  const definitions = {};
  const projects = (fixtures['/projects'] || []).map((summary) => {
    const path = '/projects/' + summary.id;
    folders[summary.id] = copy(fixtures[path + '/folders'] || { project_id: summary.id, revision: stamp(), folders: [], assignments: {} });
    return copy({ ...summary, ...fixtures[path] });
  });
  for (const item of items) {
    const key = keyOf(item.project_id, item.case_id);
    details[key] = copy(fixtures['/projects/' + item.project_id + '/cases/' + item.case_id]);
    definitions[key] = copy(fixtures['/case-definitions/' + key]);
    item.revision = definitions[key]?.case.revision || details[key]?.revision || 1;
  }
  const secrets = copy(fixtures['/secrets']?.items || []);
  db.caseLibrary = { items, projects, folders, definitions, details, secrets };
  return db.caseLibrary;
}

function projectView(library, project) {
  const items = library.items.filter((item) => item.project_id === project.id && item.lifecycle !== 'archived');
  const names = new Set();
  for (const item of items) {
    const definition = library.definitions[keyOf(project.id, item.case_id)]?.case;
    for (const match of JSON.stringify(definition || {}).matchAll(/\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}/g)) names.add(match[1]);
  }
  const credentials = Object.fromEntries([...names].sort().map((name) => [
    name, library.secrets.find((item) => item.name === name)?.value ?? null,
  ]));
  return { ...project, credentials, missing_secrets: Object.keys(credentials).filter((name) => credentials[name] === null),
    cases: { ...project.cases, total: items.length, api: items.filter((item) => item.kind === 'api').length, ui: items.filter((item) => item.kind === 'ui').length } };
}

function updateSecrets(library, method, name, body) {
  if (method === 'GET' && !name) return { items: library.secrets };
  if (!name || !['PUT', 'DELETE'].includes(method)) return;
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return problem('凭据名称无效');
  if (method === 'PUT' && !body?.value) return problem('请输入凭据值');
  library.secrets = library.secrets.filter((item) => item.name !== name);
  if (method === 'PUT') library.secrets.push({ name, value: body.value });
  return { name, saved: method === 'PUT' };
}

function changeFolder(tree, method, id, body) {
  if (body.expected_revision && body.expected_revision !== tree.revision) return problem('文件夹已变更，请刷新后重试', 409);
  const parent = (id === 'move-cases' ? body.folder_id : body.parent_id) || null;
  const lookup = new Map(tree.folders.map((node) => [node.id, node]));
  if (parent && !lookup.has(parent)) return problem('目标文件夹不存在');
  if (method === 'POST' && id === 'move-cases') {
    if (!body.case_ids?.length || body.case_ids.some((cid) => !Object.hasOwn(tree.assignments, cid))) return problem('请选择有效的用例');
    for (const cid of body.case_ids) tree.assignments[cid] = parent;
  } else {
    const name = String(body.name || '').trim().normalize();
    if (!name) return problem('请填写文件夹名称');
    const folder = method === 'PATCH' ? lookup.get(id) : { id: 'folder-' + stamp() };
    if (!folder) return problem('文件夹不存在', 404);
    for (let node = lookup.get(parent); node; node = lookup.get(node.parent_id)) {
      if (node.id === folder.id) return problem('不能将文件夹移入自身或子文件夹');
    }
    if (tree.folders.some((node) => node.id !== folder.id && node.parent_id === parent && node.name === name)) return problem('同级文件夹名称重复');
    Object.assign(folder, { name, parent_id: parent });
    if (method === 'POST') tree.folders.push(folder);
  }
  tree.revision = stamp();
  return tree;
}

function removeCases(library, targets, expectedWorkspace, workspace) {
  if (expectedWorkspace !== null && expectedWorkspace !== workspace) return problem('数据目录已变化，请刷新页面后重新选择要删除的用例', 409);
  const deleted = [];
  const errors = [];
  for (const { project_id, case_id } of targets) {
    const key = keyOf(project_id, case_id);
    const item = library.items.find((row) => row.project_id === project_id && row.case_id === case_id);
    if (!item) {
      errors.push({ project_id, case_id, message: '用例不存在' });
      continue;
    }
    library.items.splice(library.items.indexOf(item), 1);
    Reflect.deleteProperty(library.details, key);
    Reflect.deleteProperty(library.definitions, key);
    const tree = library.folders[project_id];
    Reflect.deleteProperty(tree.assignments, case_id);
    tree.revision = stamp();
    const login = library.projects.find((project) => project.id === project_id)?.login;
    if (login) for (const field of ['ui_case', 'api_case', 'api_check_case']) if (login[field] === case_id) login[field] = null;
    deleted.push({ project_id, case_id, title: item.title, warnings: [] });
  }
  return { deleted, errors };
}

function caseStatus(db, item, end) {
  const noHistory = { state: null, last_state: null, current: false, last_status: null, last_run_at: null, current_status: null };
  if (!db.runs) {
    const { state, last_state, current, current_status, last_status, last_run_at } = item;
    return { state, last_state, current, current_status, last_status, last_run_at };
  }
  const relevant = db.runs.filter((run) => run.project_id === item.project_id && run.trigger !== 'login' && run.family !== 'load'
    && !['Queued', 'Running'].includes(run.status) && (!end || Date.parse(run.created_at) < Date.parse(end)))
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  for (const run of relevant) {
    const row = run.results?.find((result) => result.case_id === item.case_id && !result.prerequisite);
    if (!row) continue;
    const savedScope = item.state?.scope?.run_id === run.id ? item.state.current ?? item.current : null;
    const current = savedScope !== false && (row.case_revision == null ? savedScope === true : row.case_revision === item.revision);
    const outcome = resultOutcome(row);
    const state = { ...row.state, current, outcome: current ? outcome : 'not_run', historical_outcome: outcome,
      scope: { ...row.state?.scope, run_id: run.id } };
    return { state, last_state: state, current, current_status: current ? verdictStatus[outcome] : null,
      last_status: verdictStatus[outcome], last_run_at: row.started_at || run.created_at };
  }
  return noHistory;
}

export function restoreCaseStates(db, changedProjects) {
  for (const item of db.caseLibrary.items) {
    const key = keyOf(item.project_id, item.case_id);
    const detail = db.caseLibrary.details[key];
    const outdated = [item, detail].some((view) => view.current_status === 'Unverified' || view.last_status === 'Unverified'
      || [view.state, view.last_state].some((state) => state && (!Object.hasOwn(verdictStatus, state.outcome)
        || (state.historical_outcome && !Object.hasOwn(verdictStatus, state.historical_outcome)))));
    if (!changedProjects.has(item.project_id) && !outdated) continue;
    item.revision ??= db.caseLibrary.definitions[key].case.revision ?? detail.revision;
    const view = caseStatus(db, item);
    Object.assign(item, view);
    Object.assign(detail, view);
  }
}

function saveCase(library, projectId, item, body) {
  const key = keyOf(projectId, item.case_id);
  const source = library.definitions[key];
  if (source.content_revision !== body.expected_content_revision) return problem('用例版本已变更，请刷新后重试', 409);
  const draft = copy(body.case);
  if (!draft || draft.id !== item.case_id || draft.kind !== item.kind || !draft.title?.trim()) return problem('用例定义无效');
  const automation = draft.extensions?.automation;
  const actions = (automation?.actions || []).filter((step) => step.action);
  if (actions.some((step, index) => step._edit_id !== source.editing?.actions?.[index]?.id)
    || actions.length !== (source.editing?.actions?.length || 0)) return problem('请保留既有动作及其顺序');
  for (const step of actions) Reflect.deleteProperty(step, '_edit_id');
  if (JSON.stringify(draft) === JSON.stringify(source.case)) return { changed: false, revision: item.revision };
  draft.revision = item.revision + 1;
  const depends = draft.extensions?.depends_on || [];
  const detail = library.details[key];
  Object.assign(item, { title: draft.title, module: draft.module, priority: draft.priority, lifecycle: draft.lifecycle,
    revision: draft.revision, depends_on: JSON.stringify(depends), current: false, current_status: null });
  if (item.state) item.state = { ...item.state, historical_outcome: item.state.outcome, current: false, outcome: 'not_run' };
  Object.assign(detail, { id: item.case_id, title: item.title, module: item.module, priority: item.priority,
    risk: draft.risk, expected: draft.expected, revision: item.revision, depends_on: depends });
  if (draft.kind === 'api') {
    detail.request = Object.fromEntries(['method', 'path', 'headers', 'params', 'json', 'form'].filter((field) => field in draft).map((field) => [field, draft[field]]));
    detail.assertions = draft.assertions;
    detail.extract = draft.extensions?.extract || {};
  } else if (automation) {
    detail.start_url = automation.start_url;
    if (JSON.stringify(automation) !== JSON.stringify(source.case.extensions.automation)) detail.steps = [...automation.actions, ...(automation.assertions || [])].map((step) => ({
      text: step.name || [step.action || step.assertion, step.locators?.[0]?.value || step.selector, step.url || step.value || step.expected].filter((value) => value != null).join(' '),
      check: !!step.assertion, locators: (step.locators || []).map((loc) => loc.value || loc.name || loc.by),
    }));
    source.editing.actions = automation.actions.flatMap((step, index) => step.action ? [{ index, id: 'action-' + stamp(), action: step.action }] : []);
  }
  source.case = draft;
  source.content_revision = stamp();
  const tree = library.folders[projectId];
  tree.revision = stamp();
  if (draft.lifecycle === 'archived') Reflect.deleteProperty(tree.assignments, draft.id);
  else if (!Object.hasOwn(tree.assignments, draft.id)) tree.assignments[draft.id] = null;
  return { changed: true, revision: draft.revision, content_revision: source.content_revision };
}

export function casesApi({ path, method = 'GET', params = new URLSearchParams(), body, db, fixtures }) {
  const [, resource, projectId, section, id, sub] = path.split('/').map(decodeURIComponent);
  if (!['cases', 'projects', 'case-definitions', 'secrets'].includes(resource)) return;
  const library = initCases(db, fixtures);
  const workspace = db.settings?.data_id ?? fixtures['/settings']?.data_id;
  if (resource === 'secrets') return updateSecrets(library, method, projectId, body);
  if (resource === 'cases') {
    if (method === 'POST' && projectId === 'delete') return removeCases(library, body.items, params.get('expected_workspace'), workspace);
    if (method !== 'GET' || projectId) return;
    const selected = (params.get('projects') || '').split(',').filter(Boolean);
    const start = params.get('start');
    const end = params.get('end');
    const items = library.items.filter((item) => item.lifecycle !== 'archived' && (!selected.length || selected.includes(item.project_id))
      && (!params.get('kind') || item.kind === params.get('kind')) && (!end || Date.parse(item.created_at) < Date.parse(end))
      && (params.get('added') !== 'true' || !start || Date.parse(item.created_at) >= Date.parse(start)))
      .map((item) => ({ ...item, ...caseStatus(db, item, end) }));
    return { total: items.length, items, workspace_identity: workspace };
  }
  if (!projectId && method === 'GET' && resource === 'projects') return library.projects.map((project) => projectView(library, project));
  if (sub || !projectId || section === 'login' || id === 'export' || ['report', 'postman'].includes(section)) return;
  const project = library.projects.find((p) => p.id === projectId);
  if (!project) return problem('项目不存在', 404);
  if (resource === 'case-definitions') {
    if (method !== 'GET' || !section) return;
    return library.definitions[keyOf(projectId, section)] || problem('用例不存在', 404);
  }
  if (!section && method === 'PATCH') {
    if (Object.hasOwn(body, 'name') && !body.name?.trim()) return problem('请填写项目名称');
    for (const field of ['timeout_seconds', 'page_load_seconds']) {
      if (Object.hasOwn(body, field) && (!Number.isFinite(body[field]) || body[field] <= 0)) return problem('超时需要是大于零的秒数');
    }
    if (Object.hasOwn(body, 'ignore_https_errors') && typeof body.ignore_https_errors !== 'boolean') return problem('忽略 HTTPS 证书错误必须是布尔值');
    for (const field of ['name', 'category', 'timeout_seconds', 'page_load_seconds', 'ignore_https_errors']) {
      if (Object.hasOwn(body, field)) project[field] = body[field];
    }
    for (const item of library.items) if (item.project_id === projectId) item.project_name = project.name;
    return projectView(library, project);
  }
  if (!section && method === 'GET') return projectView(library, project);
  if (section === 'session' && method === 'DELETE') {
    project.session = { saved: false };
    return { cleared: true };
  }
  if (section === 'folders') {
    const tree = library.folders[projectId];
    if (method === 'GET' && !id) return tree;
    if ((method === 'POST' && (!id || id === 'move-cases')) || (method === 'PATCH' && id)) return changeFolder(tree, method, id, body);
  }
  if (section === 'cases' && id) {
    const item = library.items.find((row) => row.project_id === projectId && row.case_id === id);
    if (!item) return problem('用例不存在', 404);
    if (method === 'GET') return { ...library.details[keyOf(projectId, id)], ...caseStatus(db, item) };
    if (method === 'PUT') return saveCase(library, projectId, item, body);
    if (method === 'DELETE') return removeCases(library, [{ project_id: projectId, case_id: id }], params.get('expected_workspace'), workspace);
  }
}
