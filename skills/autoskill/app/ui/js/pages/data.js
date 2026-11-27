// Move complete data roots through the kernel; a copied root is not an activated runtime.
import { h, icon, replace, api, copyText, toast, refresh } from '../core.js';
import { errorState } from '../ui.js';
import { projectIndex } from '../shell.js';
import { openConflictDrawer } from '../sync-conflict-drawer.js';

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

const SYNC_STATUS = { imported: '已导入', skipped: '已跳过', pending: '传输中', conflict: '有冲突', failed: '失败' };

function manualSyncTools(projects) {
  const output = h('div', { class: 'stack', 'aria-live': 'polite' });
  const projectSelect = h('select', { class: 'select sync-project-select', 'aria-label': '选择导出项目' },
    h('option', { value: '' }, '全部项目'),
    projects.map((item) => h('option', { value: item.id }, item.name)));
  const exportBtn = h('button', { class: 'btn', type: 'button' }, '导出用例包');
  exportBtn.addEventListener('click', async () => {
    exportBtn.disabled = true;
    try {
      const body = { include_environments: true, include_accounts: true };
      if (projectSelect.value) body.project_id = projectSelect.value;
      const receipt = await api('/sync/export', { method: 'POST', body });
      const download = receipt.download_url ? new URL(receipt.download_url, location.href) : null;
      const safeDownload = download && download.origin === location.origin && download.pathname.startsWith('/api/sync/exports/');
      replace(output, h('div', { class: 'stack', role: 'status' },
        receipt.file && h('pre', { class: 'code', dataset: { testid: 'sync-export-path' } }, receipt.file),
        h('div', { class: 'row wrap', style: 'gap:8px' },
          safeDownload && h('a', { class: 'btn primary sm', href: download.href, download: '' }, '下载同步 ZIP'),
          receipt.file && h('button', { class: 'btn sm', type: 'button', onClick: () => copyText(receipt.file) }, icon('copy'), '复制路径'),
          receipt.case_count !== undefined && h('span', { class: 'sub' }, `${receipt.case_count} 条用例`))));
    } catch (error) {
      toast(error.message || '导出用例包失败', { bad: true });
    } finally {
      exportBtn.disabled = false;
    }
  });

  const fileInput = h('input', { class: 'input sync-file-input', type: 'file', accept: '.zip,.json', 'aria-label': '选择同步包' });
  const importBtn = h('button', { class: 'btn', type: 'button' }, '导入同步包');
  importBtn.addEventListener('click', async () => {
    const file = fileInput.files?.[0];
    if (!file) { toast('请先选择同步 ZIP 文件', { bad: true }); return; }
    importBtn.disabled = true;
    try {
      const form = new FormData();
      form.append('file', file);
      const response = await fetch('/api/sync/import', { method: 'POST', body: form });
      let data = null;
      try { data = await response.json(); } catch { /* empty body */ }
      if (!response.ok) throw new Error(data?.detail || `导入失败（${response.status}）`);
      if (data?.status === 'conflict' && (data.conflict_id || data.conflict?.id)) {
        const cid = data.conflict_id || data.conflict.id;
        toast('检测到同步冲突，请处理');
        await openConflictDrawer(cid, () => refresh());
        refresh();
        return;
      }
      toast(`已导入 ${data?.imported_cases?.length ?? 0} 条用例`);
      refresh();
    } catch (error) {
      toast(error.message || '导入同步包失败', { bad: true });
    } finally {
      importBtn.disabled = false;
    }
  });

  return h('div', { class: 'stack sync-manual-tools' },
    h('div', { class: 'row wrap', style: 'gap:8px' }, projectSelect, exportBtn, fileInput, importBtn),
    output);
}

function conflictSummaryText(item) {
  const parts = [];
  const caseCount = item.case_count ?? item.cases?.length ?? 0;
  const envCount = item.environment_count ?? item.environments?.length ?? 0;
  const accCount = item.account_count ?? item.accounts?.length ?? 0;
  if (caseCount) parts.push(`${caseCount} 条用例`);
  if (envCount) parts.push(`${envCount} 个环境`);
  if (accCount) parts.push(`${accCount} 个账号`);
  return parts.join(' · ') || '存在差异';
}

function conflictsTable(conflicts) {
  const pending = conflicts.filter((item) => item && item.status !== 'resolved');
  if (!pending.length) return null;
  return h('div', { class: 'stack' },
    h('h3', {}, `待处理冲突 (${pending.length})`),
    h('table', { class: 'table kv' },
      h('tbody', {}, pending.map((item) => h('tr', {},
        h('td', {}, item.project_name || item.project_id || '—'),
        h('td', { class: 'mono sub' }, item.source_file || item.id),
        h('td', { class: 'sub' }, conflictSummaryText(item)),
        h('td', { class: 'actions' }, h('button', { class: 'btn sm primary', type: 'button',
          onClick: () => openConflictDrawer(item.id, () => refresh()) }, '处理')))))));
}

function syncCard(sync, projects) {
  const scan = h('button', { class: 'btn primary', type: 'button' }, '立即扫描');
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
    const conflictId = row.conflict_id || (row.status === 'conflict' ? row.id : '');
    return h('tr', {}, h('td', { class: 'mono' }, row.file),
      h('td', {}, row.unchanged ? '未变化' : (SYNC_STATUS[row.status] || row.status || '')),
      h('td', { class: 'sub' }, note),
      h('td', { class: 'actions' }, conflictId && h('button', { class: 'btn sm primary', type: 'button',
        onClick: () => openConflictDrawer(conflictId, () => refresh()) }, '处理')));
  };
  const files = (sync.files || []).length
    ? h('table', { class: 'table kv' }, h('tbody', {}, sync.files.map(syncRow)))
    : h('p', { class: 'sub' }, '同步目录里还没有 JSON 或 ZIP 用例文件。');
  return card('同步与错误目录', null, [
    h('div', { class: 'stack', style: 'gap:4px' },
      h('div', { class: 'sub' }, '同步目录：', h('code', { class: 'mono' }, sync.sync_dir || '未配置（设置环境变量 AUTOSKILL_SYNC_DIR）')),
      h('div', { class: 'sub' }, '错误目录：', h('code', { class: 'mono' }, sync.error_dir || '未配置（设置环境变量 AUTOSKILL_ERROR_DIR）'))),
    h('div', { class: 'row wrap' }, scan),
    manualSyncTools(projects),
    conflictsTable(sync.conflicts || []),
    result, files,
  ]);
}

export async function render(view) {
  let status;
  try { status = await api('/data/status'); }
  catch (error) { replace(view, h('div', { class: 'card' }, errorState(error, () => render(view)))); return; }
  if (typeof status?.root !== 'string' || !status.root) throw new Error('服务没有返回当前数据目录');
  let sync;
  let projects = [];
  try { sync = await api('/sync'); } catch { sync = { files: [], conflicts: [], sync_dir: null, error_dir: null, last_scan: null }; }
  try {
    const all = await projectIndex();
    projects = (all || []).filter((item) => !item.broken);
  } catch { projects = []; }
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
    exportPanel(shared), importPanel(status, shared), syncCard(sync, projects));
}
