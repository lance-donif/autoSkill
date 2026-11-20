// Element candidates and ordered frame/open-shadow scopes share the same form controls.
import { h, icon, replace } from './core.js';

const KINDS = [['testid', '测试标识'], ['role', '角色'], ['label', '标签'], ['placeholder', '占位提示'], ['text', '文本'], ['css', 'CSS'], ['xpath', 'XPath']];

function locatorControl(value, label, touch) {
  const original = structuredClone(value);
  const kind = h('select', { class: 'select', 'aria-label': label + ' 定位方式' },
    KINDS.map(([key, title]) => h('option', { value: key, selected: key === value.by }, title)));
  const input = h('input', { class: 'input mono', value: value.value || '', 'aria-label': label + ' 定位值', onInput: touch });
  const role = h('input', { class: 'input', value: value.role || '', 'aria-label': label + ' 角色', onInput: touch });
  const name = h('input', { class: 'input', value: value.name || '', 'aria-label': label + ' 名称', onInput: touch });
  const nth = h('input', { class: 'input', type: 'number', min: 1, step: 1, value: (value.nth || 0) + 1, 'aria-label': label + ' 第几个', onInput: touch });
  const anchor = h('input', { class: 'input mono', value: value.xpath_anchor || '', 'aria-label': label + ' XPath 元素锚点', onInput: touch,
    placeholder: 'Shadow DOM 内唯一的 CSS 元素锚点' });
  const detail = h('div', { class: 'stack', style: 'gap:8px' });
  function paint() {
    replace(detail, kind.value === 'role' ? [labelled('角色', role), labelled('名称（可选）', name)] : labelled('定位值', input),
      kind.value === 'xpath' && [labelled('XPath 元素锚点（CSS，Shadow DOM 内必填）', anchor),
        h('p', { class: 'hint' }, '主文档 XPath 无需锚点。Shadow DOM 内填写当前根中唯一的 CSS 元素锚点，并使用以 . 开头的自身、子元素或后代路径，例如 .//button。不能使用 ../、父级、祖先或相邻轴；仅支持 open Shadow DOM。')],
      labelled('第几个匹配（从 1 开始）', nth));
  }
  kind.addEventListener('change', () => { paint(); touch(); });
  paint();
  return { node: h('div', { class: 'stack', style: 'gap:8px' }, kind, detail), read: () => {
    const result = { ...original, by: kind.value };
    if (kind.value === 'role') { result.role = role.value; if (name.value) result.name = name.value; else Reflect.deleteProperty(result, 'name'); Reflect.deleteProperty(result, 'value'); }
    else { result.value = input.value; Reflect.deleteProperty(result, 'role'); Reflect.deleteProperty(result, 'name'); }
    if (!Number.isInteger(nth.valueAsNumber) || nth.valueAsNumber < 1) throw new Error(label + '的匹配序号需要从 1 开始');
    if (nth.valueAsNumber > 1 || Object.hasOwn(original, 'nth')) result.nth = nth.valueAsNumber - 1;
    if (original.unique && (result.by !== original.by || result.role !== original.role || result.name !== original.name || result.nth !== original.nth)) {
      Reflect.deleteProperty(result, 'unique');
      Reflect.deleteProperty(result, 'anchor');
    }
    if (kind.value === 'xpath' && anchor.value) {
      if (!input.value.startsWith('.')) throw new Error(label + '带元素锚点的 XPath 需要以 . 开头');
      result.xpath_anchor = anchor.value;
    }
    else Reflect.deleteProperty(result, 'xpath_anchor');
    return result;
  } };
}

function labelled(label, control) {
  return h('label', { class: 'field' }, h('span', { class: 'label' }, label), control);
}

export function locatorList(values, label, touch = () => undefined) {
  const rows = [];
  const list = h('div', { class: 'stack', style: 'gap:8px' });
  let changed = false;
  const mark = () => { changed = true; touch(); };
  const paint = () => replace(list, rows.map((row, index) => h('fieldset', { class: 'editor-group' },
    h('legend', {}, `候选 ${index + 1}`), row.node,
    h('button', { class: 'btn sm danger', type: 'button', onClick: () => { rows.splice(index, 1); paint(); mark(); } }, '删除定位候选'))));
  for (const [index, value] of values.entries()) rows.push(locatorControl(value, label + ` 候选${index + 1}`, mark));
  paint();
  return { node: h('div', { class: 'stack', style: 'gap:8px' }, list,
    h('button', { class: 'btn sm', type: 'button', onClick: () => { rows.push(locatorControl({ by: 'css', value: '' }, label + ` 候选${rows.length + 1}`, mark)); paint(); mark(); } },
      icon('plus'), '添加定位候选：' + label)), get changed() { return changed; }, read: () => rows.map((row) => row.read()) };
}

export function contextEditor(values, label, touch = () => undefined) {
  const rows = [];
  const list = h('div', { class: 'stack', style: 'gap:8px' });
  let changed = false;
  const mark = () => { changed = true; touch(); };
  const paint = () => replace(list, rows.map((row, index) => h('fieldset', { class: 'editor-group' },
    h('legend', {}, `上下文 ${index + 1}`), row.kind, row.locators.node,
    h('button', { class: 'btn sm danger', type: 'button', onClick: () => { rows.splice(index, 1); paint(); mark(); } }, '移除上下文'))));
  function add(value) {
    const kind = h('select', { class: 'select', 'aria-label': label + ` 上下文${rows.length + 1} 类型`, onChange: mark },
      h('option', { value: 'frame', selected: value.kind === 'frame' }, 'iframe'), h('option', { value: 'shadow', selected: value.kind === 'shadow' }, 'Shadow DOM（open）'));
    rows.push({ original: structuredClone(value), kind, locators: locatorList(value.locators || [], label + ` 上下文${rows.length + 1}`, mark) });
  }
  for (const value of values || []) add(value);
  paint();
  return { node: h('div', { class: 'stack', style: 'gap:8px' }, h('p', { class: 'hint' }, '从主文档开始，按顺序进入 iframe 或开放式 Shadow DOM。空路径表示主文档。'), list,
    h('button', { class: 'btn sm', type: 'button', onClick: () => { add({ kind: 'frame', locators: [{ by: 'css', value: '' }] }); paint(); mark(); } },
      icon('plus'), '添加上下文：' + label)), get changed() { return changed; },
  read: () => rows.map((row) => ({ ...row.original, kind: row.kind.value, locators: row.locators.read() })) };
}

export function stepLocators(step, label, touch, { inherited = false } = {}) {
  const values = structuredClone(step.locators || []);
  if (step.selector) values.push(step.selector.startsWith('xpath=') ? { by: 'xpath', value: step.selector.slice(6) } : { by: 'css', value: step.selector });
  if (step.placeholder) values.push({ by: 'placeholder', value: step.placeholder });
  if (step.role) values.push({ by: 'role', role: step.role, ...(step.name ? { name: step.name } : {}) });
  const locators = locatorList(values, label + ' 元素', touch);
  const context = contextEditor(step.context || [], label, touch);
  const parentLocators = inheritance('沿用父断言元素定位', inherited && !values.length, locators.node, touch, inherited);
  const parentContext = inheritance('沿用父断言上下文', inherited && !Object.hasOwn(step, 'context'), context.node, touch, inherited);
  return { node: h('details', { class: 'more', open: true }, h('summary', {}, '目标上下文与元素定位'),
    parentContext.node, context.node, parentLocators.node, locators.node), apply: () => {
    if (inherited && parentLocators.checked()) {
      for (const key of ['locators', 'selector', 'placeholder', 'role']) Reflect.deleteProperty(step, key);
    } else if (locators.changed) {
      step.locators = locators.read();
      for (const key of ['selector', 'placeholder', 'role']) Reflect.deleteProperty(step, key);
    }
    applyContext(step, context, inherited, parentContext);
  } };
}

function applyContext(step, context, inherited, parentContext) {
  if (inherited) {
    if (parentContext.checked()) Reflect.deleteProperty(step, 'context');
    else step.context = context.read();
    return;
  }
  if (!context.changed) return;
  const value = context.read();
  if (value.length) step.context = value;
  else Reflect.deleteProperty(step, 'context');
}

function inheritance(label, value, body, touch, enabled) {
  const input = h('input', { class: 'check', type: 'checkbox', checked: value, onChange: () => { body.hidden = input.checked; touch(); } });
  if (enabled) body.hidden = value;
  return { node: enabled ? h('label', { class: 'row' }, input, label) : null, checked: () => input.checked };
}
