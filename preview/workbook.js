// Render the native functional-case contract from the browser's frozen definitions and results.
import { displayLines, workbookBytes } from './workbook-xml.js';

const labels = { Pass: '通过', Fail: '失败', NotRun: '未执行' };
const chapter = /^\s*(?:第[一二三四五六七八九十百\d]+[章节]\s*|\d+(?:\.\d+)*(?:[、.)）]\s*|\s+))/;
const numbered = /^(\d+)\.\s+(.+)$/s;
const text = (value) => value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value);
const unique = (values) => [...new Set(values.filter(Boolean))];
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const metadata = (definition) => definition?.extensions?.test_case_document || {};
const businessPath = (doc) => Array.isArray(doc.business_path) && doc.business_path.length && doc.business_path.every((part) => typeof part === 'string' && part.trim()) ? doc.business_path : null;
const rawPath = (row, project) => businessPath(row.document) || [project.name, row.definition.module && row.definition.module !== '未分组' ? row.definition.module : '【待确认：功能项】'];
const numberingScope = 'FT 仅在本报告快照内编号；跨报告按原始标识与定义哈希关联。';
const technicalTitle = /^(?:(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)|查询|提交|修改|删除)\s+(?:https?:\/\/|\/)/i;
const missingSteps = '【待确认：功能测试步骤尚未提供】；自动化动作和接口请求保留在执行明细。';
const missingExpected = '【待确认：有来源支持的预期结果】；技术断言不是已编写的功能测试预期。';

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}

async function contentHash(value) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(value)));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function folderPath(project, tree, id) {
  if (!Object.hasOwn(tree.assignments, id)) throw new Error('用例文件夹归属已变化，请刷新后重试');
  const nodes = new Map(tree.folders.map((folder) => [folder.id, folder]));
  const names = [], visited = new Set();
  for (let parent = tree.assignments[id]; parent != null;) {
    const node = nodes.get(parent);
    if (!node || visited.has(parent)) throw new Error('文件夹层级无效');
    visited.add(parent);
    names.unshift(node.name);
    parent = node.parent_id;
  }
  return [project.name, ...(names.length ? names : ['【项目根目录用例】'])];
}

async function prepare(records, project, fixtures) {
  const sourceCases = fixtures['/workbook-sources/' + project.id]?.cases || {};
  for (const row of records) {
    const original = row.definition;
    const seed = fixtures['/case-definitions/' + project.id + '/' + original.id]?.case;
    row.seed = seed;
    row.source = sourceCases[original.id] || {};
    const originalHash = equal(original, seed) && row.source.definition_hash || await contentHash(original);
    row.originalHash = originalHash;
    const explicit = original.extensions?.test_case_document;
    row.document = explicit || { ...(row.source.document || {}) };
    if (!explicit && seed) {
      for (const [key, raw] of [['description','title'], ['preconditions','preconditions'], ['steps','steps'], ['expected','expected']]) {
        if (!equal(seed[raw], original[raw])) delete row.document[key];
      }
    }
    row.reportDefinition = { ...original, extensions: { ...original.extensions, test_case_document: row.document } };
    row.version = await contentHash(row.reportDefinition);
    row.originalPath = rawPath(row, project);
    row.path = [...(!businessPath(row.document) && row.folder ? row.folder : row.originalPath)];
    row.notes = [];
    if (row.folder) row.notes.push(businessPath(row.document) ? '业务分组取自功能设计；本次文件夹组织路径见编号映射。' : '按本次导出时的文件夹完整路径分类，原始业务归属见编号映射。');
    else if (!businessPath(row.document)) row.notes.push('按已保存的项目和模块分类。');
  }
  const roots = new Set(records.map((row) => row.path[0]));
  if (roots.size > 1) for (const row of records) if (row.path[0] !== project.name) row.path.unshift(project.name);
  const parents = new Set(records.flatMap((row) => row.path.slice(1).map((_, i) => JSON.stringify(row.path.slice(0, i + 1)))));
  const paths = new Map();
  for (const row of records) {
    if (parents.has(JSON.stringify(row.path))) row.path.push(row.folder ? '【本级用例】' : '【待确认：功能项】');
    const key = JSON.stringify(row.path);
    if (!paths.has(key)) paths.set(key, { choices: new Set(), rows: [] });
    const group = paths.get(key);
    group.rows.push(row);
    let prefix = row.document.prefix;
    if (!row.definition.extensions?.test_case_document && !businessPath(row.document) && prefix) {
      const originalTree = fixtures['/projects/' + project.id + '/folders'];
      const oldLeaf = originalTree && Object.hasOwn(originalTree.assignments, row.definition.id) && folderPath(project, originalTree, row.definition.id).at(-1);
      if (oldLeaf !== row.path.at(-1)) prefix = '';
    }
    if (typeof prefix === 'string' && /^[A-Z0-9]+$/.test(prefix) && !['【本级用例】', '【项目根目录用例】'].includes(row.path.at(-1))) group.choices.add(prefix);
  }
  const occurrences = new Map();
  for (const group of paths.values()) for (const choice of group.choices) occurrences.set(choice, (occurrences.get(choice) || 0) + 1);
  for (const group of paths.values()) {
    const candidate = [...group.choices][0];
    const declared = group.choices.size === 1 && occurrences.get(candidate) === 1;
    const prefix = declared ? candidate : 'WQR' + (await contentHash(group.rows[0].path)).slice(0, 12).toUpperCase();
    group.rows.forEach((row, index) => {
      row.ft = 'FT-' + prefix + '-' + String(index + 1).padStart(3, '0');
      if (!declared) row.notes.push('【待确认：功能项拼音缩写】；WQR 为本报告临时显示标识。');
    });
  }
}

function parts(value) {
  if (Array.isArray(value)) return value.map(text).filter((line) => line.trim());
  if (typeof value !== 'string' || !value.trim()) return [];
  const lines = value.split(/\r\n|\r|\n/);
  if (!lines.some((line) => numbered.test(line))) return lines.filter((line) => line.trim());
  const groups = [];
  for (const line of lines) {
    if (numbered.test(line) || !groups.length) groups.push(line);
    else groups[groups.length - 1] += '\n' + line;
  }
  return groups;
}

function procedure(row, data) {
  const doc = row.document, seedDoc = metadata(row.seed);
  const unchanged = row.source.steps && row.source.expected && equal([metadata(row.definition).steps, metadata(row.definition).expected, row.definition.steps, row.definition.expected],
    [seedDoc.steps, seedDoc.expected, row.seed?.steps, row.seed?.expected]);
  if (unchanged) {
    const replaceId = (s) => String(s).replace(/FT-[A-Z0-9]+-\d{3,}(?=\/)/g, row.ft);
    for (const entry of row.source.data || []) data.push([replaceId(entry.id), entry.value]);
    return { steps: replaceId(row.source.steps), expected: replaceId(row.source.expected), mapping: row.source.step_mapping };
  }
  const raw = parts(doc.steps || row.definition.steps);
  if (!doc.steps && raw.every((step) => /^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?:https?:\/\/|\/)/i.test(step.replace(/^\d+\.\s+/, '')))) raw.splice(0, raw.length, missingSteps);
  if (!raw.length) raw.push(missingSteps);
  const mapping = {}, steps = [], leading = [];
  raw.forEach((original, index) => {
    let value = original.replace(/^\d+\.\s+/, '');
    if (/^等待\s*\d+(?:\.\d+)?\s*(?:毫秒|秒|分钟|ms|s)\s*$/.test(value)) {
      if (steps.length) steps[steps.length - 1] += '（' + value + '）';
      else leading.push(value);
    } else {
      value = value.replace(/^等待(出现|消失)/, '检查$1').replace(/点击(搜索|新增|提交)(?:按钮)?(?=$|[，。；、\s])/g, '点击【$1】');
      steps.push([...leading, value].join('，'));
      leading.length = 0;
    }
    mapping[index + 1] = Math.max(steps.length, 1);
  });
  if (!steps.length) steps.push('检查等待条件【待确认：业务检查动作】；原始时序：' + leading.join('，'));
  let value = doc.expected || row.definition.expected;
  if (!doc.expected && row.definition.kind === 'api' && /^HTTP\s+\d{3}(?:，并满足录制到的字段与结构)?$/.test(value || '')) value = null;
  const rawExpected = Array.isArray(value) ? value.join('\n') : value || missingExpected;
  const expected = new Map();
  for (const original of (rawExpected.split(/\r\n|\r|\n/).some((line) => numbered.test(line)) ? parts(rawExpected) : [rawExpected])) {
    const match = original.match(numbered);
    const target = match && mapping[match[1]] || steps.length;
    let line = match ? match[2] : original;
    if (match && !mapping[match[1]]) line = '【待确认：原预期编号 ' + match[1] + ' 与步骤的对应关系】' + line;
    expected.set(target, [...(expected.get(target) || []), line]);
  }
  function numberedText(entries, field) {
    return entries.map(([index, original]) => {
      let value = original;
      if (/[\r\n]/.test(value) || value.length > 4000) {
        const key = row.ft + '/' + field + '/' + index;
        data.push([key, value]);
        value = value.split(/[\r\n]/)[0].slice(0, 160) + '（完整原文见「测试数据」：' + key + '）';
      }
      return index + '. ' + value;
    }).join('\n');
  }
  let stepText = numberedText(steps.map((line, i) => [i + 1, line]), '步骤');
  let expectedText = numberedText([...expected.entries()].sort(([a], [b]) => a - b).map(([i, lines]) => [i, lines.join('；')]), '预期');
  if (displayLines(stepText, 52) > 20 || displayLines(expectedText, 52) > 20) {
    data.push([row.ft + '/完整步骤', stepText], [row.ft + '/完整预期', expectedText]);
    stepText = '1. 按「测试数据」' + row.ft + '/完整步骤 执行本用例的完整操作链。';
    expectedText = '1. 按「测试数据」' + row.ft + '/完整预期 核对对应步骤的原始判据。';
  }
  return { steps: stepText, expected: expectedText, mapping };
}

function makeTable(name, headers, widths) {
  return { name, widths, rows: [{ cells: headers, header: true }] };
}

function append(table, cells) { table.rows.push({ cells }); }

function chunks(value, width) {
  const out = [];
  let current = '', lines = 1, column = 0;
  for (const char of String(value)) {
    if (current.length >= 950 || lines >= 17) { out.push(current); current = ''; lines = 1; column = 0; }
    current += char;
    column++;
    if (char === '\n' || column >= width / 1.8) { lines++; column = 0; }
  }
  if (current) out.push(current);
  return out;
}

function actualStatus(result) {
  return { pass: 'Pass', fail: 'Fail', not_run: 'NotRun' }[result?.state?.outcome] || (Object.hasOwn(labels, result?.status) ? result.status : 'NotRun');
}

function screenshotReferences(result) {
  const records = [result, result.detail, ...(result.detail?.attempts || [])].filter(Boolean);
  return unique(records.flatMap((r) => [].concat(r.screenshot || r.screenshots || []).map((s) => '运行 ' + result.run_id + '；截图来源：' + text(s))));
}

function verify(cases, mapping, intended, counts) {
  const rows = cases.rows.filter((r) => r.status);
  const ids = new Set(), seen = new Set();
  if (rows.length !== intended || mapping.rows.length !== intended + 1 || cases.widths.length !== 14) throw new Error('工作簿用例范围或列数不匹配');
  for (const row of rows) {
    const [id, name, trace, , , steps, expected] = row.cells;
    const numbers = (value) => value.split('\n').map((line) => Number(line.match(/^(\d+)\. \S/)?.[1]));
    const actions = numbers(steps), checks = numbers(expected);
    if (ids.has(id) || !/^FT-[A-Z0-9]+-\d{3,}$/.test(id) || row.cells.length !== 14 || row.cells.slice(0, 8).some((value) => !String(value ?? '').trim())
      || name !== row.path.map((part) => part.replace(chapter, '').trim()).join('-') || trace !== row.path.at(-1)
      || !actions.every((v, i) => v === i + 1) || !checks.every((v, i) => actions.includes(v) && (!i || checks[i - 1] < v))
      || row.cells[9] !== labels[row.status]) throw new Error('工作簿用例结构或结果不匹配：' + id);
    ids.add(id);
  }
  for (const { cells } of mapping.rows.slice(1)) {
    if (!ids.has(cells[0]) || seen.has(cells[0]) || !cells[1] || !/^[a-f0-9]{64}$/.test(cells[2])) throw new Error('工作簿编号溯源不完整');
    seen.add(cells[0]);
  }
  if (counts && counts.total !== counts.passed + counts.failed + counts.not_run) throw new Error('执行汇总数量不匹配');
}

function dashboard(project, taskId, run, detailScope, records, counts) {
  const dt = run?.finished_at || run?.created_at || new Date().toISOString();
  const sheet = { name: '概览', widths: Array(12).fill(13), rows: [], merges: [], chartModules: 0 };
  function line(values = {}, style = 0, height = 19) {
    const cells = Array(12).fill('');
    for (const [address, value] of Object.entries(values)) cells[address.charCodeAt(0) - 65] = value;
    sheet.rows.push({ cells, style, height });
  }
  function merged(from, to, row) { sheet.merges.push(from + row + ':' + to + row); }
  line({ A: '测试报告概览' }, 10, 36); merged('A','L',1);
  for (const [label, value] of [['项目', project.name], ['测试范围', run?.simulated ? '浏览器模拟执行报告' : '当前导出范围'], ['被测地址', project.base_url], ['记录时间', dt]]) {
    line({ A: label, D: value }, 11); const i = sheet.rows.length;
    merged('A','C',i); merged('D','L',i); sheet.rows.at(-1).styles = { 3: 12 };
  }
  line({}, 12, 8);
  const judged = counts.Pass + counts.Fail;
  line({ A:'用例总数', D:'通过', G:'失败', J:'通过率' }, 13, 22);
  line({ A:records.length, D:counts.Pass, G:counts.Fail, J:judged ? counts.Pass / judged : '—' }, 14, 37);
  sheet.rows.at(-1).styles = { 3:15, 6:16, 9:judged ? 17 : 14 };
  for (const i of [7,8]) for (const pair of [['A','C'],['D','F'],['G','I'],['J','L']]) merged(...pair, i);
  const modules = new Map();
  for (const record of records) {
    const declared = record.definition.module;
    const inferred = record.originalPath[1];
    const name = declared && declared !== '未分组' ? declared : inferred && !inferred.startsWith('【待确认') ? inferred : '未分组';
    if (!modules.has(name)) modules.set(name, { Pass:0, Fail:0, NotRun:0, total:0 });
    const values = modules.get(name);
    values[record.conclusion]++;
    values.total++;
  }
  const ordered = [...modules].sort(([nameA, a], [nameB,b]) => b.total - a.total || (nameA < nameB ? -1 : Number(nameA > nameB)));
  sheet.chartModules = Math.min(8, ordered.length);
  line({}, 12, 8);
  line({ A:'执行结果分布', F: ordered.length > 8 ? '业务模块结果（前 8 个）' : '业务模块结果' }, 13);
  merged('A','E',10); merged('F','L',10);
  for (let n = 11; n <= 22; n++) line(n === 14 && !records.length ? { A:'暂无测试用例' } : {}, 12);
  if (!records.length) merged('A','L',14);
  line({ A:'通过率 = 通过 ÷（通过 + 失败）' }, 12); merged('A','E',23);
  line({ A:'执行结果', B:'数量', F:'业务模块', H:'未执行', I:'通过', J:'失败', K:'合计' }, 13, 24);
  merged('F','G',24);
  const statuses = ['NotRun','Pass','Fail'];
  for (let i = 0; i < Math.max(ordered.length, 3); i++) {
    const item = ordered[i];
    const cells = {};
    if (i < 3) { cells.A = labels[statuses[i]]; cells.B = counts[statuses[i]]; }
    if (item) { const [name, values] = item; Object.assign(cells, { F:name, H:values.NotRun, I:values.Pass, J:values.Fail, K:values.total }); }
    line(cells, 12, 24); merged('F','G',25 + i);
  }
  line({}, 12, 12);
  for (const [key, value] of [['导出标识', taskId], ...(run ? [['执行总数',run.total],['执行通过',run.passed],['执行失败',run.failed],['执行未执行',run.not_run]] : []), ...(detailScope ? [['执行明细范围', detailScope]] : [])]) {
    line({ A:key, D:value }, 11); const i = sheet.rows.length; merged('A','C',i); merged('D','L',i); sheet.rows.at(-1).styles = { 3:12 };
  }
  return sheet;
}

export async function functionalWorkbook({ project, records, fixtures, taskId, run, detailScope }) {
  const template = fixtures['/workbook-template'];
  if (!template?.headers || !template?.widths || !template?.fills || !template?.colors) throw new Error('缺少功能测试用例模板，请刷新页面');
  await prepare(records, project, fixtures);
  const cases = makeTable('功能测试用例', template.headers, template.widths);
  const mapping = makeTable('编号映射', ['本报告 FT 标识','原始用例标识','冻结定义 SHA256','本报告业务路径','来源业务路径','任务标识','关联运行','原步骤号到展示步骤号','编号范围','本次导出的文件夹组织路径','原始编辑定义 SHA256'], [30,28,68,60,60,30,60,40,65,65,68]);
  const execution = makeTable('执行明细', ['原始用例标识','本报告 FT 标识','关联运行','结论','实际结果','原因','执行时间','验证环境','轮次','证据和技术明细'], [30,30,48,16,60,60,34,28,14,85]);
  const sources = makeTable('来源与冻结定义', ['本报告 FT 标识','原始用例标识','冻结定义 SHA256','定义片段顺序','原始定义（按顺序拼接）'], [30,28,68,18,110]);
  const notes = makeTable('长文本', ['原文引用','片段顺序','完整内容（按顺序拼接）'], [48,14,110]);
  const data = [];
  const group = (name, depth, note = '') => cases.rows.push({ cells: [name, ...Array(11).fill(''), note, ''], depth, note: 'autoskill:business-level=' + depth });
  function shorten(value, label, width) {
    if (displayLines(value, width) <= 20 && String(value ?? '').length <= 32000) return value;
    chunks(value, 110).forEach((part, i) => append(notes, [label, i + 1, part]));
    return String(value).split(/\r\n|\r|\n/, 1)[0].slice(0, 100) + '\n全文见「长文本」：' + label;
  }
  const root = { children: new Map(), rows: [] };
  for (const row of records) {
    let node = root;
    for (const part of row.path) {
      if (!node.children.has(part)) node.children.set(part, { children: new Map(), rows: [] });
      node = node.children.get(part);
    }
    node.rows.push(row);
  }
  const counts = { Pass: 0, Fail: 0, NotRun: 0 };
  function writeRow(row) {
    const { definition: def, observations, seed, source } = row, doc = row.document, seedDoc = metadata(seed);
    const procedureData = procedure(row, data);
    const sourceValue = (field, current, previous, fallback) => equal([doc[field], current], [seedDoc[field], previous]) && source[field] || doc[field] || fallback;
    const description = sourceValue('description', def.title, seed?.title, !technicalTitle.test(def.title || '') && def.title || '【待确认：具体功能测试目标】');
    const preconditions = sourceValue('preconditions', def.preconditions, seed?.preconditions, def.preconditions || '【待确认：必要前置条件】');
    const author = doc.author || (!def.extensions?.test_case_document && source.author) || '【待确认：用例编制人】';
    const statuses = observations.map(actualStatus);
    const status = row.status || (statuses.includes('Fail') ? 'Fail' : statuses.at(-1) || 'NotRun');
    counts[status]++;
    row.conclusion = status;
    const runIds = unique([...(run ? [run.id] : []), ...observations.map((obs) => obs.run_id)]);
    const environments = unique([...(run?.simulated ? ['浏览器模拟'] : []), ...observations.map((obs) => obs.simulated ? '浏览器模拟' : obs.engine),
      ...(observations.length && project.base_url ? [project.base_url] : [])]);
    const actual = row.aggregate || observations.map((obs) => (obs.run_id ? obs.run_id + '：' : '') + labels[actualStatus(obs)] + '　' + unique([text(obs.actual), text(obs.reason)]).join('；')).join('\n');
    const gaps = unique([doc.remarks || (!def.extensions?.test_case_document && source.remarks), ...row.notes, ...[description, preconditions, procedureData.steps, procedureData.expected, author].flatMap((field) => String(field).match(/【待(?:确认|补充)[^】]*】/g) || [])]);
    const remarks = ['原始标识：' + def.id + '；冻结定义：' + row.version, ...gaps].join('\n');
    const cells = [row.ft, row.path.map((part) => part.replace(chapter, '').trim()).join('-'), row.path.at(-1), description, preconditions,
      procedureData.steps, procedureData.expected, author, actual, labels[status], doc.executor || '', environments.join('\n'), remarks,
      unique(observations.flatMap(screenshotReferences)).join('\n')];
    for (const i of [3,4,8,11,12,13]) cells[i] = shorten(cells[i], row.ft + '/' + template.headers[i], template.widths[i]);
    cases.rows.push({ cells, status, path: row.path, note: '原始用例标识：' + def.id + '\n定义哈希：' + row.version });
    append(mapping, [row.ft, def.id, row.version, text(row.path), text(row.originalPath), taskId, text(runIds), text(procedureData.mapping), numberingScope, text(row.folder || []), row.originalHash]);
    chunks(canonical(row.reportDefinition), 110).forEach((part, i) => append(sources, [row.ft, def.id, row.version, i + 1, part]));
    for (const obs of observations) {
      const key = (obs.run_id || taskId) + '/' + (obs.id || def.id);
      append(execution, [def.id, row.ft, obs.run_id, labels[actualStatus(obs)], shorten(text(obs.actual), key + '/实际结果', 60),
        shorten(text(obs.reason), key + '/原因', 60), obs.started_at, obs.simulated ? '浏览器模拟' : obs.engine,
        obs.detail?.load_iteration, shorten(text(obs.detail), key + '/执行明细', 85)]);
    }
  }
  function writeTree(node, depth) {
    for (const [name, child] of node.children) {
      group(name, depth);
      child.rows.forEach(writeRow);
      writeTree(child, depth + 1);
    }
  }
  writeTree(root, 1);
  if (!records.length) group(project.name, 1, '本次导出范围未包含用例。');
  const overview = dashboard(project, taskId, run, detailScope, records, counts);
  const sheets = [overview, cases, mapping, execution, sources];
  if (data.length) {
    const full = makeTable('测试数据', ['原文引用','原文片段（按片段顺序直接拼接）','片段顺序'], [50,110,12]);
    for (const [key, value] of data) chunks(value, 110).forEach((part, i) => append(full, [key, part, i + 1]));
    sheets.push(full);
  }
  if (notes.rows.length > 1) sheets.push(notes);
  verify(cases, mapping, records.length, run);
  return workbookBytes(template, sheets);
}
