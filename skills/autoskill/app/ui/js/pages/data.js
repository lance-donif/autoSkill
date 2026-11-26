// Move complete data roots through the kernel; a copied root is not an activated runtime.
import { h, icon, replace, api, copyText, toast, refresh } from '../core.js';
import { errorState } from '../ui.js';

function field(id, label, value, placeholder, hint) {
  const input = h('input', { id, class: 'input mono', value, placeholder, autocomplete: 'off', spellcheck: false });
  return { input, node: h('div', { class: 'field' }, h('label', { htmlFor: id }, label), input,
    hint && h('p', { class: 'hint' }, hint)) };
}

function card(title, description, body) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, title), description && h('p', { class: 'sub' }, description))),
    h('div', { class: 'card-body stack' }, body));
}

function operation({ label, fields, run, receipt, setBusy, actions }) {
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const progress = h('p', { class: 'sub', role: 'status', hidden: true });
  const result = h('div', { class: 'stack', 'aria-live': 'polite' });
  const button = h('button', { class: 'btn primary', type: 'submit' }, label);
  actions.push(button);
  const form = h('form', { class: 'stack' }, fields, h('div', { class: 'field' }, error),
    h('div', { class: 'row wrap' }, button, progress), result);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.hidden = true;
    result.replaceChildren();
    progress.textContent = '正在处理，请保持当前页面打开…';
    progress.hidden = false;
    setBusy(true);
    try { replace(result, receipt(await run())); }
    catch (problem) { error.textContent = problem.message || '操作没有完成，请重试'; error.hidden = false; }
    finally { progress.hidden = true; setBusy(false); }
  });
  return form;
}

function importPanel(status, shared) {
  const source = field('data-import-source', '旧数据位置', '', '', '填写 autoSkill 所在机器上的完整路径。原目录会保留。');
  const target = field('data-import-target', '新的空数据目录', status.suggested_import_target || status.root + '-imported', '',
    '导入会复制到这个目录；已有数据的目录不能合并或覆盖。');
  return card('导入旧数据', '把旧 workspace、完整数据目录或 autoSkill 数据 ZIP 迁入另一个空目录。', operation({
    ...shared, label: '导入到新目录', fields: [source.node, target.node],
    run: async () => {
      if (!source.input.value.trim()) throw new Error('请填写旧数据目录或 ZIP 的完整路径');
      if (!target.input.value.trim()) throw new Error('请填写新的空数据目录');
      if (target.input.value.trim() === status.root) throw new Error('请选择另一个空目录，当前目录正在使用');
      return await api('/data/import', { method: 'POST', body: { source: source.input.value.trim(), target: target.input.value.trim() } });
    },
    receipt: (value) => {
      if (value?.status !== 'imported' || typeof value.root !== 'string') throw new Error('服务未返回完成的导入回执，请查看数据目录后重试');
      return h('div', { class: 'stack', role: 'status' }, h('h3', {}, '已导入，尚未启用'),
        h('pre', { class: 'code', 'data-testid': 'imported-data-root' }, value.root),
        h('p', { class: 'sub' }, '当前服务仍使用上方的数据目录。请在 DSH 的 autoSkill 插件配置中，将 workspace 改为这个新目录，然后重启 autoSkill 插件。'),
        h('p', { class: 'sub' }, '独立运行时，在启动设置中选择这个数据目录后重新启动。'),
        h('button', { class: 'btn', type: 'button', onClick: () => copyText(value.root) }, icon('copy'), '复制新目录'));
    },
  }));
}

function exportPanel(shared) {
  const destination = field('data-export-destination', 'ZIP 文件路径（可选）', '', '留空使用服务默认导出位置',
    '填写 autoSkill 所在机器上的完整 ZIP 文件路径，包含文件名。生成后也可以直接下载。');
  return card('导出全部数据', '完整保留项目、用例、执行历史、截图和附件、报告、凭据与登录态，以及数据目录中的设置。', operation({
    ...shared, label: '导出全部数据', fields: h('details', {}, h('summary', {}, '指定导出位置'), destination.node),
    run: () => api('/data/export', { method: 'POST', body: destination.input.value.trim() ? { destination: destination.input.value.trim() } : {} }),
    receipt: (value) => {
      if (value?.status !== 'exported' || typeof value.download_url !== 'string') throw new Error('服务未返回完成的导出回执，请重试');
      const download = new URL(value.download_url, location.href);
      if (download.origin !== location.origin || !download.pathname.startsWith('/api/data/exports/')) throw new Error('服务返回的下载地址无效');
      return h('div', { class: 'stack', role: 'status' }, h('h3', {}, '完整数据 ZIP 已生成'),
        value.archive && h('pre', { class: 'code', 'data-testid': 'exported-data-archive' }, value.archive),
        h('div', { class: 'row wrap' }, h('a', { class: 'btn primary', href: download.href, download: '' }, '下载完整数据 ZIP')));
    },
  }));
}

const SYNC_STATUS = { imported: '已导入', skipped: '已跳过', failed: '失败' };

function syncCard(sync) {
  const scan = h('button', { class: 'btn primary' }, '立即扫描');
  const result = h('div', { class: 'stack' }, sync.last_scan
    ? h('p', { class: 'sub' }, `最近扫描：${sync.last_scan.at || ''}，识别 ${sync.last_scan.files} 个文件，导入 ${sync.last_scan.imported} 个`)
    : h('p', { class: 'sub' }, '还没有扫描记录。'));
  scan.addEventListener('click', async () => {
    scan.disabled = true;
    try {
      const receipt = await api('/sync/scan', { method: 'POST' });
      if (receipt.status === 'ok') { toast(`扫描完成：导入 ${receipt.imported} 个文件`); refresh(); }
      else { replace(result, h('p', { class: 'error' }, receipt.hint || '同步目录尚未配置')); }
    } catch (error) { toast(error.message, { bad: true }); }
    finally { scan.disabled = false; }
  });
  const syncRow = (row) => {
    const note = [row.project_id, ...(row.cases?.length ? [`${row.cases.length} 条用例`] : []), row.reason].filter(Boolean).join(' · ');
    return h('tr', {}, h('td', { class: 'mono' }, row.file),
      h('td', {}, row.unchanged ? '未变化' : (SYNC_STATUS[row.status] || row.status || '')),
      h('td', { class: 'sub' }, note));
  };
  const files = sync.files.length
    ? h('table', { class: 'table kv' }, h('tbody', {}, sync.files.map(syncRow)))
    : h('p', { class: 'sub' }, '同步目录里还没有 JSON 或 ZIP 用例文件。');
  return card('同步与错误目录', '把开发同步上来的通过状态用例导入项目用例库；失败回放按日期、项目和运行归档到错误目录。', [
    h('div', { class: 'stack', style: 'gap:4px' },
      h('div', { class: 'sub' }, '同步目录：', h('code', { class: 'mono' }, sync.sync_dir || '未配置（设置环境变量 AUTOSKILL_SYNC_DIR）')),
      h('div', { class: 'sub' }, '错误目录：', h('code', { class: 'mono' }, sync.error_dir || '未配置（设置环境变量 AUTOSKILL_ERROR_DIR）'))),
    h('div', { class: 'row wrap' }, scan),
    result, files,
  ]);
}

export async function render(view) {
  let status;
  try { status = await api('/data/status'); }
  catch (error) { replace(view, h('div', { class: 'card' }, errorState(error, () => render(view)))); return; }
  if (typeof status?.root !== 'string' || !status.root) throw new Error('服务没有返回当前数据目录');
  let sync;
  try { sync = await api('/sync'); } catch { sync = { files: [], sync_dir: null, error_dir: null, last_scan: null }; }
  const reload = h('button', { class: 'btn', onClick: () => refresh() }, icon('reset'), '刷新目录状态');
  const actions = [reload];
  const setBusy = (busy) => { for (const action of actions) action.disabled = busy; };
  const shared = { actions, setBusy };
  const caseSkill = typeof status.external_case_skill === 'string' ? status.external_case_skill : status.external_case_skill?.resolved;
  replace(view,
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '数据管理')), reload),
    card('当前数据目录', null, [
      h('pre', { class: 'code', 'data-testid': 'current-data-root' }, status.root),
      h('div', { class: 'row wrap' }, h('button', { class: 'btn', onClick: () => copyText(status.root) }, icon('copy'), '复制当前目录'),
        h('span', { class: 'sub' }, '数据集标识：'), h('code', { class: 'mono', 'data-testid': 'current-data-id' }, status.data_id || '尚未初始化')),
      typeof caseSkill === 'string' && h('p', { class: 'sub' }, '用例设计表：' + caseSkill),
    ]),
    exportPanel(shared), importPanel(status, shared), syncCard(sync));
}
