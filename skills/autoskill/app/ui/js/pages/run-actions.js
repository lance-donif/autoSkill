// Keep action receipts across page redraws; an unknown response is never a successful mutation.
import { ensurePreferences } from '../preferences.js';
import { h, icon, href, go, route, refresh } from '../core.js';
import { preferredHeaded } from '../ui.js';
import { startOperation, operationRequest, classifyStartError } from '../start-operation.js';

const receipts = new Map();
const notices = new Map();

export function returnToRuns() {
  const params = route().params;
  const saved = params.get('return');
  if (saved?.startsWith('#/runs?') || saved === '#/runs') return saved;
  return href('runs', Object.fromEntries([...params].filter(([key]) => !['case', 'cursor', 'issues', 'return'].includes(key))));
}

export function recoveryActions(run, row) {
  if (row.state?.phase !== 'blocked' && row.status !== 'Blocked') return null;
  const category = row.category;
  let text = '请根据上方原因补齐条件，再手动重新运行；本次记录会保留。';
  let action;
  if (['missing_secret', 'session_expired'].includes(category)) {
    text = category === 'missing_secret' ? '先在项目设置补齐提示中缺少的凭据，再重新运行。' : '先在项目设置检查登录信息并重新登录，再重新运行。';
    action = h('a', { class: 'btn sm', href: href('cases', { projects: run.project_id, settings: run.project_id }) }, '打开本项目设置');
  } else if (category === 'environment') {
    text = '先核对执行环境和上方原因；更正后手动运行，旧记录不会改变。';
    action = h('a', { class: 'btn sm', href: href('diagnostics') }, '检查运行环境');
  } else if (['prerequisite_failed', 'missing_variable'].includes(category)) {
    text = '先检查本次前置用例的结果及输出数据，更正后再运行。';
    action = h('a', { class: 'btn sm', href: href('cases', { projects: run.project_id }) }, '查看本项目用例');
  }
  return h('div', { class: 'stack', 'aria-label': '继续处理' }, h('p', { class: 'sub' }, text),
    action && h('div', { class: 'row wrap' }, action));
}

export function runActionFeedback(run) {
  const entries = [...receipts].filter(([key]) => key.startsWith(run.id + ':')).map(([, value]) => value);
  const created = notices.get(run.id);
  if (!created && !entries.length) return null;
  return h('div', { class: 'stack', 'aria-live': 'polite' }, created && h('div', { class: 'banner' }, created),
    entries.map((entry) => h('div', { class: 'banner' + (['unknown', 'rejected'].includes(entry.status) ? ' bad' : ''), role: 'status' },
      entry.message, entry.status === 'unknown' && h('a', { class: 'btn sm', href: returnToRuns() }, '查看执行记录'))));
}

function actionError(key, error) {
  const status = classifyStartError(error);
  receipts.set(key, { key, status, message: status === 'rejected' ? `请求被拒绝：${error.message}`
    : `未收到明确回执，操作可能已经生效：${error.message}。可在执行记录核对；本页没有自动重发。` });
}

export function rerunCaseButton(run, caseId) {
  const key = run.id + ':rerun:case:' + caseId;
  const pending = receipts.get(key)?.status === 'pending';
  return h('button', { class: 'btn sm', disabled: pending, onClick: async () => {
    if (receipts.get(key)?.status === 'pending') return;
    receipts.set(key, { status: 'pending', message: '正在提交重新运行请求…' });
    refresh();
    try { await ensurePreferences(); } catch (error) {
      receipts.set(key, { status: 'rejected', message: '测试尚未启动：' + error.message });
      refresh();
      return;
    }
    try {
      const body = { headed: preferredHeaded(), case_id: caseId };
      const result = await startOperation('rerun:' + run.id + ':' + JSON.stringify(body), () => operationRequest(
        '/runs/' + encodeURIComponent(run.id) + '/rerun', body, (value) => typeof value?.id === 'string' && value.id.length > 0));
      const message = '已使用当前用例定义提交重新运行，运行编号 ' + result.id;
      receipts.set(key, { status: 'success', message });
      notices.set(result.id, message);
      go('runs/' + result.id);
    } catch (error) { actionError(key, error); refresh(); }
  } }, icon('play'), pending ? '正在提交…' : '只重跑这一条');
}
