// Link the immutable observation records to the matching preview run details.
import { h } from '../skills/autoskill/app/ui/js/core.js';

function observationCard(record) {
  const historical = record.source_kind === 'historical';
  const source = new URL(historical ? '../' + record.source_url : '../scripts/fixtures/runoob-observations.json', import.meta.url);
  const time = h('time', { dateTime: record.checked_at }, record.checked_at.replace('T', ' '));
  return h('section', { class: 'stack', 'aria-label': '实测来源', dataset: { observations: record.run_id } },
    h('h3', {}, '实测来源'),
    h('div', {}, record.case_id, ' · 第 ', record.revision, ' 版 · ', historical ? '历史实测' : '现场实测'),
    h('div', {}, '实测时间　', time),
    h('p', {}, record.actual),
    h('div', { class: 'row wrap' },
      h('a', { class: 'card-link', href: record.url, target: '_blank', rel: 'noopener noreferrer' }, '打开目标页'),
      h('a', { class: 'card-link', href: source.href, target: '_blank', rel: 'noopener' }, historical ? '打开原始报告' : '打开回执摘要 JSON')));
}

export function showObservations(source) {
  if (!source) return;
  const runs = new Map();
  for (const record of [...(source.previous_cases || []), ...source.cases]) {
    if (!runs.has(record.run_id)) runs.set(record.run_id, []);
    runs.get(record.run_id).push(record);
  }
  const view = document.getElementById('view');

  function syncRun() {
    const runId = location.hash.match(/^#\/runs\/([^/?]+)/)?.[1];
    for (const record of runs.get(runId) || []) {
      const body = view.querySelector(`.result[data-case="${CSS.escape(record.case_id)}"] .result-body`);
      if (body && !body.querySelector('[data-observations]')) body.append(observationCard(record));
    }
  }

  new MutationObserver(syncRun).observe(view, { childList: true, subtree: true });
  syncRun();
}
