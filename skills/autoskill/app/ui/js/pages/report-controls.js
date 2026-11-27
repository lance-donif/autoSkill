// Execution reports share the verdict, six ISTQB summary counts, HTML download and error-pack ZIP.
import { h, icon, attempt, toast, fmtDuration } from '../core.js';
import { statePill } from '../status-view.js';

export function reportVerdict(run) {
  const outcome = run.state?.outcome;
  if (run.status === 'Queued') return statePill('queued');
  if (run.status === 'Running') return statePill('running');
  if (outcome === 'fail' || run.status === 'Fail' || (run.failed || 0) > 0) return statePill('fail', '', '不通过');
  if (outcome === 'blocked' || run.status === 'Blocked' || (run.blocked || 0) > 0) return statePill('blocked', '', '受阻');
  if ((outcome === 'pass' || (!outcome && run.status === 'Pass')) && run.total > 0 && run.passed === run.total && !run.not_run) {
    return statePill('pass');
  }
  return statePill('not_run');
}

export function reportStats(run, duration) {
  const passed = run.passed || 0;
  const failed = run.failed || 0;
  const blocked = run.blocked || 0;
  const notRun = run.not_run || 0;
  const total = run.total ?? 0;
  const executed = run.executed ?? (passed + failed + blocked);
  const stat = (label, value, tone = '') => h('div', { class: 'stat ' + tone },
    h('span', { class: 'stat-value' }, String(value)), h('span', { class: 'stat-label' }, label));
  return h('div', { class: 'stats' },
    stat('总用例', total),
    stat('本次执行', executed),
    stat('通过', passed, 'pass'),
    stat('失败', failed, 'fail'),
    stat('受阻', blocked, 'blocked'),
    stat('未执行', notRun),
    stat('总耗时', fmtDuration(duration)));
}

export function reportControls(download, { errorPackUrl = '' } = {}) {
  const button = h('button', { class: 'btn', onClick: async () => {
    const result = await attempt(download, button);
    if (result) toast(result.warnings.length ? `HTML 已生成，${result.warnings.length} 张截图未能嵌入，原因已写入报告` : 'HTML 报告已生成，可离线查看全部明细', { bad: result.warnings.length > 0 });
  } }, icon('globe'), '下载 HTML 报告');
  const errorPack = errorPackUrl ? h('a', { class: 'btn', href: errorPackUrl, download: '' }, icon('folder'), '下载错误包 ZIP') : null;
  return h('section', { class: 'row wrap', 'aria-label': '本次报告' }, button, errorPack);
}
