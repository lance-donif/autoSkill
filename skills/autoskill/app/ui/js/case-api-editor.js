// The API form covers requests, explicit checks and cross-case variable bindings.
import { h } from './core.js';
import { boundValue } from './value-editor.js';
import { textField, selectField, section, applyControls } from './editor-fields.js';

export function apiCaseEditor(draft, touch) {
  const controls = [
    selectField(draft, 'method', '请求方法', ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'], touch),
    textField(draft, 'path', '请求路径', touch, { required: true }),
    boundValue(draft, 'headers', '请求头', { optional: true, types: ['object'], defaultValue: {}, onChange: touch }),
    boundValue(draft, 'params', '查询参数', { optional: true, types: ['object'], defaultValue: {}, onChange: touch }),
  ];
  const body = bodyEditor(draft, touch);
  draft.assertions ||= {};
  const assertions = draft.assertions;
  const checks = [
    boundValue(assertions, 'status', '响应状态码', { types: ['number'], defaultValue: 200, onChange: touch }),
    boundValue(assertions, 'header_contains', '响应头包含', { optional: true, types: ['object'], defaultValue: {}, onChange: touch }),
    boundValue(assertions, 'body_contains', '响应正文包含', { optional: true, types: ['array'], defaultValue: [], onChange: touch }),
    boundValue(assertions, 'json_equals', '响应字段相等（JSON Pointer）', { optional: true, types: ['object'], defaultValue: {}, onChange: touch }),
    boundValue(assertions, 'schema', '响应结构', { optional: true, types: ['object', 'boolean'], defaultValue: {}, onChange: touch }),
    boundValue(assertions, 'max_ms', '最大响应时间（毫秒）', { optional: true, types: ['number'], defaultValue: 1000, onChange: touch }),
  ];
  const extension = [
    boundValue(draft.extensions, 'extract', '提取变量（名称 → JSON Pointer）', { optional: true, types: ['object'], defaultValue: {}, onChange: touch }),
    boundValue(draft.extensions, 'depends_on', '前置用例 ID', { optional: true, types: ['array'], defaultValue: [], onChange: touch }),
    selectField(draft, 'api_source', '接口来源类型', ['openapi', 'browser', 'inferred', 'source-and-official-docs'], touch),
    textField(draft, 'source_ref', '接口来源引用', touch, { required: true }),
  ];
  return { node: h('div', { class: 'stack' }, section('接口请求', controls), body.node,
    section('响应断言', checks), section('提取与依赖', extension, '变量使用 {{名称}} 引用；凭据使用 $' + '{ENV:NAME}。保存不会发送请求。')),
  apply: () => { applyControls(controls); body.apply(); applyControls(checks); applyControls(extension); } };
}

function bodyEditor(draft, touch) {
  const original = Object.hasOwn(draft, 'json') ? 'json' : Object.hasOwn(draft, 'form') ? 'form' : 'none';
  let changed = false;
  const kind = h('select', { class: 'select', 'aria-label': '请求体类型' },
    [['none', '无请求体'], ['json', 'JSON'], ['form', '表单']].map(([value, label]) => h('option', { value, selected: value === original }, label)));
  const json = boundValue(draft, 'json', 'JSON 请求体', { defaultValue: {}, onChange: touch });
  const form = boundValue(draft, 'form', '表单请求体', { types: ['object'], defaultValue: {}, onChange: touch });
  const paint = () => { json.node.hidden = kind.value !== 'json'; form.node.hidden = kind.value !== 'form'; };
  kind.addEventListener('change', () => { changed = true; paint(); touch(); });
  paint();
  return { node: section('请求体', [kind, json.node, form.node], '复杂请求值按对象、数组和具体类型逐项编辑。'), apply: () => {
    if (kind.value === 'json') json.apply();
    if (kind.value === 'form') form.apply();
    if (!changed) return;
    if (kind.value !== 'json') Reflect.deleteProperty(draft, 'json');
    if (kind.value !== 'form') Reflect.deleteProperty(draft, 'form');
    if (kind.value !== 'none' && !Object.hasOwn(draft, kind.value)) draft[kind.value] = {};
  } };
}
