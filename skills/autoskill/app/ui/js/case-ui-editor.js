// Action identities stay fixed while their parameters and explicit assertions remain editable.
import { h, icon, replace } from './core.js';
import { boundValue } from './value-editor.js';
import { locatorList, contextEditor, stepLocators } from './locator-editor.js';
import { textField, selectField, section, applyControls } from './editor-fields.js';

const ACTIONS = {
  goto: ['打开地址', ['url']], reload: ['刷新页面', []], tab_new: ['新建标签页', ['tab', 'url']],
  tab_switch: ['切换标签页', ['tab']], tab_close: ['关闭标签页', ['tab', 'active']], tab_sync: ['同步标签页', ['tabs']],
  fill: ['输入', ['value']], type: ['逐字输入', ['value']], click: ['点击', []], dblclick: ['双击', []], hover: ['悬停', []],
  press: ['按键', ['key']], check: ['勾选', []], uncheck: ['取消勾选', []], select: ['选择选项', ['value']],
  wait_visible: ['等待可见', []], wait_hidden: ['等待隐藏', []], captcha: ['输入验证码', ['image']],
};
const ASSERTIONS = {
  visible: ['可见', []], hidden: ['隐藏', []], count: ['数量相等', ['expected']], min_count: ['至少数量', ['expected']],
  text: ['文本相等', ['expected']], contains: ['文本包含', ['expected']], value: ['输入值相等', ['expected']],
  checked: ['勾选状态', ['expected']], attribute: ['属性相等', ['attribute', 'expected']],
  url: ['地址相等', ['expected']], url_contains: ['地址包含', ['expected']], title: ['页面标题', ['expected']],
};
const TARGET_ACTIONS = new Set(['fill', 'type', 'click', 'dblclick', 'hover', 'press', 'check', 'uncheck', 'select', 'wait_visible', 'wait_hidden', 'captcha']);
const TARGET_ASSERTIONS = new Set(['visible', 'hidden', 'count', 'min_count', 'text', 'contains', 'value', 'checked', 'attribute']);
const PARAM_LABEL = { url: '地址', value: '输入或选项值', key: '按键', tab: '标签页编号', active: '关闭后活动标签页', expected: '预期值', attribute: '属性名' };

export function uiCaseEditor(draft, editing, touch) {
  draft.extensions.automation ||= { start_url: '', actions: [], assertions: [] };
  const automation = draft.extensions.automation;
  const start = textField(automation, 'start_url', '起始地址', touch, { required: true });
  const dependencies = boundValue(draft.extensions, 'depends_on', '前置用例 ID', { optional: true, types: ['array'], defaultValue: [], onChange: touch });
  const actionRows = (automation.actions || []).map((step, index) => {
    const token = editing?.actions?.find((item) => item.index === index)?.id;
    return { step, token, control: stepEditor(step, `步骤 ${index + 1}`, touch) };
  });
  const finalRows = (automation.assertions || []).map((step, index) => ({ step, control: stepEditor(step, `结束断言 ${index + 1}`, touch) }));
  const actions = h('div', { class: 'stack' });
  const finals = h('div', { class: 'stack' });
  const remove = (rows, row, paint) => { rows.splice(rows.indexOf(row), 1); paint(); touch(); };
  const paintActions = () => replace(actions, actionRows.map((row, index) => rowCard(row, `步骤 ${index + 1}`,
    row.step.assertion ? () => remove(actionRows, row, paintActions) : null)));
  const paintFinals = () => replace(finals, finalRows.map((row, index) => rowCard(row, `结束断言 ${index + 1}`, () => remove(finalRows, row, paintFinals))));
  paintActions(); paintFinals();
  const add = h('button', { class: 'btn', type: 'button', onClick: () => {
    const step = { assertion: 'visible', locators: [{ by: 'css', value: '' }] };
    finalRows.push({ step, control: stepEditor(step, `结束断言 ${finalRows.length + 1}`, touch) }); paintFinals(); touch();
  } }, icon('plus'), '添加结束断言');
  return { node: h('div', { class: 'stack' }, section('页面执行', [start, dependencies], '现有动作数量和顺序保持固定。可修改操作类型与参数；断言可以增删。'), actions,
    section('结束断言', [finals, add])), apply: () => {
    start.apply(); dependencies.apply();
    for (const row of [...actionRows, ...finalRows]) row.control.apply();
    for (const row of actionRows) {
      if (row.step.action && !row.token) throw new Error('动作身份信息不完整，请重新读取用例后编辑');
      if (row.step.action) row.step._edit_id = row.token;
    }
    automation.actions = actionRows.map((row) => row.step);
    if (Object.hasOwn(automation, 'assertions') || finalRows.length) automation.assertions = finalRows.map((row) => row.step);
  } };
}

function rowCard(row, title, remove) {
  return h('fieldset', { class: 'editor-group' }, h('legend', {}, title, row.step.assertion ? ' · 断言' : ' · 动作'), row.control.node,
    remove && h('button', { class: 'btn sm danger', type: 'button', onClick: remove }, '删除断言'));
}

function stepEditor(step, label, touch, { transition = false } = {}) {
  const assertion = Object.hasOwn(step, 'assertion');
  const key = assertion ? 'assertion' : 'action';
  const table = assertion ? ASSERTIONS : ACTIONS;
  const kind = h('select', { class: 'select', 'aria-label': label + (assertion ? ' 断言类型' : ' 操作类型') },
    Object.entries(table).map(([value, [name]]) => h('option', { value, selected: step[key] === value }, name)));
  const content = h('div', { class: 'stack' });
  let controls = [];
  function paint() {
    controls = stepFields(step, label, touch, assertion, transition);
    replace(content, controls.map((control) => control.node));
  }
  kind.addEventListener('change', () => {
    try { applyControls(controls); } catch { /* Incomplete old parameters do not block choosing another operation. */ }
    cleanParameters(step, key, kind.value, table[kind.value][1]);
    step[key] = kind.value;
    seedParameters(step, kind.value, table[kind.value][1]);
    paint(); touch();
  });
  paint();
  return { node: h('div', { class: 'stack' }, kind, content), apply: () => applyControls(controls) };
}

function cleanParameters(step, key, operation, allowed) {
  const params = key === 'action' ? ['url', 'value', 'key', 'tab', 'active', 'image', 'image_context'] : ['expected', 'attribute'];
  for (const field of params) if (!allowed.includes(field) && !(field === 'image_context' && allowed.includes('image'))) Reflect.deleteProperty(step, field);
  const targets = key === 'action' ? TARGET_ACTIONS : TARGET_ASSERTIONS;
  if (!targets.has(operation)) for (const field of ['locators', 'context', 'selector', 'placeholder', 'role']) Reflect.deleteProperty(step, field);
}

function seedParameters(step, operation, fields) {
  for (const key of fields) {
    if (key === 'expected' && !validExpected(operation, step[key])) step[key] = paramDefault(operation, key);
    if (Object.hasOwn(step, key)) continue;
    step[key] = key === 'image' ? [{ by: 'css', value: '' }] : key === 'tabs' ? { active: 't1' } : paramDefault(operation, key);
  }
}

function validExpected(operation, value) {
  if (operation === 'checked') return typeof value === 'boolean';
  if (operation !== 'count' && operation !== 'min_count') return true;
  return (Number.isInteger(value) && value >= 0) || (typeof value === 'string' && /^\$\{ENV:[A-Za-z_][A-Za-z0-9_]*\}$/.test(value));
}

function stepFields(step, label, touch, assertion, transition) {
  const key = assertion ? 'assertion' : 'action';
  const op = step[key];
  const required = (assertion ? ASSERTIONS : ACTIONS)[op]?.[1] || [];
  const controls = [textField(step, 'name', label + ' 说明', touch)];
  for (const param of required) {
    if (param === 'image' || param === 'tabs') continue;
    controls.push(boundValue(step, param, label + ' ' + PARAM_LABEL[param], { types: paramTypes(op, param), defaultValue: paramDefault(op, param), onChange: touch }));
  }
  const targets = assertion ? TARGET_ASSERTIONS : TARGET_ACTIONS;
  if (targets.has(op)) controls.push(stepLocators(step, label, touch, { inherited: transition }));
  if (op === 'captcha') controls.push(captchaFields(step, label, touch));
  if (op === 'tab_sync' || Object.hasOwn(step, 'tabs')) controls.push(boundValue(step, 'tabs', label + ' 标签页变化', { types: ['object'], defaultValue: { active: 't1' }, onChange: touch }));
  if (!transition) controls.push(...timingFields(step, label, touch, assertion));
  controls.push(boundValue(step, 'oracle', label + ' 预期来源', { types: ['object'], defaultValue: {}, optional: true, onChange: touch }));
  if (step.recording_result) controls.push({ node: h('details', { class: 'more' }, h('summary', {}, '录制时的实际结果（只读）'), h('pre', { class: 'code' }, JSON.stringify(step.recording_result, null, 2))), apply: () => undefined });
  return controls;
}

function paramTypes(op, key) {
  if (key !== 'expected') return ['string'];
  if (op === 'checked') return ['boolean'];
  return op === 'count' || op === 'min_count' ? ['number', 'string'] : ['string', 'number', 'boolean', 'null', 'object', 'array'];
}
function paramDefault(op, key) { return key === 'expected' && op === 'checked' ? true : key === 'expected' && ['count', 'min_count'].includes(op) ? 0 : ''; }

function timingFields(step, label, touch, assertion) {
  const controls = [boundValue(step, 'timeout_seconds', label + ' 等待时间（秒）', { types: ['number'], optional: true, defaultValue: 10, onChange: touch })];
  if (!assertion) return controls;
  controls.push(selectField(step, 'mode', label + ' 判断方式', [['eventually', '等待目标成立'], ['always', '持续成立']], touch, 'eventually'),
    boundValue(step, 'stable_for_seconds', label + ' 连续成立时间（秒）', { types: ['number'], optional: true, defaultValue: 0, onChange: touch }), transitionFields(step, label, touch));
  return controls;
}

function transitionFields(step, label, touch) {
  const original = Object.hasOwn(step, 'transition_from');
  const value = structuredClone(step.transition_from || { assertion: 'visible' });
  const child = stepEditor(value, label + ' 来源状态', touch, { transition: true });
  const enabled = h('input', { class: 'check', type: 'checkbox', checked: original, 'aria-label': label + ' 启用来源状态', onChange: () => { child.node.hidden = !enabled.checked; touch(); } });
  child.node.hidden = !original;
  return { node: h('div', { class: 'field' }, h('label', { class: 'row' }, enabled, '先观测来源状态'),
    h('p', { class: 'hint' }, '未设置来源定位或上下文时，沿用本断言的定位和上下文。'), child.node), apply: () => {
    if (enabled.checked) { child.apply(); step.transition_from = value; }
    else Reflect.deleteProperty(step, 'transition_from');
  } };
}

function captchaFields(step, label, touch) {
  const image = locatorList(step.image || [], label + ' 验证码图片', touch);
  const context = contextEditor(step.image_context || [], label + ' 验证码图片', touch);
  const inherited = h('input', { class: 'check', type: 'checkbox', checked: !Object.hasOwn(step, 'image_context'), 'aria-label': label + ' 图片沿用步骤上下文',
    onChange: () => { context.node.hidden = inherited.checked; touch(); } });
  context.node.hidden = inherited.checked;
  return { node: section('验证码图片', [image.node, h('label', { class: 'row' }, inherited, '图片沿用步骤上下文'), context.node]), apply: () => {
    if (image.changed) step.image = image.read();
    if (inherited.checked) Reflect.deleteProperty(step, 'image_context');
    else step.image_context = context.read();
  } };
}
