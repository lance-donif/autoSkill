// Typed controls edit nested request values without replacing a case with a raw JSON document.
import { h, replace, icon } from './core.js';

let sequence = 0;
const LABELS = { string: '文本', number: '数值', boolean: '布尔值', null: '空值', object: '对象', array: '数组' };
const kindOf = (value) => value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value === 'object' ? 'object' : typeof value;
const initial = (kind) => ({ string: '', number: 0, boolean: false, null: null, object: {}, array: [] })[kind];

export function valueEditor(value, { label, onChange = () => undefined, types = Object.keys(LABELS) } = {}) {
  const source = structuredClone(value);
  let current = source;
  let kind = kindOf(current);
  let changed = false;
  let readValue;
  const content = h('div', { class: 'value-content' });
  const touch = () => { changed = true; onChange(); };
  const type = h('select', { class: 'select value-type', 'aria-label': label + ' 类型', onChange: () => {
    kind = type.value; current = initial(kind); paint(); touch();
  } }, types.map((item) => h('option', { value: item, selected: item === kind }, LABELS[item])));
  function paint() {
    const control = valueControl(current, kind, label, touch);
    readValue = control.read;
    replace(content, control.node);
  }
  paint();
  return { node: h('div', { class: 'value-editor' }, h('div', { class: 'row wrap' }, h('span', { class: 'label grow' }, label), type), content),
    get changed() { return changed; }, read: () => changed ? readValue() : structuredClone(source) };
}

function valueControl(value, kind, label, touch) {
  if (kind === 'object') return objectControl(value, label, touch);
  if (kind === 'array') return arrayControl(value, label, touch);
  if (kind === 'null') return { node: h('span', { class: 'sub' }, 'null'), read: () => null };
  const id = 'value-' + ++sequence;
  const control = kind === 'boolean'
    ? h('input', { id, class: 'check', type: 'checkbox', checked: value, 'aria-label': label, onChange: touch })
    : h(kind === 'number' ? 'input' : 'textarea', { id, class: 'input', type: kind === 'number' ? 'number' : undefined,
      step: kind === 'number' ? 'any' : undefined, rows: kind === 'string' ? 2 : undefined, value, 'aria-label': label, onInput: touch });
  return { node: control, read: () => {
    if (kind === 'boolean') return control.checked;
    if (kind === 'string') return control.value;
    if (control.value.trim() === '' || !Number.isFinite(control.valueAsNumber)) throw new Error(label + '需要有限的数值');
    return control.valueAsNumber;
  } };
}

function objectControl(value, label, touch) {
  const rows = [];
  const list = h('div', { class: 'value-children' });
  const paint = () => replace(list, rows.map((row) => row.node));
  const add = (key, child) => {
    const name = h('input', { class: 'input mono', value: key, 'aria-label': `${label} 字段名 ${rows.length + 1}`, placeholder: '字段名', onInput: touch });
    const editor = valueEditor(child, { label: label + '.' + (key || '新字段'), onChange: touch });
    const row = { name, editor };
    row.node = h('div', { class: 'value-child' }, h('div', { class: 'row' }, name,
      h('button', { class: 'btn sm icon danger', type: 'button', 'aria-label': '删除字段：' + label + '.' + key, onClick: () => {
        rows.splice(rows.indexOf(row), 1); paint(); touch();
      } }, icon('trash'))), editor.node);
    rows.push(row);
  };
  for (const [key, child] of Object.entries(value)) add(key, child);
  paint();
  return { node: h('div', { class: 'stack', style: 'gap:8px' }, list,
    h('button', { class: 'btn sm', type: 'button', onClick: () => { add('', ''); paint(); touch(); } }, icon('plus'), '添加字段：' + label)),
  read: () => {
    const names = rows.map((row) => row.name.value);
    if (names.some((name) => !name)) throw new Error(label + '包含空字段名');
    if (new Set(names).size !== names.length) throw new Error(label + '包含重复字段名');
    return Object.fromEntries(rows.map((row) => [row.name.value, row.editor.read()]));
  } };
}

function arrayControl(value, label, touch) {
  const rows = [];
  const list = h('div', { class: 'value-children' });
  const paint = () => replace(list, rows.map((row) => row.node));
  const add = (child) => {
    const editor = valueEditor(child, { label: `${label}[${rows.length}]`, onChange: touch });
    const row = { editor };
    row.node = h('div', { class: 'value-child' }, editor.node,
      h('button', { class: 'btn sm danger', type: 'button', onClick: () => { rows.splice(rows.indexOf(row), 1); paint(); touch(); } }, '删除数组项'));
    rows.push(row);
  };
  for (const child of value) add(child);
  paint();
  return { node: h('div', { class: 'stack', style: 'gap:8px' }, list,
    h('button', { class: 'btn sm', type: 'button', onClick: () => { add(''); paint(); touch(); } }, icon('plus'), '添加数组项：' + label)),
  read: () => rows.map((row) => row.editor.read()) };
}

export function boundValue(target, key, label, { types, optional = false, defaultValue = '', onChange = () => undefined } = {}) {
  const present = Object.hasOwn(target, key);
  const editor = valueEditor(present ? target[key] : defaultValue, { label, types, onChange });
  const enabled = h('input', { class: 'check', type: 'checkbox', checked: present, 'aria-label': '启用' + label, onChange: () => {
    editor.node.hidden = !enabled.checked; onChange();
  } });
  if (optional) editor.node.hidden = !present;
  return { node: h('div', { class: 'field' }, optional && h('label', { class: 'row' }, enabled, '启用' + label), editor.node),
    get changed() { return editor.changed || (optional && enabled.checked !== present); },
    apply: () => {
      if (optional && !enabled.checked) { Reflect.deleteProperty(target, key); return; }
      if (present || editor.changed || (optional && enabled.checked)) target[key] = editor.read();
    } };
}
