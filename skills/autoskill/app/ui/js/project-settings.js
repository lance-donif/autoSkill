// Runtime configuration and machine-local credentials in the project settings drawer.
import { h, replace, api, attempt, toast } from './core.js';

const field = (label, control) => h('div', { class: 'field' }, h('label', { for: control.id }, label), control);

export function runtimeFields(project) {
  const timeout = h('input', { class: 'input', type: 'number', min: 0, step: 'any', id: 'ps-timeout', value: project.timeout_seconds ?? 20 });
  const pageLoad = h('input', { class: 'input', type: 'number', min: 0, step: 'any', id: 'ps-page-load', value: project.page_load_seconds ?? 90 });
  const tls = h('input', { class: 'check', type: 'checkbox', id: 'ps-tls', checked: project.ignore_https_errors });
  const seconds = [['timeout_seconds', timeout, timeout.valueAsNumber], ['page_load_seconds', pageLoad, pageLoad.valueAsNumber]];
  const ignoreErrors = tls.checked;
  return { node: h('div', { class: 'stack' }, h('h3', {}, '执行设置'), field('操作超时（秒）', timeout), field('页面加载超时（秒）', pageLoad),
    h('label', { class: 'row', for: 'ps-tls' }, tls, '忽略 HTTPS 证书错误')), changes() {
    const changed = {};
    for (const [key, input, original] of seconds) {
      const value = input.valueAsNumber;
      if (Object.is(value, original)) continue;
      if (!Number.isFinite(value) || value <= 0) throw new Error('超时需要是大于零的秒数');
      changed[key] = value;
    }
    if (tls.checked !== ignoreErrors) changed.ignore_https_errors = tls.checked;
    return changed;
  } };
}

export function credentialControls(project, changed) {
  const rows = new Map();
  const list = h('div', { class: 'stack', style: 'gap:12px' });
  // The reference tag only distinguishes rows when unreferenced credentials are listed too.
  const syncTags = () => {
    const mixed = [...rows.values()].some((item) => !item.tag);
    for (const item of rows.values()) if (item.tag) item.tag.hidden = !mixed;
  };
  function addRow(name, value, referenced) {
    if (rows.has(name)) { rows.get(name).input.value = value || ''; return; }
    const input = h('input', { class: 'input', type: 'text', value: value || '', placeholder: '还没有填写', autocomplete: 'off', 'aria-label': name });
    const save = h('button', { class: 'btn', onClick: async () => {
      if (await attempt(() => api('/secrets/' + encodeURIComponent(name), { method: 'PUT', body: { value: input.value } }), save)) {
        toast(`${name} 已保存`); await changed();
      }
    } }, '保存');
    const remove = h('button', { class: 'btn danger', 'aria-label': '删除凭据 ' + name, onClick: async () => {
      if (await attempt(() => api('/secrets/' + encodeURIComponent(name), { method: 'DELETE' }), remove)) {
        if (referenced) input.value = ''; else { row.remove(); rows.delete(name); syncTags(); }
        toast(`${name} 已删除`); await changed();
      }
    } }, '删除');
    const tag = referenced && h('span', { class: 'tag', hidden: true }, '本项目引用');
    const row = h('div', { class: 'stack', style: 'gap:4px' }, h('div', { class: 'row wrap' }, h('span', { class: 'mono' }, name), tag, input, save, remove));
    rows.set(name, { input, tag });
    list.append(row);
    syncTags();
  }
  for (const [name, value] of Object.entries(project.credentials || {})) addRow(name, value, true);
  const name = h('input', { class: 'input', id: 'ps-secret-name', placeholder: '例如：ORDER_TOKEN', autocomplete: 'off' });
  const value = h('input', { class: 'input', id: 'ps-secret-value', placeholder: '凭据内容', autocomplete: 'off' });
  const add = h('button', { class: 'btn', onClick: async () => {
    const secret = name.value.trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(secret) || !value.value) {
      toast('请填写有效的凭据名称与内容，名称使用字母、数字和下划线且不能以数字开头', { bad: true });
      return;
    }
    if (await attempt(() => api('/secrets/' + encodeURIComponent(secret), { method: 'PUT', body: { value: value.value } }), add)) {
      addRow(secret, value.value, Object.hasOwn(project.credentials || {}, secret));
      name.value = ''; value.value = ''; toast(`${secret} 已保存`); await changed();
    }
  } }, iconPlus(), '添加凭据');
  const load = h('button', { class: 'btn', onClick: async () => {
    const data = await attempt(() => api('/secrets'), load);
    if (data) { for (const item of data.items) addRow(item.name, item.value, Object.hasOwn(project.credentials || {}, item.name)); }
  } }, '查看全部本机凭据');
  const status = h('div');
  if (!rows.size) replace(status, h('p', { class: 'sub' }, '这个项目暂未引用本机凭据。'));
  return h('div', { class: 'stack' }, h('h3', {}, '凭据'), status, list,
    h('details', { class: 'more' }, h('summary', {}, '添加与管理本机凭据'),
      h('div', { class: 'stack', style: 'margin-top:12px' }, field('凭据名称', name), field('凭据内容', value), h('div', { class: 'row wrap' }, add, load))),
    h('p', { class: 'hint sub' }, '凭据按名称在本机共享，引用同名凭据的其他项目也会使用这个值。明文保存在当前数据目录的 .local/credentials.json；普通分享包可选择加密携带，完整包包含已保存的值。'));
}

function iconPlus() { return h('span', { 'aria-hidden': 'true' }, '+'); }
