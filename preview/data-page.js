// Browser backups contain the editable preview data for this site.
import { h, replace, api, toast } from '../skills/autoskill/app/ui/js/core.js';
import { confirm } from '../skills/autoskill/app/ui/js/ui.js';
import { loadPreferences } from '../skills/autoskill/app/ui/js/preferences.js';
import { refreshShell } from '../skills/autoskill/app/ui/js/shell.js';
import { getData, restoreData } from './store.js';

const FORMAT = 'autoskill-browser-backup';
const VERSION = 1;
const SYNC_STATUS = { imported: '已导入', skipped: '已跳过', failed: '失败' };

function card(title, ...content) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, title)),
    h('div', { class: 'card-body stack' }, ...content));
}

function syncCard(sync) {
  const status = h('p', { class: 'sub', role: 'status' },
    `最近扫描：${sync.last_scan.at}，识别 ${sync.last_scan.files} 个文件，导入 ${sync.last_scan.imported} 个`);
  const scan = h('button', { class: 'btn primary', type: 'button' }, '立即扫描（示例）');
  scan.addEventListener('click', async () => {
    scan.disabled = true;
    try {
      const receipt = await api('/sync/scan', { method: 'POST' });
      if (receipt.status === 'ok') {
        status.textContent = `示例扫描：识别 ${receipt.files} 个文件，导入 ${receipt.imported} 个（演示数据不会变化）`;
        toast('示例模式：不会访问本地同步目录');
      }
    } catch (error) { toast(error.message, { bad: true }); }
    finally { scan.disabled = false; }
  });
  const row = (file) => h('tr', {},
    h('td', { class: 'mono' }, file.file),
    h('td', {}, SYNC_STATUS[file.status] || file.status || ''),
    h('td', { class: 'sub' }, [file.project_id, file.cases?.length ? `${file.cases.length} 条用例` : '', file.reason].filter(Boolean).join(' · ')));
  return card('同步与错误目录',
    h('div', { class: 'stack', style: 'gap:4px' },
      h('div', { class: 'sub' }, '同步目录：', h('code', { class: 'mono' }, sync.sync_dir)),
      h('div', { class: 'sub' }, '错误目录：', h('code', { class: 'mono' }, sync.error_dir))),
    h('p', { class: 'sub' }, '开发把通过状态的用例文件同步到这里后，自动导入项目用例库；失败回放按日期、项目和运行归档到错误目录。'),
    h('div', { class: 'row wrap' }, scan), status,
    h('table', { class: 'table kv' }, h('tbody', {}, sync.files.map(row))));
}


export async function render(view) {
  const sync = await api('/sync').catch(() => null);
  const syncPanel = sync ? syncCard(sync) : h('p', { class: 'sub' }, '示例中没有同步目录数据。');
  const notice = h('p', { class: 'sub', role: 'status', hidden: true });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const choose = h('input', { type: 'file', class: 'input', id: 'preview-backup', accept: '.json,application/json' });
  const download = h('button', { class: 'btn primary', type: 'button' }, '导出全部数据');
  const restore = h('button', { class: 'btn', type: 'button', disabled: true }, '恢复备份');
  let busy = false;
  function available() {
    download.disabled = busy;
    choose.disabled = busy;
    restore.disabled = busy || !choose.files.length;
  }
  async function perform(action) {
    if (busy) return;
    busy = true;
    available();
    notice.hidden = true;
    error.hidden = true;
    try {
      const message = await action();
      if (message) {
        notice.textContent = message;
        notice.hidden = false;
      }
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
    } finally {
      busy = false;
      available();
    }
  }
  choose.addEventListener('change', () => { error.hidden = true; notice.hidden = true; available(); });
  download.addEventListener('click', () => perform(async () => {
    const backup = { format: FORMAT, version: VERSION, data: await getData() };
    const url = URL.createObjectURL(new Blob([JSON.stringify(backup)], { type: 'application/json' }));
    const filename = 'autoSkill-预览备份-' + new Date().toISOString().slice(0, 10) + '.json';
    const link = h('a', { href: url, download: filename });
    document.body.append(link);
    try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
    return '已开始下载 ' + filename;
  }));
  restore.addEventListener('click', () => perform(async () => {
    const file = choose.files[0];
    let backup;
    try { backup = JSON.parse(await file.text()); }
    catch { throw new Error('无法读取备份，请选择有效的 JSON 文件。'); }
    if (backup?.format !== FORMAT || backup.version !== VERSION || !backup.data || typeof backup.data !== 'object' || Array.isArray(backup.data)) {
      throw new Error('请选择由此预览导出的数据备份。');
    }
    if (!await confirm({ title: '恢复这份备份？', text: file.name + ' 将替换本页的预览数据。', action: '恢复', danger: true })) return;
    await restoreData(backup.data);
    choose.value = '';
    await Promise.all([loadPreferences(), refreshShell()]);
    return '已恢复 ' + file.name;
  }));
  replace(view,
    h('div', { class: 'page-head' }, h('h1', {}, '数据管理')),
    h('div', { class: 'banner' }, '刷新页面会还原示例数据。示例包含开发/测试环境：在「测试用例」页筛选单一项目后，顶部可切换运行环境；同步与错误目录使用演示数据。'),
    syncPanel,
    card('导出备份', h('div', { class: 'row' }, download)),
    card('导入备份', h('div', { class: 'field' }, h('label', { for: 'preview-backup' }, '选择备份文件'), choose),
      h('div', { class: 'row' }, restore)), notice, error);
}
