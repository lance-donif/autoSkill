// The interactive Pages demo generates documents from the browser's current sample data.
import { casesApi } from './cases-api.js';
import { runPage, compactCaseCounts } from './runs-api.js';
import { folderPath, functionalWorkbook } from './workbook.js';
import { validateTasks } from './schedules-api.js';
import { previewZip, readPreviewEntry } from './preview-zip.js';
import { workspaceConflict, workspaceIdentity } from './workspace.js';

export { previewZip };
const MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const verdicts = { pass: '通过', fail: '失败', not_run: '未执行', Pass: '通过', Fail: '失败', NotRun: '未执行' };
const asText = (value) => value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value);
const mimeJson = 'application/json; charset=utf-8';
const notFound = (message) => Response.json({ detail: message }, { status: 404 });
const conflict = (message) => Response.json({ detail: message }, { status: 409 });

function file(content, filename, type) {
  return new Response(content, { headers: { 'Content-Type': type, 'Content-Disposition': "attachment; filename*=UTF-8''" + encodeURIComponent(filename) } });
}

function readCase(ctx, path, params = {}) {
  return casesApi({ ...ctx, path, method: 'GET', params: new URLSearchParams(params) });
}

function outcome(row) {
  return verdicts[row.state?.outcome || row.current_status || row.status] || '未执行';
}

function reportRow(original, result = original) {
  const automation = original.extensions?.automation || {};
  const fields = original.extensions?.test_case_document || {};
  const id = original.case_id || original.id;
  return [id, original.title || original.case_title || id, asText(original.requirement_ids), fields.description || original.description,
    asText(fields.preconditions || original.depends_on), asText(fields.steps || original.steps || automation.actions || automation.request),
    asText(fields.expected || original.expected || automation.assertions), fields.author, result.reason || result.actual || '',
    outcome(result), 'autoSkill', result.simulated ? '浏览器模拟' : result.engine || '', result.project_name || original.module || '', ''];
}

function summary(rows) {
  const counts = { '通过': 0, '失败': 0, '未执行': 0 };
  for (const row of rows) counts[row[9]]++;
  return [['结果', '用例数'], ...Object.entries(counts)];
}

function reportFile(name, rows, format, run) {
  const counts = run ? [['结果', '用例数'], ['通过', run.passed], ['失败', run.failed], ['未执行', run.not_run]] : summary(rows);
  const scope = run ? `共 ${run.total} 条，列出前 ${rows.length} 条执行明细。` : '';
  const failures = rows.filter((row) => row[9] === '失败');
  const lines = format === 'bugs' ? [`# ${name} · Bug 清单`, '', ...(scope ? [scope, `全部执行中共有 ${run.failed} 条失败。`, ''] : []),
    ...(failures.length ? failures.map((row) => `- ${row[0]} ${row[1]}　${row[8] || '执行失败'}`) : [scope ? '所列明细中没有失败用例。' : '本次没有失败用例。'])]
    : [`# ${name} · 测试简报`, '', ...(scope ? [scope, ''] : []), ...counts.slice(1).map(([key, n]) => `- ${key}：${n}`), '',
      ...rows.map((row) => `- ${row[0]} ${row[1]}：${row[9]}${row[8] ? '，' + row[8] : ''}`)];
  return file(lines.join('\n') + '\n', name + (format === 'bugs' ? '-bugs.md' : '.md'), 'text/markdown; charset=utf-8');
}

function findRun(ctx, reportId) {
  return (ctx.db.runs || []).find((run) => run.test_report?.report_id === reportId || 'run_' + run.id === reportId);
}

async function projectData(ctx, projectId) {
  const prefix = '/projects/' + encodeURIComponent(projectId);
  const list = await readCase(ctx, '/cases', { projects: projectId, range: 'all' });
  if (list.total !== list.items?.length) throw new Error('导出用例清单不完整，请刷新重试');
  const items = (list.items || []).filter((item) => item.project_id === projectId);
  return { prefix, items, definitions: await Promise.all(items.map(async (item) => {
    const record = await readCase(ctx, '/case-definitions/' + encodeURIComponent(projectId) + '/' + encodeURIComponent(item.case_id));
    return record.case;
  })) };
}

async function folderSelection(ctx, prefix) {
  const folder = ctx.params.get('folder_id');
  if (folder === null) return null;
  const tree = await readCase(ctx, prefix + '/folders');
  const expected = ctx.params.get('expected_revision');
  if (expected && expected !== String(tree.revision)) return conflict('文件夹已变化，请刷新后重试');
  const root = folder === 'root' ? null : folder;
  if (root && !tree.folders.some((node) => node.id === root)) return notFound('文件夹不存在');
  const ids = new Set([root]);
  if (ctx.params.get('recursive') !== 'false') {
    for (const parent of ids) {
      for (const node of tree.folders) if ((node.parent_id || null) === parent) ids.add(node.id);
    }
  }
  return new Set(Object.entries(tree.assignments).filter(([, parent]) => ids.has(parent || null)).map(([id]) => id));
}

function postman(definitions, project, fixture, format) {
  const secrets = new Set();
  const apiCases = definitions.filter((item) => item.kind === 'api');
  if (!apiCases.length) throw new Error('项目里还没有接口用例');
  const byId = new Map(definitions.map((row) => [row.id, row]));
  const ordered = new Set(), visiting = new Set();
  function visit(row) {
    if (ordered.has(row)) return;
    if (visiting.has(row)) throw new Error('用例依赖存在循环');
    visiting.add(row);
    for (const id of row.extensions?.depends_on || []) {
      if (!byId.has(id)) throw new Error('找不到前置用例 ' + id);
      visit(byId.get(id));
    }
    visiting.delete(row);
    ordered.add(row);
  }
  apiCases.forEach(visit);
  const cases = JSON.parse(JSON.stringify([...ordered].filter((row) => row.kind === 'api')).replace(/\$\{ENV:([\w]+)\}/g, (_, name) => {
    secrets.add(name);
    return '{{' + name + '}}';
  }));
  if (format === 'environment') {
    return { id: crypto.randomUUID(), name: project.name + ' 环境', _postman_variable_scope: 'environment',
      values: [{ key: 'base_url', value: project.base_url.replace(/\/+$/, ''), type: 'default', enabled: true },
        ...[...secrets].sort().map((key) => ({ key, value: '', type: 'secret', enabled: true }))] };
  }
  const fill = fixture?.item?.[0]?.event?.find((e) => e.listen === 'prerequest')?.script.exec[0];
  if (!fill) throw new Error('缺少示例 Postman 变量模板');
  const json = JSON.stringify;
  const escapeQuery = (text) => encodeURIComponent(text).replaceAll('%7B', '{').replaceAll('%7D', '}');
  const fields = (pairs) => Object.entries(pairs || {}).flatMap(([key, value]) => (Array.isArray(value) ? value : [value]).map((item) => ({ key, value: asText(item) })));
  const items = cases.map((row) => {
    const query = fields(row.params);
    const raw = '{{base_url}}' + row.path + (query.length ? '?' + query.map((v) => escapeQuery(v.key) + '=' + escapeQuery(v.value)).join('&') : '');
    const request = { method: row.method, description: row.expected, header: fields(row.headers),
      url: { raw, host: ['{{base_url}}'], path: row.path.split('/').filter(Boolean), query } };
    const rules = row.assertions || {};
    const prepare = [fill, '__fill(' + json(rules.json_equals || {}) + ');'];
    if (row.json != null) {
      request.body = { mode: 'raw', raw: json(row.json, null, 2), options: { raw: { language: 'json' } } };
      prepare.push('pm.request.body.update(JSON.stringify(__fill(' + json(row.json) + ')));');
      if (!request.header.some((entry) => entry.key.toLowerCase() === 'content-type')) request.header.push({ key: 'Content-Type', value: 'application/json' });
    } else if (row.form) request.body = { mode: 'urlencoded', urlencoded: fields(row.form) };
    const tests = [fill, 'const at = (p) => p === "" ? pm.response.json() : p.slice(1).split("/").reduce((o,k) => o[k.replace(/~1/g,"/").replace(/~0/g,"~")], pm.response.json());'];
    if (rules.status != null) tests.push('pm.test("状态码", () => pm.response.to.have.status(' + json(rules.status) + '));');
    for (const [k, v] of Object.entries(rules.json_equals || {})) tests.push('pm.test(' + json(k) + ', () => pm.expect(at(' + json(k) + ')).to.eql(__fill(' + json(v) + ')));');
    for (const [k, v] of Object.entries(rules.header_contains || {})) tests.push('pm.test(' + json(k) + ', () => pm.expect((pm.response.headers.get(' + json(k) + ') || "").toLowerCase()).to.include(' + json(String(v).toLowerCase()) + '));');
    for (const fragment of rules.body_contains || []) tests.push('pm.test("响应内容", () => pm.expect(pm.response.text()).to.include(' + json(fragment) + '));');
    if (rules.schema) tests.push('pm.test("响应结构", () => pm.response.to.have.jsonSchema(' + json(rules.schema) + '));');
    for (const k of rules.json_has || []) tests.push('pm.test(' + json(k) + ', () => pm.expect(at(' + json(k) + ')).not.to.equal(undefined));');
    if (rules.max_ms != null) tests.push('pm.test("响应耗时", () => pm.expect(pm.response.responseTime).to.be.at.most(' + json(rules.max_ms) + '));');
    for (const [k, p] of Object.entries(row.extensions?.extract || {})) tests.push('pm.test(' + json('提取变量 ' + k) + ', () => { const v = at(' + json(p) + '); pm.expect(v).not.to.equal(undefined); pm.collectionVariables.set(' + json(k) + ', v); pm.collectionVariables.set(' + json('__autoskill_json_' + k) + ', JSON.stringify(v)); });');
    return { id: crypto.randomUUID(), name: row.id + ' ' + row.title, request, event: [['prerequest', prepare], ['test', tests]].map(([listen, exec]) => ({ listen, script: { type: 'text/javascript', exec } })) };
  });
  const extracted = [...new Set(cases.flatMap((row) => Object.keys(row.extensions?.extract || {})))].sort();
  const initialization = ['if (pm.info.requestId === ' + json(items[0].id) + ' || !pm.collectionVariables.get("RAND")) {',
    '  pm.collectionVariables.set("RAND", pm.variables.replaceIn("{{$guid}}").replace(/-/g, "").slice(0, 12));',
    ...extracted.flatMap((key) => [key, '__autoskill_json_' + key].map((name) => '  pm.collectionVariables.unset(' + json(name) + ');')), '}'];
  return { info: { ...fixture.info, name: project.name, _postman_id: crypto.randomUUID() }, item: items,
    event: [{ listen: 'prerequest', script: { type: 'text/javascript', exec: initialization } }], variable: extracted.map((key) => ({ key, value: '' })) };
}

function uiScript(ctx, project, row, engine) {
  const path = '/projects/' + encodeURIComponent(project.id) + '/cases/' + encodeURIComponent(row.id) + '/script?engine=' + engine;
  const template = ctx.fixtures[path];
  if (typeof template !== 'string') return notFound('缺少此页面用例的独立脚本模板');
  const initial = ctx.fixtures['/case-definitions/' + project.id + '/' + row.id]?.case;
  if (row.kind !== 'ui') return notFound('仅页面用例可以导出浏览器脚本');
  const fname = row.id.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  const browser = engine === 'playwright' ? 'page' : 'driver';
  const start = template.indexOf('\ndef ' + fname + '(');
  const end = template.indexOf('\ndef main():', start);
  if (start < 0 || end < 0) return conflict('示例脚本版本不匹配');
  const auto = row.extensions.automation;
  const flows = [...(auto.setup_cases || []).map((stage) => stage.automation), auto];
  const args = JSON.stringify(JSON.stringify(flows));
  const open = engine === 'playwright' ? 'page.goto' : 'driver.get';
  const action = engine === 'playwright' ? 'page = tabs.action(step, TIMEOUT)' : 'tabs.action(step, TIMEOUT)';
  const sync = engine === 'playwright' ? 'page = tabs.sync(change, TIMEOUT)' : 'tabs.sync(change, TIMEOUT)';
  const src = [
    '', 'def ' + fname + '(' + browser + '):', '    import json', '    import re',
    '    from urllib.parse import urljoin', '    def resolve(value):',
    '        if isinstance(value, str):', '            return re.sub(r"\\$\\{ENV:([A-Za-z_][A-Za-z0-9_]*)\\}", lambda m: os.environ[m[1]], value).replace("{{RAND}}", RAND)',
    '        if isinstance(value, list):', '            return [resolve(item) for item in value]',
    '        if isinstance(value, dict):', '            return {key: resolve(item) for key, item in value.items()}', '        return value',
    '    flows = resolve(json.loads(' + args + '))', '    tabs = ' + engine + '_tabs(' + browser + ')',
    '    ' + open + '(urljoin(BASE_URL + "/", flows[0]["start_url"]))', '    for flow in flows:',
    '        for step in flow.get("actions", []) + flow.get("assertions", []):', '            op = step.get("action", "")',
    '            if "url" in step: step["url"] = urljoin(BASE_URL + "/", step["url"])', '            change = step.get("tabs")',
    '            if change:', '                for tab in change.get("opened", []):', '                    if tab.get("url"): tab["url"] = urljoin(BASE_URL + "/", tab["url"])',
    '            if op.startswith("tab_"):', '                ' + action, '            elif op == "goto":', '                ' + open + '(step["url"])',
    '            elif op == "reload":', '                ' + (engine === 'playwright' ? 'page.reload()' : 'driver.refresh()'),
    ...(engine === 'playwright' ? ['            elif op and change and change.get("closed"):', '                with PlaywrightAction(page, tabs, change):',
      '                    execute(AssertionDriver(page), step)'] : []),
    '            elif op:', '                execute(AssertionDriver(' + browser + '), step)', '            else:', '                verify(AssertionDriver(' + browser + '), step)',
    '            if change:', '                ' + sync, ...(engine === 'playwright' ? ['    return page'] : []), '',
  ].join('\n');
  const code = initial && JSON.stringify(initial.extensions?.automation) === JSON.stringify(auto) ? template : template.slice(0, start) + src + template.slice(end);
  const base = ctx.fixtures['/projects/' + project.id]?.base_url;
  return file(base ? code.replace(JSON.stringify(base), JSON.stringify(project.base_url)) : code, fname + '_' + engine + '.py', 'text/x-python; charset=utf-8');
}

function seededWorkbook(ctx, project, definitions, tree) {
  const { fixtures, db } = ctx;
  const seeded = fixtures['/projects/' + project.id];
  if (!seeded || project.name !== seeded.name || project.base_url !== seeded.base_url
      || tree.revision !== fixtures['/projects/' + project.id + '/folders']?.revision) return null;
  const initial = fixtures['/cases'].items.filter((item) => item.project_id === project.id);
  if (definitions.length !== initial.length || definitions.some((item) =>
    JSON.stringify(item) !== JSON.stringify(fixtures['/case-definitions/' + project.id + '/' + item.id]?.case))) return null;
  const sameProject = (run) => run.project_id === project.id && run.trigger !== 'login' && run.family !== 'load';
  const runs = (db.runs || []).filter(sameProject);
  const originals = fixtures['/runs'].items.filter(sameProject);
  if (db.runs && (runs.length !== originals.length || runs.some((r) => JSON.stringify(r) !== JSON.stringify(fixtures['/runs/' + r.id])))) return null;
  return fixtureFile(fixtures[ctx.path] || fixtures['/projects/' + project.id + '/report']);
}

async function projectWorkbook(ctx, project, data, selection) {
  const tree = await readCase(ctx, data.prefix + '/folders');
  const clean = !selection && seededWorkbook(ctx, project, data.definitions, tree);
  if (clean) return clean;
  const runs = ctx.db.runs || ctx.fixtures['/runs'].items.map((r) => ctx.fixtures['/runs/' + r.id]);
  const byId = new Map(runs.map((r) => [r.id, r]));
  const records = data.definitions.flatMap((definition, index) => {
    if (selection && !selection.has(definition.id)) return [];
    const item = data.items[index];
    const run = item.current && byId.get(item.state?.scope?.run_id);
    const result = run?.results?.find((row) => row.case_id === definition.id && !row.prerequisite);
    return [{ definition, folder: folderPath(project, tree, definition.id), observations: result ? [result] : [] }];
  });
  const bytes = await functionalWorkbook({ fixtures: ctx.fixtures, project, records, taskId: 'export-' + crypto.randomUUID() });
  return file(bytes, project.id + (ctx.path.endsWith('/report') ? '-测试报告' : '-测试用例') + '.xlsx', MIME);
}

async function runWorkbook(ctx, run) {
  const project = run.report_project || ctx.fixtures['/projects/' + run.project_id] || { id: run.project_id, name: run.project_name };
  const detail = runPage(run);
  const records = new Map();
  // Compacted load runs have an exact count for every selected case plus a bounded detail page.
  for (const [index, row] of (run.load_templates || detail.results || []).entries()) {
    if (row.prerequisite || records.has(row.case_id)) continue;
    let definition = run.report_definitions?.[row.case_id];
    if (!definition) {
      const key = run.project_id + '/' + row.case_id;
      const candidates = [ctx.fixtures['/case-definitions/' + key]?.case, ctx.db.caseLibrary?.definitions[key]?.case];
      definition = candidates.find((item) => item && item.revision === row.case_revision);
    }
    if (!definition) throw new Error('此旧运行缺少用例 ' + row.case_id + ' 的冻结版本，无法生成可追溯的 Excel');
    const record = { definition, observations: [] };
    if (run.load_templates) {
      const { pass: passed, fail: failed, not_run: notRun } = compactCaseCounts(run, index);
      record.status = failed ? 'Fail' : notRun ? 'NotRun' : 'Pass';
      record.aggregate = '浏览器模拟，共 ' + (passed + failed + notRun) + ' 次；通过 ' + passed + '，失败 ' + failed + '，未执行 ' + notRun + '。';
    }
    records.set(row.case_id, record);
  }
  for (const row of detail.results || []) if (!row.prerequisite) records.get(row.case_id).observations.push(row);
  const detailScope = detail.result_page ? '共 ' + run.total + ' 条执行记录，展示前 ' + detail.results.length + ' 条明细；汇总涵盖全部执行。' : '';
  const bytes = await functionalWorkbook({ fixtures: ctx.fixtures, project, records: [...records.values()], taskId: run.id, run, detailScope });
  return file(bytes, run.id + (run.simulated ? '-浏览器模拟' : '') + '.xlsx', MIME);
}

function fixtureFile(value) {
  if (typeof value?.base64 !== 'string') return null;
  return file(Uint8Array.from(atob(value.base64), (letter) => letter.charCodeAt(0)), value.filename, value.media_type);
}

export async function filesApi(ctx) {
  const { path, method, params, fixtures } = ctx;
  const [, resource, pid, section, id, sub] = path.split('/').map(decodeURIComponent);
  if (resource === 'case-plans' && section === 'excel' && method === 'GET') return fixtureFile(fixtures[path]) || notFound('用例设计表不存在');
  if (resource === 'task-reports') {
    const run = findRun(ctx, pid);
    if (!run) {
      const design = Object.entries(fixtures).some(([key, entry]) => key.startsWith('/case-plans/') && entry.test_report?.report_id === pid);
      if (design && section === 'excel' && method === 'GET') return fixtureFile(fixtures[path]) || notFound('设计报告不存在');
      return notFound('执行报告不存在');
    }
    if (!section || section === 'retry') return run.test_report;
    if (method !== 'GET' || !['excel', 'markdown', 'bugs'].includes(section)) return;
    if (run.test_report?.status !== 'ready') return conflict('报告尚未就绪');
    const captured = fixtureFile(fixtures[path]);
    if (captured) return captured;
    if (section === 'excel') return runWorkbook(ctx, run);
    const sampled = Boolean(run.load_templates);
    const { results } = runPage(run);
    const rows = (results || []).filter((row) => !row.prerequisite).map((row) => reportRow(row));
    return reportFile(run.simulated ? pid + '-浏览器模拟' : pid, rows, section, sampled ? run : undefined);
  }
  if (resource === 'projects' && method === 'GET' && (section === 'report' || section === 'postman' || (section === 'folders' && id === 'export') || (section === 'cases' && sub === 'script'))) {
    const project = await readCase(ctx, '/projects/' + encodeURIComponent(pid));
    if (!project || project instanceof Response) return notFound('项目不存在');
    const { prefix, definitions, items } = await projectData(ctx, pid);
    if (sub === 'script') {
      const row = definitions.find((item) => item.id === id);
      return row ? uiScript(ctx, project, row, params.get('engine') || 'playwright') : notFound('用例不存在');
    }
    if (section === 'postman') {
      const format = params.get('part') || 'collection';
      if (!['environment', 'collection'].includes(format)) return notFound('Postman 导出类型无效');
      const data = postman(definitions, project, fixtures[prefix + '/postman?part=collection'], format);
      return file(JSON.stringify(data, null, 2), pid + '.postman_' + format + '.json', mimeJson);
    }
    const selection = await folderSelection(ctx, prefix);
    if (selection instanceof Response) return selection;
    if (params.get('part') !== 'markdown') return projectWorkbook(ctx, project, { prefix, definitions, items }, selection);
    const rows = definitions.flatMap((row, i) => !selection || selection.has(row.id) ? [reportRow(row, items[i])] : []);
    return reportFile(pid + (section === 'report' ? '-测试报告' : '-测试用例'), rows, 'markdown');
  }
  if (resource === 'diagnostics' && method === 'POST') {
    const reason = 'GitHub Pages 在浏览器中运行，未连接本机 autoSkill 服务。';
    const kind = ctx.body.kind;
    const result = kind === 'engines' ? { engines: (fixtures['/engines']?.engines || []).filter((e) => !ctx.body.engine || e.id === ctx.body.engine)
      .map((e) => ({ ...e, available: false, version: null, diagnosis: reason })) }
      : kind === 'doctor' ? { ok: false, platform: navigator.platform, workspace: '浏览器示例数据', missing: ['本机执行器连接'], hint: reason }
        : { ok: false, files: 0, missing: ['本机离线包清单'] };
    return { id: crypto.randomUUID(), created_at: new Date().toISOString(), status: 'Completed', result };
  }
  if (['project-materials', 'shares'].includes(resource)) return materialsApi(ctx);
  if (resource === 'workflows' && method === 'GET' && !pid) return fixtures['/workflows'] || { items: [], total: 0 };
  if (resource === 'conversations' || resource === 'assets' || resource === 'knowledge') return fixtures[path];
  return undefined;
}

const storedExports = new Map();
const bytesOf = (text) => new TextEncoder().encode(text);
const hex = (bytes) => [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
async function digest(bytes) { return hex(await crypto.subtle.digest('SHA-256', bytes)); }
function archiveLink(bytes, name, note) {
  const path = '/project-materials/download/' + crypto.randomUUID();
  storedExports.set(path, { bytes, name });
  return { download_url: '/api' + path, bytes: bytes.length, archive: name, note };
}

const caseFileKinds = new Map([['api-cases.json', 'api'], ['functional-cases.json', 'ui']]);

async function updatedCaseFile(ctx, pid, item, definitions) {
  const source = fixtureFile(ctx.fixtures[item.download_url.slice(4)]);
  if (!source) return null;
  const original = await source.json();
  const saved = new Map(definitions.map((row) => [row.id, row]));
  const rows = [];
  for (const row of original) {
    if (saved.has(row.id)) {
      rows.push(saved.get(row.id));
      saved.delete(row.id);
    } else if (!ctx.fixtures['/case-definitions/' + pid + '/' + row.id]) {
      rows.push(row);
    }
  }
  rows.push(...saved.values());
  return JSON.stringify(rows) === JSON.stringify(original) ? null : bytesOf(JSON.stringify(rows, null, 2) + '\n');
}

async function materialIndex(ctx, pid) {
  const key = '/project-materials/' + encodeURIComponent(pid);
  const base = ctx.fixtures[key];
  if (!base) return null;
  const project = await readCase(ctx, '/projects/' + encodeURIComponent(pid));
  const library = ctx.db.caseLibrary;
  const definitions = library.items.filter((row) => row.project_id === pid)
    .map((row) => library.definitions[pid + '/' + row.case_id].case);
  const edited = ctx.db.knowledgeFiles?.[pid] || {};
  const items = base.items.filter((item) => item.category !== 'knowledge' || !Object.hasOwn(edited, item.name));
  const files = new Map();
  for (const [index, item] of items.entries()) {
    const kind = item.category === 'testcases' && caseFileKinds.get(item.name);
    if (!kind) continue;
    const bytes = await updatedCaseFile(ctx, pid, item, definitions.filter((row) => row.kind === kind));
    if (bytes) {
      files.set(item.id, file(bytes, item.name, mimeJson));
      items[index] = { ...item, bytes: bytes.length, sha256: await digest(bytes) };
    }
  }
  for (const [name, record] of Object.entries(edited)) {
    if (record === null) continue;
    const id = await digest(bytesOf('knowledge/' + name));
    files.set(id, file(record.content, name.split('/').at(-1), 'text/markdown; charset=utf-8'));
    items.push({ id, name, category: 'knowledge', text: true, bytes: bytesOf(record.content).length, sha256: record.hash,
      download_url: '/api' + key + '/files/' + id, reference: 'projects/' + pid + '/knowledge/' + name });
  }
  return { index: { ...base, items, project }, files, definitions };
}

function materialFile(ctx, item, files) {
  if (!item) return notFound('项目文件不存在');
  return files.get(item.id) || fixtureFile(ctx.fixtures[item.download_url.slice(4)]) || notFound('没有找到文件原件');
}

async function projectShare(ctx) {
  const pid = ctx.body.project_ids?.[0];
  const data = await projectData(ctx, pid);
  const lib = ctx.db.caseLibrary;
  const project = await readCase(ctx, '/projects/' + encodeURIComponent(pid));
  if (!project || project instanceof Response) return notFound('项目不存在');
  const attachments = {};
  for (const section of ['definitions', 'details']) {
    attachments[section] = Object.fromEntries(Object.entries(lib[section]).filter(([key]) => key.startsWith(pid + '/')));
  }
  const snapshot = { format: 'autoSkill-pages-project', version: 1,
    project: { ...project, credentials: Object.fromEntries(Object.keys(project.credentials || {}).map((key) => [key, null])) },
    items: data.items, folders: lib.folders[pid], ...attachments, knowledge: ctx.db.knowledgeFiles?.[pid] || {} };
  if (ctx.body.full) {
    snapshot.runs = (ctx.db.runs || []).filter((run) => run.project_id === pid);
    snapshot.tasks = (ctx.db.tasks || []).filter((task) => task.project_id === pid);
  }
  if (ctx.body.include_secrets) {
    if (!ctx.body.passphrase) throw new Error('请填写凭据加密口令');
    const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
    const key = await shareKey(ctx.body.passphrase, salt);
    const values = lib.secrets.filter((entry) => Object.hasOwn(project.credentials || {}, entry.name));
    const encrypted = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, bytesOf(JSON.stringify(values)));
    snapshot.secrets = { salt: [...salt], iv: [...iv], ciphertext: [...new Uint8Array(encrypted)] };
  }
  const entries = [['preview-project.json', JSON.stringify(snapshot)], ...data.definitions.map((row) => ['cases/' + row.id + '.json', JSON.stringify(row, null, 2)])];
  return archiveLink(previewZip(entries), pid + '-preview.zip', '浏览器示例项目，可在预览的分享页重新导入。');
}

async function shareKey(passphrase, salt) {
  const raw = await crypto.subtle.importKey('raw', bytesOf(passphrase), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey({ name: 'PBKDF2', salt, iterations: 240000, hash: 'SHA-256' }, raw, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
}

async function importShare(ctx) {
  const headers = new Headers(ctx.headers);
  let bytes, overwrite, password;
  if (ctx.path === '/shares/upload') {
    const buffer = ctx.body instanceof Blob ? await ctx.body.arrayBuffer() : ctx.body;
    if (!(buffer instanceof ArrayBuffer) && !(buffer instanceof Uint8Array)) throw new Error('请先选择预览 ZIP 文件');
    bytes = new Uint8Array(buffer);
    overwrite = headers.get('x-autoskill-overwrite') === '1';
    password = new TextDecoder().decode(Uint8Array.from(atob(headers.get('x-autoskill-passphrase') || ''), (c) => c.charCodeAt(0)));
  } else {
    const record = [...storedExports.values()].findLast((entry) => entry.name === ctx.body.source);
    if (!record) throw new Error('请使用文件选择框上传项目分享 ZIP，或填写本次预览已导出的文件名');
    bytes = record.bytes;
    overwrite = ctx.body.overwrite;
    password = ctx.body.passphrase;
  }
  const src = JSON.parse(new TextDecoder().decode(readPreviewEntry(bytes, 'preview-project.json')), (key, value) => {
    if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('分享包包含无效字段');
    return value;
  });
  const pid = src.project?.id;
  if (src.format !== 'autoSkill-pages-project' || src.version !== 1 || !/^[\w-]+$/.test(pid || '') || !Array.isArray(src.items)
      || !src.items.every((row) => row.project_id === pid && typeof row.case_id === 'string')
      || !src.folders || !Array.isArray(src.folders.folders) || !src.folders.assignments || !src.definitions || !src.details
      || !src.items.every((r) => src.definitions[pid + '/' + r.case_id]?.case?.id === r.case_id && src.details[pid + '/' + r.case_id])
      || (src.runs !== undefined && (!Array.isArray(src.runs) || src.runs.some((r) => !r?.id || r.project_id !== pid || !Array.isArray(r.results))))) throw new Error('示例项目分享包内容无效');
  if (src.tasks !== undefined) validateTasks(src.tasks, new Set([pid]));
  await readCase(ctx, '/projects');
  const library = ctx.db.caseLibrary;
  if (library.projects.some((project) => project.id === pid) && !overwrite) return { imported: [], skipped: [{ id: pid, reason: '项目已存在' }], next: '勾选替换后可重新导入。' };
  if (!ctx.fixtures['/projects/' + pid]) throw new Error('当前预览无法加载此项目的配套样例资源');
  let secrets = [];
  if (src.secrets) {
    const sealed = src.secrets;
    try {
      const key = await shareKey(password, new Uint8Array(sealed.salt));
      const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: new Uint8Array(sealed.iv) }, key, new Uint8Array(sealed.ciphertext));
      secrets = JSON.parse(new TextDecoder().decode(plain));
      if (!Array.isArray(secrets) || !secrets.every((v) => typeof v.name === 'string' && typeof v.value === 'string')) throw new Error('无效凭据');
    } catch { throw new Error('分享包凭据解密失败，请检查口令'); }
  }
  library.projects = [...library.projects.filter((p) => p.id !== pid), src.project];
  library.items = [...library.items.filter((r) => r.project_id !== pid), ...src.items];
  library.folders[pid] = src.folders;
  for (const field of ['definitions', 'details']) {
    for (const key of Object.keys(library[field])) if (key.startsWith(pid + '/')) delete library[field][key];
    for (const row of src.items) {
      const key = pid + '/' + row.case_id;
      library[field][key] = src[field][key];
    }
  }
  for (const entry of secrets) library.secrets = [...library.secrets.filter((v) => v.name !== entry.name), entry];
  (ctx.db.knowledgeFiles ||= {})[pid] = src.knowledge || {};
  if (Array.isArray(src.runs)) ctx.db.runs = [...(ctx.db.runs || []).filter((r) => r.project_id !== pid), ...src.runs.filter((r) => r.project_id === pid)];
  if (Array.isArray(src.tasks)) ctx.db.tasks = [...(ctx.db.tasks || []).filter((t) => t.project_id !== pid), ...src.tasks.filter((t) => t.project_id === pid)];
  return { imported: [pid], skipped: [], next: '示例项目已恢复到当前浏览器。' };
}

async function materialsApi(ctx) {
  const { path, method, params, body, db, fixtures } = ctx;
  const [, resource, pid, section, id] = path.split('/').map(decodeURIComponent);
  if (resource === 'shares') {
    if (method !== 'POST') return;
    if (pid === 'export') return projectShare(ctx);
    if (pid === 'import' || pid === 'upload') return importShare(ctx);
    return;
  }
  if (pid === 'download') {
    const archive = storedExports.get(path);
    return archive ? file(archive.bytes, archive.name, 'application/zip') : notFound('下载已过期，请重新导出');
  }
  if (!pid) return { projects: await readCase(ctx, '/projects') };
  const materials = await materialIndex(ctx, pid);
  if (!materials) return notFound('项目不存在');
  const { index, files, definitions } = materials;
  if (!section && method === 'GET') return { ...index, workspace_identity: workspaceIdentity(ctx) };
  if (section === 'files' && method === 'GET') return materialFile(ctx, index.items.find((item) => item.id === id), files);
  if (section === 'export' && method === 'POST') {
    const entries = [];
    for (const item of index.items) {
      const response = materialFile(ctx, item, files);
      if (!response.ok) return response;
      entries.push([item.category + '/' + item.name, new Uint8Array(await response.arrayBuffer())]);
    }
    entries.push(['cases.json', JSON.stringify(definitions, null, 2)]);
    return archiveLink(previewZip(entries), pid + '-materials.zip', '项目资料包含当前浏览器保存的知识文件和用例。');
  }
  if (section === 'knowledge') {
    if (method === 'DELETE') {
      const changed = workspaceConflict(ctx);
      if (changed) return changed;
    }
    const name = method === 'GET' ? params.get('file') : body.file;
    if (!name || name.split(/[\\/]/).some((part) => !part || part === '.' || part === '..' || ['__proto__', 'constructor', 'prototype'].includes(part))) throw new Error('请输入有效的知识文件名');
    const item = index.items.find((entry) => entry.category === 'knowledge' && entry.name === name);
    if (method === 'GET') {
      const response = materialFile(ctx, item, files);
      return response.ok ? { file: name, content: await response.text(), content_revision: item.sha256 } : response;
    }
    if ((item?.sha256 || null) !== (body.expected_hash || null)) return conflict('文件已更新，请重新打开');
    const knowledge = ((db.knowledgeFiles ||= {})[pid] ||= {});
    if (method === 'PUT') {
      if (typeof body.content !== 'string') throw new Error('缺少知识文件内容');
      knowledge[name] = { content: body.content, hash: await digest(bytesOf(body.content)) };
      return { file: name, content_revision: knowledge[name].hash };
    }
    if (method === 'DELETE' && item) { knowledge[name] = null; return { deleted: name }; }
    return notFound('知识文件不存在');
  }
  return fixtures[path];
}
