// Keep request receipts separate from execution results and across ordinary page refreshes.
import { h, icon, replace, href, go, toast } from './core.js';
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
    return { project_id: item.project_id, case_ids: [...new Set(item.case_ids)].sort(), name: item.name };
  }).sort((a, b) => a.project_id.localeCompare(b.project_id));
}

function parseRows(items, response) {
  return items.map((item) => {
    const runs = (Array.isArray(response?.runs) ? response.runs : []).filter((run) => run.project_id === item.project_id && typeof run.id === 'string' && run.id.trim());
    const skipped = (Array.isArray(response?.skipped) ? response.skipped : []).filter((row) => row.project_id === item.project_id && typeof row.reason === 'string' && row.reason.trim());
    const base = { project_id: item.project_id, name: item.name, item };
    if (runs.length === 1 && !skipped.length) return { ...base, state: 'accepted', id: runs[0].id };
    if (skipped.length === 1 && !runs.length) return { ...base, state: 'rejected', reason: skipped[0].reason };
    return { ...base, state: 'unknown', reason: '服务未返回这个项目的完整启动回执，请先核对执行记录。' };
  });
}

async function submit(items, engine, headed) {
  const receipt = { id: crypto.randomUUID(), rows: items.map((item) => ({ project_id: item.project_id, name: item.name, item })), engine, headed, pending: true };
  receipts.push(receipt);
  save();
  let response;
  try {
    response = await operationRequest('/runs', { items: items.map(({ project_id, case_ids }) => ({ project_id, case_ids })), engine, headed });
    receipt.rows = parseRows(items, response);
  } catch (error) {
    receipt.rows = items.map((item) => ({ project_id: item.project_id, name: item.name, item, state: classifyStartError(error), reason: error.message }));
  }
  receipt.pending = false;
  const accepted = receipt.rows.every((row) => row.state === 'accepted');
  if (accepted) {
    receipts = receipts.filter((item) => item.id === receipt.id || item.pending || item.rows.some((row) => row.state !== 'accepted'));
    if (receipt.rows.length === 1) receipts = receipts.filter((item) => item.id !== receipt.id);
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
    const key = JSON.stringify(['current-case-run', preferenceDataId(), chosen.map(({ project_id, case_ids }) => ({ project_id, case_ids })), engine, headed]);
    return await startOperation(key, () => submit(chosen, engine, headed));
  } catch (error) {
    toast('测试尚未启动：' + error.message, { bad: true });
    return null;
  } finally { if (button) { button.disabled = false; button.replaceChildren(...original); } }
}

onPreferences(restore);
