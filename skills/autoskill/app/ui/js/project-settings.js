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

// Postman-style environments: one project keeps several addresses and variable sets.
export function environmentFields(project) {
  const environments = (project.environments || []).map((item) => ({
    id: item.id, name: item.name, base_url: item.base_url || '', is_default: !!item.is_default,
    variables: { ...(item.variables || {}) },
  }));
  let active = project.active_environment_id || '';
  const original = JSON.stringify({ environments, active });
  const list = h('div', { class: 'stack', style: 'gap:12px' });
  const hint = h('p', { class: 'hint sub' }, environments.length ? '' : '还没有环境：添加后即可在用例表和顶部范围栏一键切换。');

  function variableRow(env, name = '', value = '') {
    let key = name;
    const keyInput = h('input', { class: 'input mono', value: name, placeholder: '变量名', 'aria-label': '变量名', autocomplete: 'off' });
    const valueInput = h('input', { class: 'input', value: String(value), placeholder: '值', 'aria-label': '变量值', autocomplete: 'off' });
    const apply = () => {
      const next = keyInput.value.trim();
      if (key !== next) delete env.variables[key];
      key = next;
      if (next) env.variables[next] = valueInput.value;
    };
    keyInput.addEventListener('change', apply);
    valueInput.addEventListener('input', apply);
    const remove = h('button', { class: 'btn sm danger', type: 'button', 'aria-label': '删除变量', onClick: () => {
      if (key) delete env.variables[key];
      row.remove();
    } }, '删除');
    const row = h('div', { class: 'row wrap', style: 'gap:8px' }, keyInput, valueInput, remove);
    return row;
  }

  function envBlock(env) {
    const name = h('input', { class: 'input', value: env.name, placeholder: '环境名称，例如 测试', 'aria-label': '环境名称', autocomplete: 'off' });
    name.addEventListener('input', () => { env.name = name.value; });
    const base = h('input', { class: 'input mono', value: env.base_url, placeholder: 'http://10.0.0.8:8080；留空沿用项目地址', 'aria-label': '环境地址', autocomplete: 'off' });
    base.addEventListener('input', () => { env.base_url = base.value.trim(); });
    const isActive = h('input', { class: 'check', type: 'radio', name: 'ps-env-active', id: 'ps-env-active-' + env.id, checked: active === env.id });
    isActive.addEventListener('change', () => {
      if (!isActive.checked) return;
      active = env.id;
    });
    const isDefault = h('input', { class: 'check', type: 'checkbox', id: 'ps-env-default-' + env.id, checked: env.is_default });
    isDefault.addEventListener('change', () => {
      if (isDefault.checked) {
        for (const other of environments) other.is_default = other === env;
        for (const box of list.querySelectorAll('.ps-env-default')) box.checked = box.id === 'ps-env-default-' + env.id;
      } else {
        env.is_default = false;
      }
    });
    isDefault.className = 'check ps-env-default';
    const variables = h('div', { class: 'stack', style: 'gap:8px' },
      Object.entries(env.variables).map(([key, value]) => variableRow(env, key, value)));
    const addVariable = h('button', { class: 'btn sm', type: 'button', onClick: () => variables.append(variableRow(env)) }, '+ 变量');
    const remove = h('button', { class: 'btn sm danger', type: 'button', onClick: () => {
      environments.splice(environments.indexOf(env), 1);
      if (active === env.id) active = environments[0]?.id || '';
      paint();
    } }, '删除环境');
    return h('div', { class: 'stack', style: 'gap:10px;border:1px solid var(--line);border-radius:var(--r-md);padding:12px' },
      h('div', { class: 'row wrap' }, h('label', { class: 'grow', for: name.id = 'ps-env-name-' + env.id }, '名称', name)),
      h('div', { class: 'row wrap' }, h('label', { class: 'grow', for: base.id = 'ps-env-base-' + env.id }, '地址', base)),
      h('div', { class: 'row wrap' },
        h('label', { class: 'row', for: isActive.id }, isActive, '运行使用此环境'),
        h('label', { class: 'row', for: isDefault.id }, isDefault, '默认环境')),
      h('div', { class: 'sub' }, '变量（用例中写 {{名称}} 引用）'), variables,
      h('div', { class: 'row wrap' }, addVariable, h('span', { class: 'grow' }), remove));
  }

  function paint() {
    replace(list, environments.map(envBlock));
    hint.textContent = environments.length ? '' : '还没有环境：添加后即可在用例表和顶部范围栏一键切换。';
  }
  paint();

  const add = h('button', { class: 'btn', type: 'button', onClick: () => {
    let number = environments.length + 1;
    while (environments.some((item) => item.id === 'env-' + number)) number += 1;
    environments.push({ id: 'env-' + number, name: '环境 ' + number, base_url: '', is_default: environments.length === 0, variables: {} });
    if (!active) active = 'env-' + number;
    paint();
  } }, '添加环境');

  return {
    node: h('div', { class: 'stack' }, h('h3', {}, '运行环境'),
      h('p', { class: 'sub' }, '同一套用例按环境切换地址和变量；切换后立即生效于单条、批量回放和同步用例。'),
      hint, list, add,
      h('p', { class: 'hint sub' }, '敏感凭据继续使用本机凭据库：在用例里写 ${ENV:名称}，不要直接放进环境变量。')),
    changes() {
      if (JSON.stringify({ environments, active }) === original) return {};
      const seen = new Set();
      const rows = environments.map((env) => {
        const name = env.name.trim();
        if (!name) throw new Error('请填写环境名称');
        if (!/^[a-z0-9][a-z0-9_-]{0,47}$/.test(env.id)) throw new Error('环境标识只能使用小写字母、数字、短横线和下划线');
        if (seen.has(env.id)) throw new Error('环境标识重复');
        seen.add(env.id);
        if (env.base_url && !/^https?:\/\/[^\s/]+/.test(env.base_url)) throw new Error(`环境「${name}」的地址需要是完整网址`);
        for (const key of Object.keys(env.variables)) {
          if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error(`环境「${name}」的变量名 ${key || '（空）'} 只能使用字母、数字和下划线，且不能以数字开头`);
        }
        return { id: env.id, name, base_url: env.base_url, variables: env.variables, is_default: !!env.is_default };
      });
      if (active && !rows.some((env) => env.id === active)) active = '';
      return { environments: rows, active_environment_id: active };
    },
  };
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
