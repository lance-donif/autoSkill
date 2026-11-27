// Runtime settings, single-environment switcher, two-tab editor and plaintext credentials.
import { h, replace, api, attempt, toast } from './core.js';

const field = (label, control) => h('div', { class: 'field' }, h('label', { for: control.id }, label), control);
const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const ENV_ID_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
const ENTRY_MODES = [['direct', '直接进入'], ['redirect', '登录后跳转'], ['nav', '平台导航进入']];
const AUTH_MODES = [['none', '无需登录'], ['account_password', '账号密码登录']];

export function runtimeFields(project) {
  const timeout = h('input', { class: 'input', type: 'number', min: 0, step: 'any', id: 'ps-timeout', value: project.timeout_seconds ?? 20 });
  const pageLoad = h('input', { class: 'input', type: 'number', min: 0, step: 'any', id: 'ps-page-load', value: project.page_load_seconds ?? 90 });
  const tls = h('input', { class: 'check', type: 'checkbox', id: 'ps-tls', checked: project.ignore_https_errors });
  const seconds = [['timeout_seconds', timeout, timeout.valueAsNumber], ['page_load_seconds', pageLoad, pageLoad.valueAsNumber]];
  const ignoreErrors = tls.checked;
  return {
    node: h('div', { class: 'stack' }, h('h3', {}, '执行设置'), field('操作超时（秒）', timeout), field('页面加载超时（秒）', pageLoad),
      h('label', { class: 'row', for: 'ps-tls' }, tls, '忽略 HTTPS 证书错误')),
    changes() {
      const changed = {};
      for (const [key, input, original] of seconds) {
        const value = input.valueAsNumber;
        if (Object.is(value, original)) continue;
        if (!Number.isFinite(value) || value <= 0) throw new Error('超时需要是大于零的秒数');
        changed[key] = value;
      }
      if (tls.checked !== ignoreErrors) changed.ignore_https_errors = tls.checked;
      return changed;
    },
  };
}

function normalizeEntry(raw) {
  const entry = raw && typeof raw === 'object' ? raw : {};
  return {
    mode: ['direct', 'redirect', 'nav'].includes(entry.mode) ? entry.mode : 'direct',
    login_url: String(entry.login_url || ''),
    target_url: String(entry.target_url || ''),
    nav_label: String(entry.nav_label || ''),
    nav_locators: Array.isArray(entry.nav_locators) ? entry.nav_locators : [],
    verify_url_contains: String(entry.verify_url_contains || ''),
    verify_locators: Array.isArray(entry.verify_locators) ? entry.verify_locators : [],
  };
}

function normalizeEnv(item) {
  const entries = Object.entries(item.variables || {}).map(([key, value]) => ({
    key, value: value === null || value === undefined ? '' : String(value),
  }));
  const accountIds = Array.isArray(item.account_ids) ? [...item.account_ids]
    : Array.isArray(item.accounts) ? item.accounts.map((acc) => acc.id).filter(Boolean) : [];
  return {
    id: item.id,
    name: item.name || '',
    base_url: item.base_url || '',
    is_default: !!item.is_default,
    login_required: !!item.login_required,
    auth_mode: item.auth_mode || (item.login_required ? 'account_password' : 'none'),
    platform_id: item.platform_id || '',
    platform_name: item.platform_name || '',
    entry: normalizeEntry(item.entry),
    account_ids: accountIds,
    default_account_id: item.default_account_id || accountIds[0] || '',
    entries,
  };
}

function entriesToObject(entries, envName) {
  const result = {};
  for (const row of entries) {
    const key = row.key.trim();
    if (!key && !row.value) continue;
    if (!KEY_RE.test(key)) throw new Error(`环境「${envName}」的变量名 ${key || '（空）'} 只能使用字母、数字和下划线，且不能以数字开头`);
    result[key] = row.value;
  }
  return result;
}

function formatBulkText(entries) {
  return entries.filter((row) => row.key.trim()).map((row) => `${row.key.trim()}=${row.value}`).join('\n');
}

function parseBulkText(text) {
  const rows = [];
  for (const raw of String(text || '').split('\n')) {
    const line = raw.replace(/\r$/, '');
    if (!line.trim() || line.trimStart().startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 0) rows.push({ key: line.trim(), value: '' });
    else rows.push({ key: line.slice(0, eq).trim(), value: line.slice(eq + 1) });
  }
  return rows;
}

function serializeEnvironments(environments) {
  const seen = new Set();
  return environments.map((env) => {
    const name = env.name.trim();
    if (!name) throw new Error('请填写环境名称');
    if (!ENV_ID_RE.test(env.id)) throw new Error('环境标识只能使用小写字母、数字、短横线和下划线');
    if (seen.has(env.id)) throw new Error('环境标识重复');
    seen.add(env.id);
    const baseUrl = env.base_url.trim();
    if (baseUrl && !/^https?:\/\/[^\s/]+/.test(baseUrl)) throw new Error(`环境「${name}」的地址需要是完整网址`);
    const variables = entriesToObject(env.entries, name);
    const accountIds = env.account_ids.filter(Boolean);
    const defaultAccountId = accountIds.includes(env.default_account_id) ? env.default_account_id : (accountIds[0] || '');
    return {
      id: env.id, name, base_url: baseUrl, variables, is_default: !!env.is_default,
      login_required: !!env.login_required,
      auth_mode: env.login_required ? (env.auth_mode === 'none' ? 'account_password' : env.auth_mode) : 'none',
      platform_id: env.platform_id.trim(), platform_name: env.platform_name.trim(),
      entry: { ...env.entry, login_url: env.entry.login_url.trim(), target_url: env.entry.target_url.trim(),
        nav_label: env.entry.nav_label.trim(), verify_url_contains: env.entry.verify_url_contains.trim() },
      account_ids: accountIds, default_account_id: defaultAccountId,
    };
  });
}

function seedAccounts(project, platformStore) {
  const map = new Map();
  for (const acc of platformStore?.accounts || []) {
    if (acc?.id) map.set(acc.id, { ...acc });
  }
  for (const env of project.environments || []) {
    for (const acc of env.accounts || []) {
      if (acc?.id) map.set(acc.id, { ...map.get(acc.id), ...acc });
    }
  }
  return map;
}

function sharedProjectsLabel(acc) {
  const list = Array.isArray(acc.shared_projects) ? acc.shared_projects : [];
  if (list.length < 2) return '';
  return list.map((item) => (typeof item === 'string' ? item : item.name || item.id)).filter(Boolean).join('、');
}

function accountCard(env, acc, { markDirty, onDefault, onRemove }) {
  const shared = sharedProjectsLabel(acc);
  const platformTag = acc.platform_name || env.platform_name;
  const envTag = acc.environment_name || env.name;
  const labelInput = h('input', { class: 'input', type: 'text', value: acc.label || '', placeholder: '账号名称', 'aria-label': '账号名称', autocomplete: 'off',
    onInput: () => { acc.label = labelInput.value; markDirty(acc.id); } });
  const userInput = h('input', { class: 'input mono', type: 'text', value: acc.username || '', placeholder: '用户名', 'aria-label': '用户名', autocomplete: 'off',
    onInput: () => { acc.username = userInput.value; markDirty(acc.id); } });
  const secretInput = h('input', { class: 'input mono', type: 'text', value: acc.password || '', placeholder: '密码', 'aria-label': '密码', autocomplete: 'off',
    onInput: () => { acc.password = secretInput.value; markDirty(acc.id); } });
  const isDefault = h('input', { class: 'check', type: 'radio', name: 'ps-default-acc-' + env.id, checked: env.default_account_id === acc.id,
    onChange: () => { if (isDefault.checked) onDefault(acc.id); } });
  return h('div', { class: 'ps-account-card stack' },
    h('div', { class: 'row wrap', style: 'gap:6px' },
      platformTag && h('span', { class: 'tag accent' }, `平台：${platformTag}`),
      envTag && h('span', { class: 'tag' }, `环境：${envTag}`),
      shared && h('span', { class: 'tag' }, `共用：${shared}`),
      h('span', { class: 'grow' }),
      h('label', { class: 'row' }, isDefault, '默认账号'),
      h('button', { class: 'btn sm danger', type: 'button', onClick: () => onRemove(acc.id) }, '移除')),
    h('div', { class: 'ps-account-grid' },
      h('div', { class: 'field' }, h('label', {}, '账号名称'), labelInput),
      h('div', { class: 'field' }, h('label', {}, '用户名'), userInput),
      h('div', { class: 'field' }, h('label', {}, '密码'), secretInput)));
}

function entryFields(env, repaint) {
  const mode = h('select', { class: 'select', id: 'ps-entry-mode-' + env.id, 'aria-label': '项目入口',
    onChange: () => { env.entry.mode = mode.value; repaint(); } },
  ENTRY_MODES.map(([value, label]) => h('option', { value, selected: env.entry.mode === value }, label)));
  const extras = [];
  if (env.entry.mode !== 'direct') {
    const platName = h('input', { class: 'input', type: 'text', value: env.platform_name, placeholder: '例如：业务平台', 'aria-label': '平台名称',
      onInput: () => { env.platform_name = platName.value; } });
    const platId = h('input', { class: 'input mono', type: 'text', value: env.platform_id, placeholder: '例如：erp-main', 'aria-label': '平台标识',
      onInput: () => { env.platform_id = platId.value; } });
    const loginUrl = h('input', { class: 'input mono', type: 'text', value: env.entry.login_url, placeholder: 'https://...', 'aria-label': '登录地址',
      onInput: () => { env.entry.login_url = loginUrl.value; } });
    extras.push(h('div', { class: 'ps-two-col' },
      h('div', { class: 'field' }, h('label', {}, '平台名称'), platName),
      h('div', { class: 'field' }, h('label', {}, '平台标识'), platId)),
    h('div', { class: 'field' }, h('label', {}, '登录地址'), loginUrl));
  }
  if (env.entry.mode === 'redirect') {
    const targetUrl = h('input', { class: 'input mono', type: 'text', value: env.entry.target_url, placeholder: 'https://...', 'aria-label': '目标地址',
      onInput: () => { env.entry.target_url = targetUrl.value; } });
    extras.push(h('div', { class: 'field' }, h('label', {}, '目标地址'), targetUrl));
  }
  if (env.entry.mode === 'nav') {
    const navLabel = h('input', { class: 'input', type: 'text', value: env.entry.nav_label, placeholder: '例如：订单管理', 'aria-label': '导航入口名称',
      onInput: () => { env.entry.nav_label = navLabel.value; } });
    const verifyUrl = h('input', { class: 'input mono', type: 'text', value: env.entry.verify_url_contains, placeholder: '例如：/orders', 'aria-label': '到达核对网址',
      onInput: () => { env.entry.verify_url_contains = verifyUrl.value; } });
    extras.push(h('div', { class: 'ps-two-col' },
      h('div', { class: 'field' }, h('label', {}, '导航入口名称'), navLabel),
      h('div', { class: 'field' }, h('label', {}, '到达核对网址'), verifyUrl)));
  }
  return h('div', { class: 'stack', style: 'gap:10px' }, field('项目入口', mode), extras);
}

function loginSection(env, accountsMap, { markDirty, repaint }) {
  const authMode = h('select', { class: 'select', id: 'ps-auth-mode-' + env.id, 'aria-label': '登录方式',
    onChange: () => {
      env.auth_mode = authMode.value;
      env.login_required = authMode.value !== 'none';
      repaint();
    } },
  AUTH_MODES.map(([value, label]) => h('option', { value, selected: (env.login_required ? env.auth_mode : 'none') === value }, label)));
  if (!env.login_required) return h('div', { class: 'stack', style: 'gap:10px' }, field('登录方式', authMode));

  const linked = env.account_ids.map((id) => accountsMap.get(id)).filter(Boolean);
  const unlinked = [...accountsMap.values()].filter((acc) => !env.account_ids.includes(acc.id));
  const addNew = h('button', { class: 'btn sm', type: 'button', onClick: () => {
    const id = 'acc-' + Date.now().toString(36) + '-' + (accountsMap.size + 1);
    const created = {
      id, platform_id: env.platform_id.trim(), platform_name: env.platform_name.trim(),
      environment_key: env.id, environment_name: env.name.trim(),
      label: `账号 ${linked.length + 1}`, username: '', password: '', variables: {}, shared_projects: [],
    };
    accountsMap.set(id, created);
    env.account_ids.push(id);
    if (!env.default_account_id) env.default_account_id = id;
    markDirty(id);
    repaint();
  } }, '+ 添加账号');

  let attachSelect = null;
  if (unlinked.length) {
    attachSelect = h('select', { class: 'select ps-inline-select', 'aria-label': '关联已有账号', onChange: () => {
      if (!attachSelect.value) return;
      env.account_ids.push(attachSelect.value);
      if (!env.default_account_id) env.default_account_id = attachSelect.value;
      repaint();
    } },
    h('option', { value: '' }, '关联已有账号…'),
    unlinked.map((acc) => h('option', { value: acc.id },
      [acc.label || acc.username || acc.id, acc.platform_name, acc.environment_name].filter(Boolean).join(' · '))));
  }

  return h('div', { class: 'stack', style: 'gap:10px' },
    field('登录方式', authMode),
    linked.length
      ? h('div', { class: 'stack', style: 'gap:10px' }, linked.map((acc) => accountCard(env, acc, {
        markDirty,
        onDefault: (id) => { env.default_account_id = id; },
        onRemove: (id) => {
          env.account_ids = env.account_ids.filter((item) => item !== id);
          if (env.default_account_id === id) env.default_account_id = env.account_ids[0] || '';
          repaint();
        },
      })))
      : h('p', { class: 'sub' }, '未配置账号'),
    h('div', { class: 'row wrap', style: 'gap:8px' }, addNew, attachSelect));
}

function variableTable(env, filterState, onChanged, repaint) {
  const search = filterState.search.trim().toLowerCase();
  const rows = env.entries.filter((entry) => {
    if (filterState.onlyEmpty && entry.value !== '') return false;
    if (!search) return true;
    return entry.key.toLowerCase().includes(search) || entry.value.toLowerCase().includes(search);
  });
  if (!rows.length) return h('p', { class: 'sub' }, env.entries.length ? '没有匹配的变量' : '暂无变量');
  return h('div', { class: 'var-table-wrap' },
    h('table', { class: 'table var-table' },
      h('thead', {}, h('tr', {}, h('th', {}, '变量名'), h('th', {}, '变量值'), h('th', { class: 'actions' }, '操作'))),
      h('tbody', {}, rows.map((entry) => {
        const keyInput = h('input', { class: 'input mono', type: 'text', value: entry.key, placeholder: '变量名', 'aria-label': '变量名', autocomplete: 'off',
          onInput: () => { entry.key = keyInput.value; onChanged(); } });
        const valInput = h('input', { class: 'input' + (entry.value === '' ? ' is-empty' : ''), type: 'text', value: entry.value, placeholder: '值', 'aria-label': '变量值', autocomplete: 'off',
          onInput: () => { entry.value = valInput.value; valInput.classList.toggle('is-empty', entry.value === ''); onChanged(); } });
        const remove = h('button', { class: 'btn sm danger', type: 'button', 'aria-label': '删除变量', onClick: () => {
          const index = env.entries.indexOf(entry);
          if (index >= 0) env.entries.splice(index, 1);
          onChanged();
          repaint();
        } }, '删除');
        return h('tr', {}, h('td', {}, keyInput), h('td', {}, valInput), h('td', { class: 'actions' }, remove));
      }))));
}

function accountPayload(acc) {
  return {
    id: acc.id, platform_id: acc.platform_id || '', platform_name: acc.platform_name || '',
    environment_key: acc.environment_key || '', environment_name: acc.environment_name || '',
    label: acc.label || '', username: acc.username || '', password: acc.password || '',
    variables: acc.variables || {},
  };
}

async function persistAccount(acc) {
  try {
    await api('/platform-accounts/' + encodeURIComponent(acc.id), { method: 'PUT', body: accountPayload(acc) });
  } catch (error) {
    if (error.status !== 404 && error.status !== 405) throw error;
  }
}

export function projectSettingsTabs(project, { runtime, credentials, sessionNode, exportNode, platformStore } = {}) {
  const environments = (project.environments || []).map(normalizeEnv);
  let activeEnvId = project.active_environment_id || '';
  let selectedEnvId = activeEnvId || environments.find((item) => item.is_default)?.id || environments[0]?.id || '';
  let activeTab = 'basic';
  const varFilter = { search: '', onlyEmpty: false, mode: 'table' };
  const accountsMap = seedAccounts(project, platformStore);
  const dirtyAccountIds = new Set();
  const markDirty = (id) => dirtyAccountIds.add(id);
  const snapshot = () => JSON.stringify({ environments: environments.map((env) => ({ ...env, entries: env.entries.filter((r) => r.key.trim() || r.value) })), activeEnvId });
  const original = snapshot();

  const envBar = h('div', { class: 'ps-env-bar stack' });
  const tabBar = h('div', { class: 'seg', role: 'tablist', 'aria-label': '项目设置标签' });
  const tabBody = h('div', { class: 'stack ps-tab-body' });
  let varTabButton = null;

  const currentEnv = () => environments.find((item) => item.id === selectedEnvId) || null;
  const varCount = () => (currentEnv()?.entries || []).filter((row) => row.key.trim()).length;
  const updateVarCount = () => { if (varTabButton) varTabButton.textContent = `变量 (${varCount()})`; };

  function paintEnvBar() {
    const env = currentEnv();
    const envSelect = environments.length ? h('select', { class: 'select ps-env-select', 'aria-label': '选择环境',
      onChange: () => { selectedEnvId = envSelect.value; paintAll(); } },
    environments.map((item) => h('option', { value: item.id, selected: item.id === selectedEnvId },
      item.name + (item.is_default ? '（默认）' : '')))) : null;
    const copySelect = environments.length ? h('select', { class: 'select ps-copy-select', 'aria-label': '新建环境复制变量名' },
      h('option', { value: '' }, '不复制变量名'),
      environments.map((item) => h('option', { value: item.id }, `复制「${item.name || item.id}」变量名 (${item.entries.filter((r) => r.key.trim()).length})`))) : null;
    const addBtn = h('button', { class: 'btn sm', type: 'button', onClick: () => {
      let number = environments.length + 1;
      while (environments.some((item) => item.id === 'env-' + number)) number += 1;
      const source = copySelect?.value ? environments.find((item) => item.id === copySelect.value) : null;
      const copied = source ? source.entries.filter((row) => row.key.trim()).map((row) => ({ key: row.key.trim(), value: '' })) : [];
      const created = normalizeEnv({ id: 'env-' + number, name: '环境 ' + number, base_url: '', is_default: environments.length === 0 });
      created.entries = copied;
      environments.push(created);
      selectedEnvId = created.id;
      if (!activeEnvId) activeEnvId = created.id;
      paintAll();
    } }, '添加环境');
    const removeBtn = env ? h('button', { class: 'btn sm danger', type: 'button', onClick: () => {
      environments.splice(environments.indexOf(env), 1);
      if (activeEnvId === env.id) activeEnvId = environments[0]?.id || '';
      selectedEnvId = environments[0]?.id || '';
      paintAll();
    } }, '删除环境') : null;
    replace(envBar,
      h('div', { class: 'row wrap', style: 'gap:8px' }, h('h3', { class: 'grow' }, '运行环境'), copySelect, addBtn, removeBtn),
      envSelect);
  }

  function paintTabs() {
    const basicBtn = h('button', { type: 'button', role: 'tab', 'aria-selected': String(activeTab === 'basic'),
      onClick: () => { activeTab = 'basic'; paintTabs(); paintBody(); } }, '基础与登录');
    varTabButton = h('button', { type: 'button', role: 'tab', 'aria-selected': String(activeTab === 'vars'),
      onClick: () => { activeTab = 'vars'; paintTabs(); paintBody(); } }, `变量 (${varCount()})`);
    replace(tabBar, basicBtn, varTabButton);
  }

  function paintBasicTab() {
    const env = currentEnv();
    let envPanel = null;
    if (env) {
      const nameInput = h('input', { class: 'input', id: 'ps-env-name-' + env.id, value: env.name, placeholder: '环境名称', 'aria-label': '环境名称', autocomplete: 'off',
        onInput: () => {
          env.name = nameInput.value;
          const opt = envBar.querySelector(`option[value="${CSS.escape(env.id)}"]`);
          if (opt) opt.textContent = (env.name || env.id) + (env.is_default ? '（默认）' : '');
        } });
      const baseInput = h('input', { class: 'input mono', id: 'ps-env-base-' + env.id, value: env.base_url, placeholder: 'https://...', 'aria-label': '环境地址', autocomplete: 'off',
        onInput: () => { env.base_url = baseInput.value.trim(); } });
      const isActive = h('input', { class: 'check', type: 'checkbox', id: 'ps-env-active-' + env.id, checked: activeEnvId === env.id,
        onChange: () => { activeEnvId = isActive.checked ? env.id : ''; } });
      const isDefault = h('input', { class: 'check', type: 'checkbox', id: 'ps-env-default-' + env.id, checked: env.is_default,
        onChange: () => {
          for (const other of environments) other.is_default = isDefault.checked && other === env;
          paintEnvBar();
        } });
      envPanel = h('div', { class: 'ps-env-panel stack' },
        h('div', { class: 'ps-two-col' },
          h('div', { class: 'field' }, h('label', { for: nameInput.id }, '环境名称'), nameInput),
          h('div', { class: 'field' }, h('label', { for: baseInput.id }, '环境地址'), baseInput)),
        h('div', { class: 'row wrap', style: 'gap:16px' },
          h('label', { class: 'row', for: isActive.id }, isActive, '运行使用此环境'),
          h('label', { class: 'row', for: isDefault.id }, isDefault, '默认环境')),
        entryFields(env, paintBody),
        loginSection(env, accountsMap, { markDirty, repaint: paintBody }));
    }
    replace(tabBody, envPanel, runtime?.node, sessionNode, credentials, exportNode);
  }

  function paintVarsTab() {
    const env = currentEnv();
    if (!env) {
      replace(tabBody, h('p', { class: 'sub' }, '请先添加运行环境'));
      return;
    }
    const content = h('div', { class: 'stack' });
    const searchInput = h('input', { class: 'input', type: 'search', value: varFilter.search, placeholder: '搜索变量名或值', 'aria-label': '搜索变量',
      onInput: () => { varFilter.search = searchInput.value; paintVarContent(); } });
    const emptyBox = h('input', { class: 'check', type: 'checkbox', checked: varFilter.onlyEmpty, 'aria-label': '只看空值',
      onChange: () => { varFilter.onlyEmpty = emptyBox.checked; paintVarContent(); } });
    const modeSwitch = h('div', { class: 'seg', role: 'group', 'aria-label': '变量编辑模式' },
      [['table', '表格'], ['bulk', '批量文本']].map(([mode, label]) => h('button', {
        type: 'button', 'aria-pressed': String(varFilter.mode === mode),
        onClick: () => { varFilter.mode = mode; paintVarsTab(); },
      }, label)));
    const addBtn = varFilter.mode === 'table' ? h('button', { class: 'btn sm', type: 'button', onClick: () => {
      env.entries.push({ key: '', value: '' });
      paintVarContent();
      content.querySelector('tbody tr:last-child input')?.focus();
    } }, '+ 变量') : null;

    function paintVarContent() {
      if (varFilter.mode === 'bulk') {
        const area = h('textarea', { class: 'input mono var-bulk-text', 'aria-label': '批量变量文本', placeholder: 'KEY=VALUE',
          value: formatBulkText(env.entries),
          onInput: () => { env.entries = parseBulkText(area.value); updateVarCount(); } });
        replace(content, area);
        return;
      }
      replace(content, variableTable(env, varFilter, updateVarCount, paintVarContent));
    }

    paintVarContent();
    replace(tabBody,
      h('div', { class: 'row wrap ps-var-toolbar' },
        varFilter.mode === 'table' && h('div', { class: 'grow', style: 'min-width:160px' }, searchInput),
        varFilter.mode === 'table' && h('label', { class: 'row' }, emptyBox, '只看空值'),
        modeSwitch, addBtn),
      content);
  }

  function paintBody() {
    if (activeTab === 'vars') paintVarsTab();
    else paintBasicTab();
  }

  function paintAll() {
    paintEnvBar();
    paintTabs();
    paintBody();
  }

  paintAll();

  return {
    node: h('div', { class: 'stack ps-settings' }, envBar, tabBar, tabBody),
    async saveAccounts() {
      for (const id of dirtyAccountIds) {
        const acc = accountsMap.get(id);
        if (acc) await persistAccount(acc);
      }
    },
    changes() {
      if (snapshot() === original) return {};
      const rows = serializeEnvironments(environments);
      const nextActive = activeEnvId && rows.some((item) => item.id === activeEnvId) ? activeEnvId : '';
      return { environments: rows, active_environment_id: nextActive };
    },
  };
}

export function environmentFields(project) {
  return projectSettingsTabs(project);
}

export function credentialControls(project, changed) {
  const rows = new Map();
  const list = h('div', { class: 'stack', style: 'gap:12px' });
  const syncTags = () => {
    const mixed = [...rows.values()].some((item) => !item.tag);
    for (const item of rows.values()) if (item.tag) item.tag.hidden = !mixed;
  };
  function addRow(name, value, referenced) {
    if (rows.has(name)) { rows.get(name).input.value = value || ''; return; }
    const input = h('input', { class: 'input', type: 'text', value: value || '', placeholder: '还没有填写', autocomplete: 'off', 'aria-label': name });
    const save = h('button', { class: 'btn', type: 'button', onClick: async () => {
      if (await attempt(() => api('/secrets/' + encodeURIComponent(name), { method: 'PUT', body: { value: input.value } }), save)) {
        toast(`${name} 已保存`); await changed();
      }
    } }, '保存');
    const remove = h('button', { class: 'btn danger', type: 'button', 'aria-label': '删除凭据 ' + name, onClick: async () => {
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
  const add = h('button', { class: 'btn', type: 'button', onClick: async () => {
    const secret = name.value.trim();
    if (!KEY_RE.test(secret) || !value.value) {
      toast('请填写有效的凭据名称与内容，名称使用字母、数字和下划线且不能以数字开头', { bad: true });
      return;
    }
    if (await attempt(() => api('/secrets/' + encodeURIComponent(secret), { method: 'PUT', body: { value: value.value } }), add)) {
      addRow(secret, value.value, Object.hasOwn(project.credentials || {}, secret));
      name.value = ''; value.value = ''; toast(`${secret} 已保存`); await changed();
    }
  } }, iconPlus(), '添加凭据');
  const load = h('button', { class: 'btn', type: 'button', onClick: async () => {
    const data = await attempt(() => api('/secrets'), load);
    if (data) { for (const item of data.items) addRow(item.name, item.value, Object.hasOwn(project.credentials || {}, item.name)); }
  } }, '查看全部本机凭据');
  const status = h('div');
  if (!rows.size) replace(status, h('p', { class: 'sub' }, '暂未引用本机凭据'));
  return h('div', { class: 'stack' }, h('h3', {}, '凭据'), status, list,
    h('details', { class: 'more' }, h('summary', {}, '添加与管理本机凭据'),
      h('div', { class: 'stack', style: 'margin-top:12px' }, field('凭据名称', name), field('凭据内容', value), h('div', { class: 'row wrap' }, add, load))));
}

function iconPlus() { return h('span', { 'aria-hidden': 'true' }, '+'); }
