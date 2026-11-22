// Shared presentation of independent lifecycle, verdict, evidence and delivery axes.
import { h, icon, href } from './core.js';

export const STATE_LABELS = {
  pending: '待处理', active: '进行中', paused: '已暂停', completed: '已结束', cancelled: '已取消', unknown: '尚未确认',
  queued: '排队中', running: '执行中', awaiting_host: '等待宿主', waiting_execution: '等待执行',
  recovering: '恢复中', blocked: '阻塞', settled: '已结束', ready: '就绪', leased: '处理中', publishing: '发布中',
  not_run: '未执行', pass: '通过', fail: '失败', unverified: '未验证', not_applicable: '不适用',
  valid: '有效', invalid: '已失效', generating: '生成中', failed: '失败', skipped: '已跳过', not_generated: '尚未生成',
  dispatching: '投递中', delivered: '已送达', obsolete: '已替代',
};
// Report and delivery pills appear only once they hold a fact worth reading.
const recorded = (value) => !!value && !['not_generated', 'not_applicable'].includes(value);
const AXES = [['business', '业务'], ['performance', '性能'], ['quality', '质量']];
// Callers whose lifecycle words mean something narrower (a resumable workflow pause) pass their own overrides.
export const stateLabel = (value, labels) => labels?.[value] || STATE_LABELS[value] || value || '未同步';
const tone = (value) => ({ pass: 'pass', valid: 'pass', fail: 'fail', failed: 'fail', invalid: 'fail',
  blocked: 'blocked', unverified: 'blocked', running: 'running', queued: 'queued', active: 'running' })[value] || 'idle';

export function statePill(value, prefix = '', text = null) {
  return h('span', { class: 'status ' + tone(value), title: prefix + (text || stateLabel(value)) },
    icon(['fail', 'failed', 'invalid'].includes(value) ? 'x' : ['pass', 'valid'].includes(value) ? 'check' : 'info'),
    prefix + (text || stateLabel(value)));
}

export function primaryState(entity, labels) {
  const state = entity?.state;
  const pendingCurrent = state?.current === false;
  const value = pendingCurrent ? 'pending' : state?.phase || state?.lifecycle;
  const text = entity?.delete_requested ? '正在取消并删除' : pendingCurrent ? '未执行' : labels?.[value] || null;
  const phase = statePill(value, '', text);
  if (!state?.scope?.historical) return phase;
  return h('span', { class: 'row wrap', 'aria-label': '历史执行阶段' }, phase,
    h('span', { class: 'tag', title: '周期 ' + state.scope.cycle_id + '；当前周期 ' + state.scope.current_cycle_id }, '历史周期'));
}

// A settled phase adds nothing beside the result pill, so lists show the phase only while it is still moving.
export function activeState(entity) {
  const phase = entity?.state?.phase || entity?.state?.lifecycle;
  return !entity?.delete_requested && ['settled', 'completed', 'blocked'].includes(phase) ? null : primaryState(entity);
}

export function stateResults(entity, compact = true) {
  const state = entity?.state;
  const outcome = state?.current === false ? 'not_run' : state?.outcome || 'not_run';
  if (compact || !state) return statePill(outcome);
  const checks = AXES.filter(([axis]) => ['pass', 'fail'].includes(state.checks?.[axis]));
  const qualifier = state.current === false ? '历史' : '';
  return h('div', { class: 'row wrap state-results', 'aria-label': '分项结果' },
    statePill(outcome, '综合：'),
    checks.map(([axis, title]) => statePill(state.checks?.[axis], qualifier + title + '：')),
    ['valid', 'invalid'].includes(state.evidence) && statePill(state.evidence, qualifier + '证据：'),
    recorded(state.report) && statePill(state.report, qualifier + '报告：', state.report === 'ready' ? '已就绪' : null),
    recorded(state.delivery) && statePill(state.delivery, qualifier + '投递：'));
}

// Supplementary facts only: callers already show stateResults(entity, false) and the reason beside it.
export function stateDetails(entity) {
  const scope = entity?.state?.scope;
  return h('div', { class: 'stack state-details' }, historicalCaseState(entity),
    scope?.cycle_id && h('p', { class: 'sub mono' }, (scope.historical ? '历史周期：' : '周期：') + scope.cycle_id,
      scope.historical && scope.current_cycle_id ? '；当前周期：' + scope.current_cycle_id : ''));
}

function historicalCaseState(entity) {
  const state = entity?.state;
  if (state?.current !== false) return null;
  const runId = state.scope?.run_id;
  return h('div', { class: 'stack', 'aria-label': '历史执行事实' },
    h('p', { class: 'sub' }, '当前输入范围尚无有效执行证据；历史执行不代表当前版本已经验证。'),
    h('div', { class: 'row wrap' }, statePill(state.phase || state.lifecycle, '历史阶段：'),
      statePill(state.historical_outcome, '历史综合：')),
    runId && h('a', { class: 'card-link mono', href: href('runs/' + encodeURIComponent(runId)) }, '查看历史执行：' + runId));
}
