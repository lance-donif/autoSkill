// Browser backups contain the editable preview data for this site.
import { h, replace } from '../skills/autoskill/app/ui/js/core.js';
import { confirm } from '../skills/autoskill/app/ui/js/ui.js';
import { loadPreferences } from '../skills/autoskill/app/ui/js/preferences.js';
import { refreshShell } from '../skills/autoskill/app/ui/js/shell.js';
import { getData, restoreData } from './store.js';

const FORMAT = 'autoskill-browser-backup';
const VERSION = 1;

function card(title, ...content) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, title)),
    h('div', { class: 'card-body stack' }, ...content));
}

export function render(view) {
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
    h('div', { class: 'banner' }, '刷新页面会还原示例数据。'),
    card('导出备份', h('div', { class: 'row' }, download)),
    card('导入备份', h('div', { class: 'field' }, h('label', { for: 'preview-backup' }, '选择备份文件'), choose),
      h('div', { class: 'row' }, restore)), notice, error);
}
