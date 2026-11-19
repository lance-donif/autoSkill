// Link the immutable observation records to the matching preview run and case views.
import { h } from '../skills/autoskill/app/ui/js/core.js';
import { closeOverlay } from '../skills/autoskill/app/ui/js/ui.js';

function observationCard(records, caseView) {
  return h('section', { class: 'stack', 'aria-label': '实测来源', dataset: { observations: records[0].run_id } },
    h('h3', {}, '实测来源'), records.map((record) => {
      const historical = record.source_kind === 'historical';
      const source = new URL(historical ? '../' + record.source_url : '../scripts/fixtures/runoob-observations.json', import.meta.url);
      const time = h('time', { dateTime: record.checked_at }, record.checked_at.replace('T', ' '));
      return h('div', { class: 'stack' },
        h('div', {}, record.case_id, ' · 第 ', record.revision, ' 版 · ', historical ? '历史实测' : '现场实测'),
        h('div', {}, '实测时间　', caseView
          ? h('a', { class: 'card-link', href: '#/runs/' + encodeURIComponent(record.run_id) + '?range=all', onClick: () => closeOverlay() }, time)
          : time),
        h('p', {}, record.actual),
        h('div', { class: 'row wrap' },
          h('a', { class: 'card-link', href: record.url, target: '_blank', rel: 'noopener noreferrer' }, '打开目标页'),
          h('a', { class: 'card-link', href: source.href, target: '_blank', rel: 'noopener' }, historical ? '打开原始报告' : '打开回执摘要 JSON')));
    }));
}

export function showObservations(source) {
  if (!source) return;
  const runs = new Map();
  const currentCases = new Map(source.cases.map((record) => [record.case_id, record]));
  for (const record of [...(source.previous_cases || []), ...source.cases]) {
    if (!runs.has(record.run_id)) runs.set(record.run_id, []);
    runs.get(record.run_id).push(record);
  }
  const view = document.getElementById('view');
  const layer = document.getElementById('layer');

  function syncRun() {
    const summary = view.querySelector('.run-summary .report-summary');
    if (!summary) return;
    const runId = location.hash.match(/^#\/runs\/([^/?]+)/)?.[1];
    const records = runs.get(runId);
    const card = summary.querySelector('[data-observations]');
    if (card && card.dataset.observations !== runId) card.remove();
    if (records && !summary.querySelector('[data-observations]')) summary.append(observationCard(records, false));
  }

  function syncCase() {
    const body = layer.querySelector('.drawer-body');
    if (!body || body.querySelector('[data-observations]')) return;
    // Script exports identify both the project and case even when display titles have been edited.
    const link = body.querySelector('a[download][href*="/cases/"][href*="/script?"]');
    if (!link) return;
    const [, , , projectId, , caseId] = new URL(link.href).pathname.split('/');
    const record = projectId === source.project_id && currentCases.get(decodeURIComponent(caseId));
    if (record) body.prepend(observationCard([record], true));
  }

  new MutationObserver(syncRun).observe(view, { childList: true, subtree: true });
  new MutationObserver(syncCase).observe(layer, { childList: true, subtree: true });
  syncRun();
  syncCase();
}
