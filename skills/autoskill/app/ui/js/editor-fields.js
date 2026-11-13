// Bind only changed form values so untouched compatibility data stays byte-for-byte equivalent.
import { h } from './core.js';

export function field(label, control) {
  return h('label', { class: 'field' }, h('span', { class: 'label' }, label), control);
}

export function textField(target, key, label, touch, { multiline = false, fallback = '', type = 'text', required = false } = {}) {
  let changed = false;
  const input = h(multiline ? 'textarea' : 'input', { class: 'input', type: multiline ? undefined : type,
    rows: multiline ? 3 : undefined, value: target[key] ?? fallback, 'aria-label': label, required,
    onInput: () => { changed = true; touch(); } });
  return { node: field(label, input), input, get changed() { return changed; }, apply: () => {
    if (!changed) return;
    if (required && !input.value.trim()) throw new Error(label + '不能为空');
    target[key] = input.value;
  } };
}

export function selectField(target, key, label, values, touch, fallback) {
  let changed = false;
  const input = h('select', { class: 'select', 'aria-label': label, onChange: () => { changed = true; touch(); } },
    values.map((item) => {
      const [value, title] = Array.isArray(item) ? item : [item, item];
      return h('option', { value, selected: value === (target[key] ?? fallback) }, title);
    }));
  return { node: field(label, input), input, get changed() { return changed; }, apply: () => { if (changed) target[key] = input.value; } };
}

export function section(title, controls, note) {
  return h('section', { class: 'editor-section stack' }, h('h3', {}, title), note && h('p', { class: 'hint' }, note), controls.map((control) => control.node || control));
}

export function applyControls(controls) { for (const control of controls) control.apply(); }
