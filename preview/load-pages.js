// Large browser-simulated load runs expand only the requested page of result rows.
import { LOAD_PAGE_SIZE } from './runs-api.js';

export function installLoadPages(currentData) {
  const view = document.getElementById('view');
  const pager = document.createElement('nav');
  pager.className = 'toolbar';
  pager.setAttribute('aria-label', '压测明细分页');
  view.before(pager);

  function render() {
    const [route, search] = location.hash.split('?');
    const id = route.startsWith('#/load-tests/') && route.slice('#/load-tests/'.length);
    const run = id && currentData().runs.find((r) => r.id === id && r.load_templates);
    pager.hidden = !run;
    pager.replaceChildren();
    if (!run) return;
    const params = new URLSearchParams(search);
    const last = Math.floor((run.total - 1) / LOAD_PAGE_SIZE);
    const page = Math.max(0, Math.min(last, Math.floor(Number(params.get('detail_offset')) / LOAD_PAGE_SIZE) || 0));
    const text = document.createElement('span');
    text.textContent = `明细第 ${page + 1} / ${last + 1} 页`;
    pager.append(text);
    for (const [label, target] of [['首页', 0], ['上一页', page - 1], ['下一页', page + 1], ['末页', last]]) {
      if (target < 0 || target > last || target === page) continue;
      const link = document.createElement('a');
      link.textContent = label;
      link.className = 'btn sm';
      const next = new URLSearchParams(params);
      next.set('detail_offset', String(target * LOAD_PAGE_SIZE));
      link.href = route + '?' + next;
      pager.append(link);
    }
  }

  render();
  window.addEventListener('hashchange', render);
}
