// Authoritative project knowledge, case files, reports and portable share packages.
import { h, icon, replace, api, scope, go, route, refresh, attempt, toast, fmtNumber } from '../core.js';
import { drawer, closeOverlay, confirm, empty, errorState } from '../ui.js';
import { refreshShell } from '../shell.js';

const TABS = [['knowledge', '知识库'], ['testcases', '用例文件'], ['reports', '报告'], ['share', '分享与导入']];
const size = (bytes) => bytes < 1024 ? bytes + ' B' : bytes < 1048576 ? (bytes / 1024).toFixed(1) + ' KB' : (bytes / 1048576).toFixed(1) + ' MB';

export async function render(view) {
  let index;
  let data;
  const params = route().params;
  const tab = TABS.some(([key]) => key === params.get('tab')) ? params.get('tab') : 'knowledge';
  try {
    index = await api('/project-materials');
    const selected = params.get('project') || scope().projects[0] || index.projects[0]?.id;
    if (selected && index.projects.some((project) => project.id === selected)) data = await api('/project-materials/' + encodeURIComponent(selected));
  } catch (error) { replace(view, h('div', { class: 'card' }, errorState(error, refresh))); return; }
  const projectId = data?.project.id;
  const project = h('select', { class: 'select', 'aria-label': '资料所属项目', onChange: () => go('project-materials', { project: project.value, tab }) },
    index.projects.map((item) => h('option', { value: item.id, selected: item.id === projectId }, item.name)));
  const tabs = h('div', { class: 'seg', role: 'group', 'aria-label': '项目资料分类' }, TABS.map(([key, label]) => h('button', {
    'aria-pressed': String(key === tab), onClick: () => go('project-materials', { project: projectId, tab: key }),
  }, label)));
  const exporting = h('div', { role: 'status', 'aria-live': 'polite' });
  const exportButton = h('button', { class: 'btn', disabled: !projectId, onClick: async () => {
    const result = await attempt(() => api(`/project-materials/${encodeURIComponent(projectId)}/export`, { method: 'POST', body: {} }), exportButton);
    if (result) replace(exporting, exportReceipt(result, '下载项目资料 ZIP'));
  } }, icon('folder'), '导出全部项目资料');
  const content = tab === 'share' ? sharePanel(projectId) : data ? fileList(data, tab)
    : h('section', { class: 'card' }, empty({ glyph: 'folder', title: '还没有项目资料', text: '新建项目后，知识库、用例和报告会集中显示在这里。' }));
  replace(view, h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '项目资料'),
    h('p', { class: 'sub' }, '每个项目使用同一资料目录，分别保存知识库、用例和报告。')), exportButton),
  h('div', { class: 'toolbar wrap' }, index.projects.length > 0 && project, tabs),
  data && h('div', { class: 'card card-body' }, h('div', { class: 'sub' }, '本机项目目录'), h('code', {}, data.root),
    h('p', { class: 'hint' }, 'knowledge / 知识库　·　testcases / 用例　·　reports / 报告。旧报告保留原文件，导出时一起收集。')),
  exporting, content);
}

function fileList(data, category) {
  const files = data.items.filter((item) => item.category === category);
  const title = TABS.find(([key]) => key === category)[1];
  const add = category === 'knowledge' && h('button', { class: 'btn primary sm', onClick: () => editKnowledge(data.project.id) }, icon('plus'), '新建知识');
  const table = files.length ? h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['文件', '大小', '内容校验', '操作'].map((label) => h('th', {}, label)))),
    h('tbody', {}, files.map((item) => h('tr', {},
      h('td', {}, h('div', { class: 'title' }, item.name), item.historical && h('span', { class: 'tag' }, '历史原件')),
      h('td', { class: 'sub num' }, size(item.bytes)),
      h('td', { class: 'sub mono', title: item.sha256 }, item.sha256.slice(0, 12)),
      h('td', { class: 'actions' }, category === 'knowledge' && item.text && h('button', { class: 'btn sm', onClick: () => editKnowledge(data.project.id, item) }, '查看 / 编辑'),
        h('a', { class: 'btn sm', href: item.download_url, download: '' }, '下载'),
        category === 'knowledge' && h('button', { class: 'btn sm danger', onClick: (event) => removeKnowledge(data.project.id, item, event.currentTarget) }, '删除'))))))
    : empty({ glyph: 'folder', title: '还没有' + title, text: category === 'reports' ? '录制、执行或工作流完成后，报告自动保存在项目的 reports 子目录。'
      : category === 'knowledge' ? '把业务规则、测试经验和参考资料保存到当前项目的知识库。' : '在「项目与用例」录制或导入用例后，这里显示权威用例文件。', actions: add });
  return h('section', { class: 'card', style: 'margin-top:16px;overflow-x:auto' },
    h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('h2', {}, title), h('p', { class: 'sub' }, fmtNumber(files.length) + ' 个文件')), files.length > 0 && add), table);
}

async function editKnowledge(projectId, item) {
  let saved;
  if (item) {
    try { saved = await api(`/project-materials/${encodeURIComponent(projectId)}/knowledge?file=${encodeURIComponent(item.name)}&all=1`); }
    catch (error) { toast(error.message, { bad: true }); return; }
  }
  const name = h('input', { class: 'input', id: 'knowledge-name', value: item?.name || '', disabled: !!item, placeholder: '例如：订单/退款规则.md' });
  const content = h('textarea', { class: 'input mono', id: 'knowledge-content', rows: 20, value: saved?.content || '', style: 'min-height:320px;resize:vertical' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const save = h('button', { class: 'btn primary', onClick: async () => {
    save.disabled = true;
    error.hidden = true;
    try {
      await api(`/project-materials/${encodeURIComponent(projectId)}/knowledge`, { method: 'PUT', body: {
        file: name.value, content: content.value, expected_hash: saved?.content_revision || null,
      } });
      closeOverlay();
      toast('知识已保存到项目原文件');
      refresh();
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { save.disabled = false; }
  } }, '保存');
  drawer({ title: item ? '查看与编辑知识' : '新建知识', subtitle: '直接维护项目 knowledge 目录；版本变化时会提示重新读取。', wide: true,
    body: [h('div', { class: 'field' }, h('label', { for: 'knowledge-name' }, '文件名'), name),
      h('div', { class: 'field' }, h('label', { for: 'knowledge-content' }, '内容'), content), error],
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save] });
}

async function removeKnowledge(projectId, item, button) {
  if (!(await confirm({ title: '删除这份知识？', text: item.name, action: '删除', danger: true }))) return;
  if (await attempt(() => api(`/project-materials/${encodeURIComponent(projectId)}/knowledge`, { method: 'DELETE',
    body: { file: item.name, expected_hash: item.sha256 } }), button)) { toast('已删除'); refresh(); }
}

function exportReceipt(result, label) {
  return h('div', { class: 'banner info', style: 'margin-top:16px' }, icon('check'), h('div', { class: 'grow' },
    h('b', {}, '资料包已生成 · ' + size(result.bytes)), h('p', {}, result.note || '文件已生成，可下载。')),
  h('a', { class: 'btn', href: result.download_url, download: '' }, label));
}

function sharePanel(projectId) {
  const format = h('select', { class: 'select', id: 'share-format' },
    h('option', { value: 'project' }, '项目分享包'), h('option', { value: 'full' }, '完整程序包（包含已保存凭据和登录态）'));
  const include = h('input', { class: 'check', type: 'checkbox', id: 'share-secrets' });
  const passphrase = h('input', { class: 'input', type: 'password', id: 'share-passphrase', placeholder: '携带项目凭据时填写加密口令', autocomplete: 'new-password' });
  format.addEventListener('change', () => { include.disabled = format.value === 'full'; passphrase.disabled = format.value === 'full'; });
  const exported = h('div', { role: 'status' });
  const exportButton = h('button', { class: 'btn primary', disabled: !projectId, onClick: async () => {
    const result = await attempt(() => api('/shares/export', { method: 'POST', body: {
      project_ids: [projectId], full: format.value === 'full', include_secrets: include.checked, passphrase: passphrase.value,
    } }), exportButton);
    if (result) replace(exported, exportReceipt(result, '下载分享包'));
  } }, '生成分享包');
  const upload = h('input', { class: 'input', type: 'file', accept: '.zip,application/zip', id: 'share-upload' });
  const source = h('input', { class: 'input', id: 'share-source', placeholder: '例如：/home/me/autoskill-share.zip' });
  const secret = h('input', { class: 'input', type: 'password', id: 'share-import-key', autocomplete: 'off', placeholder: '分享包有加密凭据时填写' });
  const overwrite = h('input', { class: 'check', type: 'checkbox', id: 'share-overwrite' });
  const imported = h('div', { role: 'status', 'aria-live': 'polite' });
  const importButton = h('button', { class: 'btn primary', onClick: async () => {
    const result = await attempt(() => {
      if (upload.files.length) return uploadShare(upload.files[0], overwrite.checked, secret.value);
      return api('/shares/import', { method: 'POST', body: { source: source.value, overwrite: overwrite.checked, passphrase: secret.value } });
    }, importButton);
    if (!result) return;
    replace(imported, h('div', { class: 'banner info', style: 'margin-top:16px' }, h('div', {},
      h('b', {}, `已导入 ${result.imported.length} 个项目 · ${result.skipped.length} 个未导入`),
      result.skipped.map((item) => h('p', {}, `${item.id}：${item.reason}`)), h('p', {}, result.next))));
    await refreshShell();
  } }, '导入项目分享包');
  return h('div', { class: 'stack', style: 'margin-top:16px' },
    h('section', { class: 'card card-body stack' }, h('h2', {}, '项目分享'),
      h('p', { class: 'sub' }, '分享包用于另一台 autoSkill 导入项目。全部资料 ZIP 用于归档与后续上传；完整历史与证据恢复使用「数据管理 → 导出全部数据」。'),
      h('div', { class: 'field' }, h('label', { for: 'share-format' }, '包类型'), format),
      h('label', { class: 'row', for: 'share-secrets' }, include, '项目分享包携带引用的本机凭据（加密）'), passphrase, exportButton, exported),
    h('section', { class: 'card card-body stack' }, h('h2', {}, '导入项目分享包'),
      h('div', { class: 'field' }, h('label', { for: 'share-upload' }, '从这台设备选择 ZIP'), upload),
      h('details', { class: 'more' }, h('summary', {}, '或使用 autoSkill 服务所在电脑上的文件'),
        h('div', { class: 'field' }, h('label', { for: 'share-source' }, '服务电脑上的 ZIP 路径'), source,
          h('span', { class: 'hint' }, '未选择上传文件时使用此路径。'))),
      h('div', { class: 'field' }, h('label', { for: 'share-import-key' }, '凭据解密口令'), secret),
      h('label', { class: 'row', for: 'share-overwrite' }, overwrite, '替换本机已有同名项目'), importButton, imported));
}

async function uploadShare(file, overwrite, passphrase) {
  const bytes = new TextEncoder().encode(passphrase);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const response = await fetch('/api/shares/upload', { method: 'POST', body: file, headers: {
    'Content-Type': 'application/zip', 'x-autoskill-overwrite': overwrite ? '1' : '0', 'x-autoskill-passphrase': btoa(binary),
  } });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || '导入没有完成');
  return result;
}
