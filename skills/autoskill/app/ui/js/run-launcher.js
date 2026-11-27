// Keep request receipts separate from execution results and across ordinary page refreshes.
import { h, icon, replace, api, href, go, toast } from './core.js';
import { preferenceDataId, preferredEngine, preferredHeaded, onPreferences, ensurePreferences } from './preferences.js';
import { startOperation, operationRequest, classifyStartError } from './start-operation.js';

let receipts = [];
let dataId;
const storageKey = () => 'autoskill.launch-receipts.' + preferenceDataId();

function restore() {
  if (dataId === preferenceDataId()) return;
  dataId = preferenceDataId();
  try { receipts = JSON.parse(sessionStorage.getItem(storageKey()) || '[]'); } catch { receipts = []; }
  if (!Array.isArray(receipts)) receipts = [];
  receipts = receipts.filter((receipt) => receipt && Array.isArray(receipt.rows));
  for (const receipt of receipts) {
    if (!receipt.pending) continue;
    receipt.pending = false;
    receipt.rows = receipt.rows.map((row) => ({ ...row, state: 'unknown', reason: '页面在等待回执时被重新加载，无法确认是否已创建运行。' }));
  }
  const successful = receipts.filter((receipt) => !receipt.pending && receipt.rows.every((row) => row.state === 'accepted'));
  receipts = receipts.filter((receipt) => !successful.includes(receipt) || (receipt.rows.length > 1 && receipt === successful.at(-1)));
  save();
}

function save() {
  try { sessionStorage.setItem(storageKey(), JSON.stringify(receipts)); } catch { /* Keep this page's receipt when browser storage is unavailable. */ }
  paint();
}

function receiptRow(row) {
  const name = row.name || row.project_id;
  if (row.state === 'accepted') return h('li', {}, h('a', { class: 'card-link', href: href('runs/' + row.id) },
    `${name}：已加入执行队列 · ${row.id}`, icon('right')));
  if (row.state === 'resubmitted') return h('li', {}, `${name}：已重新提交，结果见最新回执`);
  return h('li', {}, h('b', {}, name + (row.state === 'unknown' ? '：启动结果未知' : '：未启动')), h('p', { class: 'sub' }, row.reason));
}

function receiptCard(receipt) {
  const rejected = receipt.rows.filter((row) => row.state === 'rejected');
  const unknown = receipt.rows.some((row) => row.state === 'unknown');
  const dismiss = () => { receipts = receipts.filter((item) => item.id !== receipt.id); save(); };
  return h('section', { class: 'card card-body stack launch-receipt', 'aria-label': '测试启动回执' },
    h('div', { class: 'row wrap' }, h('h2', { class: 'grow' }, receipt.pending ? '正在启动测试' : '测试启动回执'),
      !receipt.pending && h('button', { class: 'btn sm', onClick: dismiss }, '关闭回执')),
    receipt.pending ? h('p', { class: 'sub' }, '正在提交所选用例，请等待运行编号。') : h('ul', {}, receipt.rows.map(receiptRow)),
    unknown && h('p', { class: 'error', role: 'alert' }, '可能已经创建运行。请先到执行记录核对；这里不会自动再次发送。'),
    h('div', { class: 'row wrap' },
      h('a', { class: 'btn', href: href('runs', { range: 'all' }) }, '查看执行记录'),
      rejected.length > 0 && h('button', { class: 'btn', onClick: (event) => {
        const items = rejected.map((row) => row.item);
        for (const row of rejected) row.state = 'resubmitted';
        save();
        launchRuns(items, event.currentTarget, receipt.engine, receipt.headed);
      } },
        `只重试未启动的 ${rejected.length} 个项目`)));
}

function paint() {
  const view = document.getElementById('view');
  if (!view) return;
  let host = document.getElementById('launch-receipts');
  if (!host) {
    host = h('div', { id: 'launch-receipts', class: 'launch-receipts', 'aria-live': 'polite' });
    view.before(host);
  }
  host.hidden = !receipts.length;
  replace(host, receipts.map(receiptCard));
}

function targets(items) {
  if (!Array.isArray(items) || !items.length) throw new Error('请先选择至少一条用例');
  return items.map((item) => {
    if (!item.project_id || !Array.isArray(item.case_ids) || !item.case_ids.length) throw new Error('用例范围为空或尚未加载，请重新选择后运行');
    const entry = { project_id: item.project_id, case_ids: [...new Set(item.case_ids)].sort(), name: item.name };
    if (item.environment_id) entry.environment_id = item.environment_id;
    if (item.account_id) entry.account_id = item.account_id;
    return entry;
  }).sort((a, b) => a.project_id.localeCompare(b.project_id));
}

function runPayload(item) {
  const body = { project_id: item.project_id, case_ids: item.case_ids };
  if (item.environment_id) body.environment_id = item.environment_id;
  if (item.account_id) body.account_id = item.account_id;
  return body;
}

function formatMissingReason(result) {
  const missing = Array.isArray(result?.missing_variables) ? result.missing_variables.filter(Boolean) : [];
  const parts = [];
  if (missing.length) parts.push(`缺少环境变量：${missing.join('、')}`);
  if (result?.account_missing) parts.push('当前环境未配置账号');
  if (result?.base_url_missing) parts.push('未配置运行地址');
  const message = typeof result?.message === 'string' ? result.message.trim() : '';
  if (message) {
    const coversAll = missing.every((name) => message.includes(name));
    return coversAll && !result?.account_missing && !result?.base_url_missing ? message : [message, ...parts].filter(Boolean).join('；');
  }
  return parts.join('；') || '运行前检查未通过';
}

async function preflightOne(item) {
  try {
    const body = { case_ids: item.case_ids };
    if (item.environment_id) body.environment_id = item.environment_id;
    if (item.account_id) body.account_id = item.account_id;
    const result = await api(`/projects/${encodeURIComponent(item.project_id)}/preflight`, { method: 'POST', body });
    if (result && result.ok === false) return { ok: false, reason: formatMissingReason(result) };
    return { ok: true };
  } catch (error) {
    if (error.status === 404 || error.status === 405) return { ok: true };
    if (error.details && typeof error.details === 'object') {
      const missing = Array.isArray(error.details.missing_variables) ? error.details.missing_variables : [];
      if (missing.length) return { ok: false, reason: formatMissingReason({ message: error.message, missing_variables: missing }) };
    }
    return { ok: true };
  }
}

function formatSkippedReason(row) {
  const missing = Array.isArray(row?.missing_variables) ? row.missing_variables.filter(Boolean) : [];
  if (!missing.length) return row.reason;
  return missing.every((name) => row.reason.includes(name)) ? row.reason : `${row.reason}（缺少变量：${missing.join('、')}）`;
}

function parseRows(items, response) {
  return items.map((item) => {
    const runs = (Array.isArray(response?.runs) ? response.runs : []).filter((run) => run.project_id === item.project_id && typeof run.id === 'string' && run.id.trim());
    const skipped = (Array.isArray(response?.skipped) ? response.skipped : []).filter((row) => row.project_id === item.project_id && typeof row.reason === 'string' && row.reason.trim());
    const base = { project_id: item.project_id, name: item.name, item };
    if (runs.length === 1 && !skipped.length) return { ...base, state: 'accepted', id: runs[0].id };
    if (skipped.length === 1 && !runs.length) return { ...base, state: 'rejected', reason: formatSkippedReason(skipped[0]) };
    return { ...base, state: 'unknown', reason: '服务未返回这个项目的完整启动回执，请先核对执行记录。' };
  });
}

async function runPreflight(items) {
  const checks = await Promise.all(items.map(preflightOne));
  const ready = [];
  const blocked = new Map();
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (checks[index].ok) ready.push(item);
    else blocked.set(item.project_id, { project_id: item.project_id, name: item.name, item, state: 'rejected', reason: checks[index].reason });
  }
  return { ready, blocked };
}

async function submit(items, engine, headed) {
  const receipt = { id: crypto.randomUUID(), rows: items.map((item) => ({ project_id: item.project_id, name: item.name, item })), engine, headed, pending: true };
  receipts.push(receipt);
  save();
  const { ready, blocked } = await runPreflight(items);
  let response = null;
  const resolved = new Map(blocked);
  if (ready.length) {
    try {
      response = await operationRequest('/runs', { items: ready.map(runPayload), engine, headed });
      for (const row of parseRows(ready, response)) resolved.set(row.project_id, row);
    } catch (error) {
      for (const item of ready) {
        resolved.set(item.project_id, { project_id: item.project_id, name: item.name, item, state: classifyStartError(error), reason: error.message });
      }
    }
  }
  receipt.rows = items.map((item) => resolved.get(item.project_id));
  receipt.pending = false;
  const accepted = receipt.rows.every((row) => row.state === 'accepted');
  if (accepted) {
    receipts = receipts.filter((item) => item.id === receipt.id || item.pending || item.rows.some((row) => row.state !== 'accepted'));
    if (receipt.rows.length === 1) receipts = receipts.filter((item) => item.id !== receipt.id);
  } else if (blocked.size) {
    toast([...blocked.values()][0].reason, { bad: true });
  }
  save();
  if (receipt.rows.length === 1 && receipt.rows[0].state === 'accepted') go('runs/' + receipt.rows[0].id);
  return accepted ? response : null;
}

export async function launchRuns(items, button, requestedEngine, requestedHeaded) {
  const original = button && [...button.childNodes];
  if (button) { button.disabled = true; replace(button, h('i', { class: 'spin' }), '正在启动'); }
  try {
    await ensurePreferences();
    const engine = requestedEngine ?? preferredEngine();
    const headed = requestedHeaded ?? preferredHeaded();
    restore();
    let chosen;
    try { chosen = targets(items); } catch (error) {
      receipts.push({ id: crypto.randomUUID(), rows: [{ state: 'invalid', project_id: '当前选择', reason: error.message }], engine, headed });
      save();
      return null;
    }
    const key = JSON.stringify(['current-case-run', preferenceDataId(), chosen.map(runPayload), engine, headed]);
    return await startOperation(key, () => submit(chosen, engine, headed));
  } catch (error) {
    toast('测试尚未启动：' + error.message, { bad: true });
    return null;
  } finally { if (button) { button.disabled = false; button.replaceChildren(...original); } }
}

onPreferences(restore);
