// Keep action receipts across page redraws; an unknown response is never a successful mutation.
import { ensurePreferences } from '../preferences.js';
import { h, icon, href, go, route, refresh } from '../core.js';
import { preferredHeaded } from '../ui.js';
import { startOperation, operationRequest, classifyStartError } from '../start-operation.js';

const receipts = new Map();
const notices = new Map();
const active = (run) => ['Queued', 'Running'].includes(run.status);

export function returnToRuns() {
  const params = route().params;
  const saved = params.get('return');
  if (saved?.startsWith('#/runs?') || saved === '#/runs') return saved;
  return href('runs', Object.fromEntries([...params].filter(([key]) => !['case', 'cursor', 'issues', 'return'].includes(key))));
}

export function resultConclusion(run) {
  const outcome = run.state?.outcome;
  const label = ({ pass: '本次综合通过', fail: '本次综合失败' })[outcome] || '本次未执行';
  const failedChecks = [['business', '业务'], ['performance', '性能目标'], ['quality', '回答质量']]
    .filter(([key]) => run.state?.checks?.[key] === 'fail').map(([, title]) => title + '未通过');
  return h('p', { class: outcome === 'fail' ? 'error' : 'sub', role: 'status' }, label,
    failedChecks.length ? ' · ' + failedChecks.join('、') : '',
    ` · 用例汇总：${run.not_run || 0} 未执行、${run.passed || 0} 通过、${run.failed || 0} 失败`);
}

export function recoveryActions(run, row = run) {
  if (row.state?.phase !== 'blocked' && row.status !== 'Blocked') return null;
  const category = row.category;
  let text = '请根据上方原因补齐条件，再手动重新运行；本次记录会保留。';
  let action;
  if (['missing_secret', 'session_expired'].includes(category)) {
    text = category === 'missing_secret' ? '先在项目设置补齐提示中缺少的凭据，再重新运行。' : '先在项目设置检查登录信息并重新登录，再重新运行。';
    action = h('a', { class: 'btn sm', href: href('cases', { projects: run.project_id, settings: run.project_id }) }, '打开本项目设置');
  } else if (category === 'environment' || run.family === 'native') {
    text = '先核对执行环境和上方原因；更正后手动运行，旧记录不会改变。';
    action = h('a', { class: 'btn sm', href: href('diagnostics') }, '检查运行环境');
  } else if (['prerequisite_failed', 'missing_variable'].includes(category)) {
    text = '先检查本次前置用例的结果及输出数据，更正后再运行。';
    action = h('a', { class: 'btn sm', href: href('cases', { projects: run.project_id }) }, '查看本项目用例');
  }
  return h('div', { class: 'stack', 'aria-label': '继续处理' }, h('p', { class: 'sub' }, text),
    h('div', { class: 'row wrap' }, action, run.asset_id && h('a', { class: 'btn sm', href: href('native-assets/' + run.asset_id, { project: run.project_id }) }, '查看原始测试资产')));
}

export function runActionFeedback(run) {
  const cancellation = receipts.get(run.id + ':cancel');
  if (cancellation?.status === 'waiting' && !active(run)) {
    cancellation.status = 'success';
    cancellation.message = run.status === 'Cancelled' ? '本次运行已取消；已产生的结果仍然保留。' : '本次运行已经结束，已产生的结果仍然保留。';
  }
  const entries = [...receipts].filter(([key]) => key.startsWith(run.id + ':')).map(([, value]) => value);
  const created = notices.get(run.id);
  const message = created && (run.status === 'Queued' ? created.message
    : (run.status === 'Running' ? '本次运行正在执行。' : '本次运行已结束，具体结论见上方。') + created.context);
  return h('div', { class: 'stack', 'aria-live': 'polite' }, created && h('div', { class: 'banner' }, message, createdRunLinks(created)),
    entries.map((entry) => h('div', { class: 'banner' + (['unknown', 'rejected'].includes(entry.status) ? ' bad' : ''), role: 'status' },
      entry.message, createdRunLinks(entry), entry.status === 'unknown' && h('a', { class: 'btn sm', href: returnToRuns() }, '查看执行记录'))));
}

function createdRunLinks(entry) {
  return entry.runIds?.length > 1 && h('div', { class: 'row wrap' }, entry.runIds.map((id) =>
    h('a', { class: 'btn sm', href: href('runs/' + id) }, '查看运行 ' + id)));
}

function actionError(key, error) {
  const status = classifyStartError(error);
  receipts.set(key, { key, status, message: status === 'rejected' ? `请求被拒绝：${error.message}`
    : `未收到明确回执，操作可能已经生效：${error.message}。可在执行记录核对；本页没有自动重发。` });
}

function rerunReceipt(value, candidateId) {
  if (!candidateId) return typeof value?.id === 'string' && value.id.length > 0;
  if (value?.candidate_id !== candidateId || !Array.isArray(value.run_ids)
    || !value.run_ids.every((id) => typeof id === 'string' && id.length > 0)) return false;
  return value.run_ids.length ? value.id === value.run_ids[0]
    : value.id === null && value.total === 0 && value.publication?.complete === true;
}

function preparationMessage(result) {
  const errors = Object.entries(result.preparation_errors || result.publication?.unpublished || {});
  return errors.length ? `有 ${errors.length} 条候选尚未准备成功：` + errors.map(([id, reason]) => `${id}：${String(reason)}`).join('；') + '。' : '';
}

function rerunMessage(result, runIds, native, candidateId) {
  const started = `运行 ${runIds.join('、')} 已加入执行队列。`
    + (result.dropped ? `原记录中有 ${result.dropped} 条用例已不存在，本次跳过。` : '');
  if (native) return started + '沿用原资产版本和参数。';
  if (candidateId) return started + '使用本次候选版本；入队不等于验证通过或已入库。' + preparationMessage(result);
  return started + '使用当前用例定义。';
}

function rerunContext(result, native, candidateId) {
  const version = native ? '沿用原资产版本和参数。' : candidateId ? '使用本次候选版本。' : '使用当前用例定义。';
  return version + (result.dropped ? `原记录中有 ${result.dropped} 条用例已不存在，本次跳过。` : '') + preparationMessage(result);
}

async function loadReplayPreferences(key, native) {
  if (native) return true;
  try { await ensurePreferences(); return true; } catch (error) {
    receipts.set(key, { key, status: 'rejected', message: '测试尚未启动：' + error.message });
    refresh();
    return false;
  }
}

function rerunOptions(native, candidateId, { failedOnly, count, caseId }) {
  if (native) return { action: 'all', style: ' primary', label: '按原版本再次运行', payload: {} };
  if (caseId) return { action: 'case:' + caseId, style: ' sm', label: '只重跑这一条', payload: { case_id: caseId } };
  if (failedOnly) return { action: 'failed', style: ' primary', payload: { failed_only: true },
    label: candidateId ? `再次验证失败的 ${count} 条候选` : `重跑失败的 ${count} 条` };
  return { action: 'all', style: '', payload: {}, label: candidateId ? '按本次候选版本再次验证' : '按当前用例重新运行' };
}

export function rerunButton(run, { failedOnly = false, count = 0, caseId = null } = {}) {
  if (run.workflow_id) return failedOnly ? null : h('a', { class: 'btn primary', href: href('workflows/' + run.workflow_id) }, '返回原测试任务');
  const native = run.family === 'native';
  const candidateId = native ? null : run.publication?.candidate_id;
  const { action, style, label, payload } = rerunOptions(native, candidateId, { failedOnly, count, caseId });
  const key = run.id + ':rerun:' + action;
  const blocked = receipts.get(key)?.status === 'pending';
  const page = native ? 'native-runs' : 'runs';
  return h('button', { class: 'btn' + style, disabled: blocked, onClick: async () => {
    if (receipts.get(key)?.status === 'pending') return;
    receipts.set(key, { key, status: 'pending', message: '正在提交重新运行请求…' }); refresh();
    try {
      if (!await loadReplayPreferences(key, native)) return;
      const body = native ? {} : { headed: preferredHeaded(), ...payload };
      const result = await startOperation('rerun:' + run.id + ':' + JSON.stringify(body), () => operationRequest(
        `/${page}/${encodeURIComponent(run.id)}/rerun`, body,
        (value) => rerunReceipt(value, candidateId)));
      if (candidateId && !result.run_ids.length) {
        receipts.set(key, { key, status: 'rejected', message: '本次没有候选加入执行队列，未开始新的验证。' + preparationMessage(result) });
        refresh(); return;
      }
      const runIds = candidateId ? result.run_ids : [result.id];
      const message = rerunMessage(result, runIds, native, candidateId);
      receipts.set(key, { key, status: 'success', message, runIds });
      for (const id of runIds) notices.set(id, { message, runIds, context: rerunContext(result, native, candidateId) });
      go(page + '/' + result.id);
    } catch (error) { actionError(key, error); refresh(); }
  } }, icon('play'), blocked ? '正在提交…' : label);
}

export function cancelButton(run) {
  const key = run.id + ':cancel';
  const current = receipts.get(key);
  const waiting = !!run.cancel || ['pending', 'waiting'].includes(current?.status);
  return h('button', { class: 'btn danger', disabled: waiting, onClick: async () => {
    if (['pending', 'waiting'].includes(receipts.get(key)?.status)) return;
    receipts.set(key, { key, status: 'pending', message: '正在请求取消…' }); refresh();
    try {
      const result = await startOperation(key, () => operationRequest(`/${run.family === 'native' ? 'native-runs' : 'runs'}/${encodeURIComponent(run.id)}/cancel`, {},
        (value) => typeof value?.status === 'string' && value.id === run.id));
      let message;
      let status = 'success';
      if (result.status === 'Cancelled') message = '本次运行已取消；已产生的结果仍然保留。';
      else if (!active(result)) message = '本次运行已经结束，取消没有改变已产生的结果。';
      else if (result.cancel) { status = 'waiting'; message = '取消已申请，正在等待当前执行结束；已完成的结果仍然保留。'; }
      else throw new Error('服务端仍显示运行中，尚未确认取消请求');
      receipts.set(key, { key, status, message });
    } catch (error) { actionError(key, error); }
    refresh();
  } }, icon('stop'), waiting ? '等待取消结果' : '取消运行');
}
