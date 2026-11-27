// Side-by-side sync conflict resolution drawer with independent case and env/account selection.
import { h, api, attempt, toast } from './core.js';
import { drawer, closeOverlay } from './ui.js';

function formatValue(value) {
  if (value === null || value === undefined) return '（无）';
  if (typeof value === 'string') return value || '（空）';
  return JSON.stringify(value, null, 2);
}

function diffCard({ title, meta, diffFields, local, incoming, checked, checkboxLabel, onToggle }) {
  const box = h('input', { class: 'check', type: 'checkbox', checked, 'aria-label': checkboxLabel,
    onChange: () => onToggle(box.checked) });
  const tags = Array.isArray(diffFields) ? diffFields.filter(Boolean).map((name) => h('span', { class: 'tag' }, name)) : [];
  return h('div', { class: 'conflict-item stack' },
    h('div', { class: 'row wrap', style: 'gap:8px' },
      h('label', { class: 'row' }, box, h('b', {}, title)),
      meta && h('span', { class: 'sub mono' }, meta),
      tags),
    h('div', { class: 'conflict-diff' },
      h('div', { class: 'conflict-col stack' }, h('span', { class: 'sub' }, '当前版本'), h('pre', { class: 'code' }, formatValue(local))),
      h('div', { class: 'conflict-col stack' }, h('span', { class: 'sub' }, '传入版本'), h('pre', { class: 'code' }, formatValue(incoming)))));
}

function sectionBlock(title, items, selectedSet, buildItem, repaint) {
  if (!items.length) return null;
  const selectAll = h('button', { class: 'btn sm', type: 'button', onClick: () => {
    for (const item of items) selectedSet.add(item.key ?? item.id);
    repaint();
  } }, '全选传入');
  const clearAll = h('button', { class: 'btn sm', type: 'button', onClick: () => {
    selectedSet.clear();
    repaint();
  } }, '全部保留当前');
  return h('section', { class: 'stack conflict-section' },
    h('div', { class: 'row wrap', style: 'gap:8px' },
      h('h3', { class: 'grow' }, `${title} (${items.length})`),
      selectAll, clearAll),
    items.map((item) => buildItem(item)));
}

function resolvePayload(conflict, mode, selectedCases, selectedEnvs, selectedAccounts, useIncomingAuth) {
  if (mode === 'keep_local' || mode === 'use_incoming') {
    const body = { cases: { mode }, environments: { mode }, accounts: { mode } };
    if (conflict.auth?.changed) body.auth = mode;
    return body;
  }
  const caseKeys = [...selectedCases];
  const envIds = [...selectedEnvs];
  const accIds = [...selectedAccounts];
  const body = {
    cases: caseKeys.length ? { mode: 'custom', use_incoming_keys: caseKeys } : { mode: 'keep_local' },
    environments: envIds.length ? { mode: 'custom', use_incoming_ids: envIds } : { mode: 'keep_local' },
    accounts: accIds.length ? { mode: 'custom', use_incoming_ids: accIds } : { mode: 'keep_local' },
  };
  if (conflict.auth?.changed) body.auth = useIncomingAuth ? 'use_incoming' : 'keep_local';
  return body;
}

export async function openConflictDrawer(conflictId, onResolved) {
  let conflict;
  try {
    conflict = await api('/sync/conflicts/' + encodeURIComponent(conflictId));
  } catch (error) {
    toast(error.message || '读取冲突详情失败', { bad: true });
    return;
  }
  const cases = Array.isArray(conflict.cases) ? conflict.cases : [];
  const environments = Array.isArray(conflict.environments) ? conflict.environments : [];
  const accounts = Array.isArray(conflict.accounts) ? conflict.accounts : [];
  const selectedCases = new Set();
  const selectedEnvs = new Set();
  const selectedAccounts = new Set();
  let useIncomingAuth = false;

  const container = h('div', { class: 'stack' });

  function paint() {
    const authBlock = conflict.auth?.changed ? h('section', { class: 'stack conflict-section' },
      h('h3', {}, '登录配置'),
      diffCard({
        title: '使用传入登录配置', meta: '', diffFields: [], local: conflict.auth.local, incoming: conflict.auth.incoming,
        checked: useIncomingAuth, checkboxLabel: '使用传入登录配置',
        onToggle: (on) => { useIncomingAuth = on; },
      })) : null;
    container.replaceChildren(...[
      sectionBlock('业务用例', cases, selectedCases, (item) => {
        const key = item.key ?? item.case_id;
        return diffCard({
          title: item.title || item.case_id || key, meta: item.case_id || '', diffFields: item.diff_fields,
          local: item.local, incoming: item.incoming, checked: selectedCases.has(key),
          checkboxLabel: `使用传入用例 ${item.case_id || key}`,
          onToggle: (on) => { if (on) selectedCases.add(key); else selectedCases.delete(key); },
        });
      }, paint),
      sectionBlock('运行环境', environments, selectedEnvs, (item) => diffCard({
        title: item.name || item.id, meta: item.id, diffFields: item.diff_fields,
        local: item.local, incoming: item.incoming, checked: selectedEnvs.has(item.id),
        checkboxLabel: `使用传入环境 ${item.name || item.id}`,
        onToggle: (on) => { if (on) selectedEnvs.add(item.id); else selectedEnvs.delete(item.id); },
      }), paint),
      sectionBlock('共享账号', accounts, selectedAccounts, (item) => diffCard({
        title: item.label || item.id, meta: item.platform_id || item.id, diffFields: item.diff_fields,
        local: item.local, incoming: item.incoming, checked: selectedAccounts.has(item.id),
        checkboxLabel: `使用传入账号 ${item.label || item.id}`,
        onToggle: (on) => { if (on) selectedAccounts.add(item.id); else selectedAccounts.delete(item.id); },
      }), paint),
      authBlock,
    ].filter(Boolean));
  }

  async function submit(mode, button) {
    const payload = resolvePayload(conflict, mode, selectedCases, selectedEnvs, selectedAccounts, useIncomingAuth);
    const done = await attempt(() => api(`/sync/conflicts/${encodeURIComponent(conflict.id)}/resolve`, {
      method: 'POST', body: payload,
    }), button);
    if (done) {
      closeOverlay();
      toast('冲突已处理');
      if (onResolved) onResolved(done);
    }
  }

  const exportBoth = h('a', { class: 'btn', href: `/api/sync/conflicts/${encodeURIComponent(conflict.id)}/export-both`, download: '' }, '导出两版');
  const keepLocal = h('button', { class: 'btn', type: 'button', onClick: () => submit('keep_local', keepLocal) }, '保留当前');
  const useIncoming = h('button', { class: 'btn', type: 'button', onClick: () => submit('use_incoming', useIncoming) }, '使用传入');
  const applyCustom = h('button', { class: 'btn primary', type: 'button', onClick: () => submit('custom', applyCustom) }, '按勾选应用');

  paint();
  const subtitle = [conflict.project_name || conflict.project_id, conflict.source_file].filter(Boolean).join(' · ');
  drawer({
    title: '同步冲突处理', subtitle, wide: true,
    body: [container],
    foot: [exportBoth, h('span', { class: 'grow' }), keepLocal, useIncoming, applyCustom],
  });
}
