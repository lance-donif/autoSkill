// Execution reports share the verdict, five summary values and a self-contained HTML download.
import { h, icon, attempt, toast, fmtDuration } from '../core.js';
import { statePill } from '../status-view.js';

export function reportVerdict(run) {
  const outcome = run.state?.outcome;
  if (run.status === 'Queued') return statePill('queued');
  if (run.status === 'Running') return statePill('running');
  if (outcome === 'fail' || run.status === 'Fail' || run.failed > 0) return statePill('fail', '', '不通过');
  if ((outcome === 'pass' || (!outcome && run.status === 'Pass')) && run.total > 0 && run.passed === run.total && !run.not_run) {
    return statePill('pass');
  }
  return statePill('not_run');
}

export function reportStats(run, duration) {
  const stat = (label, value, tone = '') => h('div', { class: 'stat ' + tone },
    h('span', { class: 'stat-value' }, String(value)), h('span', { class: 'stat-label' }, label));
  return h('div', { class: 'stats' },
    stat('通过', run.passed || 0, 'pass'), stat('失败', run.failed || 0, 'fail'), stat('未执行', run.not_run || 0),
    stat('用例', run.total), stat('总耗时', fmtDuration(duration)));
}

export function reportControls(download) {
  const button = h('button', { class: 'btn', onClick: async () => {
    const result = await attempt(download, button);
    if (result) toast(result.warnings.length ? `HTML 已生成，${result.warnings.length} 张截图未能嵌入，原因已写入报告` : 'HTML 报告已生成，可离线查看全部明细', { bad: result.warnings.length > 0 });
  } }, icon('globe'), '下载 HTML 报告');
  return h('section', { class: 'stack', 'aria-label': '本次报告' },
    h('h3', {}, '本次报告'), h('div', { class: 'row' }, button));
}
