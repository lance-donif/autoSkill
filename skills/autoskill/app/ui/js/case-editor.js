// The drawer always starts from the complete canonical definition, never a display projection.
import { h, api, icon, toast, replace, refresh, beforeRouteChange } from './core.js';
import { drawer, closeOverlay } from './ui.js';
import { textField, selectField, section, applyControls } from './editor-fields.js';
import { boundValue } from './value-editor.js';
import { uiCaseEditor } from './case-ui-editor.js';
import { apiCaseEditor } from './case-api-editor.js';

export async function editCase(projectId, caseId, onSaved) {
  let source;
  try { source = await api(`/case-definitions/${projectId}/${encodeURIComponent(caseId)}`); }
  catch (error) { toast(error.message, { bad: true }); return; }
  const draft = structuredClone(source.case);
  draft.extensions ||= {};
  let dirty = false;
  let saving = false;
  let pending = null;
  let releaseRoute = () => undefined;
  const originalHash = location.hash;
  const status = h('span', { class: 'sub grow', role: 'status' });
  const touch = () => { dirty = true; status.textContent = '有未保存的修改'; };
  const common = commonEditor(draft, touch);
  const execute = draft.kind === 'ui' ? uiCaseEditor(draft, source.editing, touch) : apiCaseEditor(draft, touch);
  const errorBox = h('div');
  const departure = h('div', { class: 'editor-departure', hidden: true });
  const askDiscard = (action) => {
    pending = action;
    departure.hidden = false;
    replace(departure, h('div', { class: 'banner', role: 'alert' }, icon('warn'), h('div', { class: 'grow' },
      h('p', {}, '有未保存的修改，离开会丢失本地草稿。'), h('div', { class: 'row wrap' },
        h('button', { class: 'btn sm', type: 'button', onClick: () => { pending = null; departure.hidden = true; } }, '继续编辑'),
        h('button', { class: 'btn sm danger', type: 'button', onClick: () => { const next = pending; dirty = false; closeOverlay(); next?.(); } }, '放弃修改')))));
    departure.scrollIntoView({ block: 'nearest' });
  };
  const beforeClose = () => {
    if (saving) return false;
    if (!dirty) return true;
    askDiscard(null); return false;
  };
  const onRoute = () => {
    if (location.hash === originalHash) return true;
    if (!dirty && !saving) { closeOverlay(); return true; }
    const next = location.hash;
    history.replaceState(null, '', location.pathname + location.search + originalHash);
    if (!saving) askDiscard(() => { location.hash = next; });
    return false;
  };
  const beforeUnload = (event) => { if (dirty || saving) { event.preventDefault(); event.returnValue = ''; } };
  const reloadLatest = () => askDiscard(() => editCase(projectId, caseId, onSaved));
  const save = h('button', { class: 'btn primary', type: 'button', onClick: async () => {
    if (saving) return;
    replace(errorBox);
    try {
      common.apply(); execute.apply();
      saving = true; save.disabled = true; save.textContent = '正在保存…';
      const result = await api(`/projects/${projectId}/cases/${encodeURIComponent(caseId)}`, {
        method: 'PUT', body: { expected_content_revision: source.content_revision, case: draft },
      });
      dirty = false; saving = false;
      closeOverlay(); refresh();
      toast(result.changed ? '已保存，新版本尚未执行。请手动运行检查。' : '用例内容没有变化');
      if (onSaved) onSaved(result);
    } catch (error) {
      const conflict = error.status === 409;
      replace(errorBox, h('div', { class: 'banner bad', role: 'alert' }, icon('warn'), h('div', { class: 'grow' },
        h('p', {}, conflict ? '用例已被其他会话修改。本地草稿已保留，未覆盖远端版本。' : error.message),
        conflict && h('button', { class: 'btn sm', type: 'button', onClick: reloadLatest }, '放弃草稿并读取最新版本'))));
      errorBox.scrollIntoView({ block: 'nearest' });
    } finally { saving = false; save.disabled = false; save.textContent = '保存修改'; }
  } }, '保存修改');
  const panel = drawer({ title: '编辑用例', subtitle: `${draft.id} · ${draft.kind === 'ui' ? '页面用例' : '接口用例'} · 第 ${draft.revision} 版`, wide: true,
    beforeClose, onClose: () => { releaseRoute(); window.removeEventListener('beforeunload', beforeUnload); },
    body: [departure, errorBox, h('p', { class: 'hint' }, '用例 ID、类型、版本与实际结果只读。保存后请手动运行，确认业务预期仍然成立。'), common.node, execute.node],
    foot: [status, h('button', { class: 'btn', type: 'button', onClick: () => closeOverlay() }, '取消'), save] });
  if (panel) {
    panel.classList.add('case-editor');
    releaseRoute = beforeRouteChange(onRoute); window.addEventListener('beforeunload', beforeUnload);
  }
}

function commonEditor(draft, touch) {
  const doc = structuredClone(draft.extensions.test_case_document || {});
  const business = [
    textField(doc, 'description', '功能描述', touch, { multiline: true, fallback: draft.title, required: true }),
    textField(doc, 'preconditions', '前置条件', touch, { multiline: true, fallback: draft.preconditions }),
    businessSteps(doc, draft.steps, touch),
    textField(doc, 'expected', '预期结果', touch, { multiline: true, fallback: draft.expected, required: true }),
  ];
  const metadata = [
    textField(draft, 'title', '用例标题', touch, { required: true }),
    textField(draft, 'module', '菜单名', touch, { required: true }),
    selectField(draft, 'priority', '优先级', ['P0', 'P1', 'P2'], touch),
    boundValue(draft, 'requirement_ids', '需求 ID', { types: ['array'], defaultValue: [], onChange: touch }),
    boundValue(draft, 'test_data', '测试数据', { types: ['string', 'object'], defaultValue: '', onChange: touch }),
    boundValue(draft, 'tags', '标签', { types: ['array'], defaultValue: [], onChange: touch }),
    textField(draft, 'source', '来源说明', touch, { multiline: true, required: true }),
    selectField(draft, 'risk', '业务操作类别', [['read', '读取'], ['write', '写入'], ['delete', '删除'], ['payment', '支付'], ['refund', '退款'], ['message', '消息']], touch),
    selectField(draft, 'lifecycle', '生命周期', [['active', '有效'], ['archived', '已归档']], touch),
  ];
  const documentExtras = [
    textField(doc, 'author', '业务设计作者', touch),
    textField(doc, 'remarks', '业务设计备注', touch, { multiline: true }),
    textField(doc, 'prefix', '显示编号前缀', touch),
    boundValue(doc, 'source', '业务设计来源', { optional: true, types: ['string', 'object', 'array'], defaultValue: '', onChange: touch }),
    boundValue(doc, 'business_path', '业务层级', { optional: true, types: ['array'], defaultValue: [], onChange: touch }),
  ];
  return { node: h('div', { class: 'stack' }, section('业务描述', business, '手动维护功能描述、前置条件、功能步骤和预期结果，不从浏览器动作或接口地址自动生成。'),
    h('details', { class: 'more' }, h('summary', {}, '基本信息与测试数据'), section('用例信息', metadata), section('业务设计补充', documentExtras))), apply: () => {
    applyControls(business); applyControls(metadata); applyControls(documentExtras);
    const changed = [...business, ...documentExtras].some((control) => control.changed);
    if (changed) draft.extensions.test_case_document = doc;
    for (const [index, field] of [[1, 'preconditions'], [2, 'steps'], [3, 'expected']]) {
      if (business[index].changed) draft[field] = structuredClone(doc[field]);
    }
  } };
}

function businessSteps(doc, fallback, touch) {
  let changed = false;
  const original = doc.steps ?? fallback;
  const input = h('textarea', { class: 'input', rows: 5, 'aria-label': '功能测试步骤', value: Array.isArray(original) ? original.join('\n') : original,
    onInput: () => { changed = true; touch(); } });
  return { node: h('label', { class: 'field' }, h('span', { class: 'label' }, '功能测试步骤（每行一步）'), input), get changed() { return changed; }, apply: () => {
    if (!changed) return;
    const steps = input.value.split('\n').filter((value) => value.trim());
    if (!steps.length) throw new Error('功能测试步骤不能为空');
    doc.steps = steps;
  } };
}
