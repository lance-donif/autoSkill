// Task targets retain native parameters and host delivery separately from replay cases.
import { h, replace, api, href } from './core.js';

const field = (label, control, note) => h('div', { class: 'field' }, h('label', { for: control.id }, label), control, note && h('span', { class: 'hint' }, note));

export function targetEditor(task, project, onKindChange) {
  const kind = h('select', { class: 'select', id: 'tk-kind' },
    [['replay', '用例回放'], ['native', '原生资产'], ['maintenance', 'AI 维护工作流']].map(([value, label]) => h('option', { value, selected: value === (task?.kind || 'replay') }, label)));
  const asset = h('select', { class: 'select', id: 'tk-asset' });
  const workflow = h('select', { class: 'select', id: 'tk-workflow' });
  const parameters = h('textarea', { class: 'input mono', id: 'tk-parameters', rows: 5, value: JSON.stringify(task?.parameters || {}, null, 2) });
  const host = h('select', { class: 'select', id: 'tk-host' }, ['generic', 'dsh'].map((value) => h('option', { value, selected: value === (task?.host || 'generic') }, value === 'dsh' ? 'DeepSeek Harness' : '通用宿主')));
  const session = h('input', { class: 'input', id: 'tk-host-session', value: task?.host_session_id || '', placeholder: '可选，使用目标宿主会话' });
  const overlap = h('select', { class: 'select', id: 'tk-overlap' },
    [['skip', '前次未结束时跳过'], ['queue', '继续入队']].map(([value, label]) => h('option', { value, selected: value === (task?.overlap || 'skip') }, label)));
  const native = field('原生资产', asset, '沿用资产原入口执行，参数可提供 args、env、thresholds 或 load。');
  const maintenance = field('维护工作流', workflow, '需要运行中的宿主继续工作；送达宿主不表示维护完成。');
  const options = h('div', { class: 'stack' }, field('任务参数（JSON）', parameters), field('执行宿主', host), field('宿主会话', session));
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  let request = 0;
  let loading = false;
  let loadError = '';

  function choices(select, items, value, name) {
    replace(select, h('option', { value: '' }, `请选择${name}`), items.map((item) => h('option', { value: item.id, selected: item.id === value }, item.title)));
    if (value && !items.some((item) => item.id === value)) select.append(h('option', { value, selected: true }, `${value}（当前不可用）`));
  }
  async function load() {
    const mine = ++request;
    loading = true;
    loadError = '';
    error.hidden = true;
    try {
      if (kind.value === 'native') {
        const data = await api(`/assets/${encodeURIComponent(project.value)}?all=1`);
        if (mine === request) choices(asset, data.assets.map((item) => ({ id: item.id, title: item.name + ' · ' + item.engine })), task?.asset_id, '资产');
      } else if (kind.value === 'maintenance') {
        const data = await api('/workflows?projects=' + encodeURIComponent(project.value));
        if (mine === request) choices(workflow, data.items.map((item) => ({ id: item.workflow_id, title: item.title || item.workflow_id })), task?.workflow_id, '工作流');
      }
    } catch (problem) {
      if (mine === request) { loadError = problem.message; error.textContent = loadError; error.hidden = false; }
    } finally { if (mine === request) loading = false; }
  }
  function change() {
    native.hidden = kind.value !== 'native';
    maintenance.hidden = kind.value !== 'maintenance';
    options.hidden = kind.value === 'replay';
    onKindChange(kind.value);
    load();
  }
  kind.addEventListener('change', change);
  project.addEventListener('change', load);
  const node = h('div', { class: 'stack' }, field('任务类型', kind), native, maintenance, options,
    h('div', { class: 'row wrap' }, h('a', { class: 'card-link', href: href('native-assets') }, '管理原生资产'), h('a', { class: 'card-link', href: href('workflows') }, '管理需求工作流')),
    field('重叠执行', overlap), error);
  native.hidden = kind.value !== 'native';
  maintenance.hidden = kind.value !== 'maintenance';
  options.hidden = kind.value === 'replay';
  load();
  return { node, kind: () => kind.value, read() {
    if (kind.value === 'replay') return { kind: 'replay', overlap: overlap.value };
    if (loading) throw new Error('正在读取任务目标，请稍后保存');
    if (loadError) throw new Error(loadError);
    const values = JSON.parse(parameters.value);
    if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error('任务参数需要是 JSON 对象');
    if (kind.value === 'native' && !asset.value) throw new Error('请选择原生资产');
    if (kind.value === 'maintenance' && !workflow.value) throw new Error('请选择维护工作流');
    return { kind: kind.value, parameters: values, asset_id: kind.value === 'native' ? asset.value : '',
      workflow_id: kind.value === 'maintenance' ? workflow.value : '', host: host.value, host_session_id: session.value, overlap: overlap.value };
  } };
}
