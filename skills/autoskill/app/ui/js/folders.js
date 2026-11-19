// Project-local folder trees organize cases without rewriting their business module or identity.
import { h, icon, api, toast, refresh } from './core.js';
import { drawer, closeOverlay } from './ui.js';

export const folderSelection = new Map();
export const ALL_FOLDERS = '__all__';
const foldersByTree = new WeakMap();
const expandedByProject = new Map();

export function folderPath(tree, folderId) {
  if (!foldersByTree.has(tree)) foldersByTree.set(tree, new Map(tree.folders.map((item) => [item.id, item])));
  const byId = foldersByTree.get(tree);
  const names = [];
  let current = byId.get(folderId);
  const visited = new Set();
  while (current && !visited.has(current.id)) {
    visited.add(current.id);
    names.unshift(current.name);
    current = byId.get(current.parent_id);
  }
  return names.join(' / ');
}

export function folderSelect(tree, selectedId, label, emptyLabel = '不放入文件夹') {
  return h('select', { class: 'select', 'aria-label': label }, h('option', { value: '', selected: !selectedId }, emptyLabel),
    tree.folders.map((item) => h('option', { value: item.id, selected: item.id === selectedId }, folderPath(tree, item.id))));
}

export function folderTree(project, tree, selectedId, choose) {
  if (!expandedByProject.has(project.id)) expandedByProject.set(project.id, new Set([ALL_FOLDERS]));
  const expanded = expandedByProject.get(project.id);
  const children = new Map();
  for (const folder of tree.folders) {
    const parent = folder.parent_id ?? ALL_FOLDERS;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(folder);
  }
  const counts = new Map();
  for (const id of Object.values(tree.assignments)) counts.set(id, (counts.get(id) || 0) + 1);

  function branch(id, title, depth, total) {
    const nested = children.get(id);
    const key = `folder-${project.id}-${id}`;
    const item = h('button', { class: 'folder-item' + (selectedId === id ? ' selected' : ''), id: key,
      dataset: { folderId: id }, 'aria-pressed': String(selectedId === id), 'aria-label': `${title}，${total} 条用例`,
      onClick: () => {
        if (nested) expanded.add(id);
        choose(id);
      } }, icon('folder'), h('span', { class: 'grow ellipsis' }, title), h('span', { class: 'sub num' }, total));
    const row = h('div', { class: 'folder-row', style: `padding-inline-start:${Math.min(depth, 6) * 10}px` }, item);
    if (!nested) return row;
    const group = h('div', { class: 'folder-children', id: key + '-children', hidden: !expanded.has(id) },
      nested.map((folder) => branch(folder.id, folder.name, depth + 1, counts.get(folder.id) || 0)));
    const verb = () => expanded.has(id) ? '收起' : '展开';
    const toggle = h('button', { class: 'folder-toggle', id: key + '-toggle', 'aria-expanded': String(expanded.has(id)),
      'aria-controls': group.id, 'aria-label': verb() + title, onClick: () => {
        if (expanded.has(id)) expanded.delete(id);
        else expanded.add(id);
        group.hidden = !expanded.has(id);
        toggle.setAttribute('aria-expanded', String(expanded.has(id)));
        toggle.setAttribute('aria-label', verb() + title);
      } }, icon('right'));
    row.append(toggle);
    return [row, group];
  }

  return h('nav', { class: 'folder-tree', 'aria-label': `${project.name}文件夹` },
    branch(ALL_FOLDERS, '全部用例', 0, Object.keys(tree.assignments).length));
}

export function folderEditor(project, tree, parentId, folder = null) {
  const name = h('input', { class: 'input', id: 'folder-name', value: folder?.name || '', required: true, autofocus: true, placeholder: '例如：订单回归' });
  const parent = folderSelect(tree, folder ? folder.parent_id : parentId, '上级文件夹', '无上级文件夹');
  if (folder) for (const option of parent.options) if (option.value === folder.id) option.disabled = true;
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const save = h('button', { class: 'btn primary', form: 'folder-form', type: 'submit' }, folder ? '保存' : '创建文件夹');
  const form = h('form', { id: 'folder-form', class: 'stack', onSubmit: async (event) => {
    event.preventDefault();
    error.hidden = true;
    save.disabled = true;
    try {
      await api(`/projects/${encodeURIComponent(project.id)}/folders` + (folder ? '/' + encodeURIComponent(folder.id) : ''), {
        method: folder ? 'PATCH' : 'POST', body: { name: name.value, parent_id: parent.value || null, expected_revision: tree.revision },
      });
      closeOverlay();
      toast(folder ? '文件夹已保存' : '文件夹已创建');
      refresh();
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { save.disabled = false; }
  } },
  h('div', { class: 'field' }, h('label', { for: 'folder-name' }, '文件夹名称'), name),
  h('div', { class: 'field' }, h('span', { class: 'label' }, '上级文件夹'), parent), error);
  drawer({ title: folder ? '编辑文件夹' : '新建文件夹', subtitle: project.name, body: form,
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save] });
}

export async function moveCases(items) {
  const groups = new Map();
  for (const item of items) groups.set(item.project_id, [...(groups.get(item.project_id) || []), item.case_id]);
  let trees;
  try { trees = await Promise.all([...groups.keys()].map(async (id) => ({ tree: await api(`/projects/${encodeURIComponent(id)}/folders`), id }))); }
  catch (error) { toast(error.message, { bad: true }); return; }
  const destinations = new Map(trees.map(({ tree, id }) => [id, folderSelect(tree, null, `「${id}」目标文件夹`)]));
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const save = h('button', { class: 'btn primary', onClick: async () => {
    error.hidden = true;
    save.disabled = true;
    try {
      for (const { tree, id } of trees) {
        await api(`/projects/${encodeURIComponent(id)}/folders/move-cases`, { method: 'POST', body: {
          case_ids: groups.get(id), folder_id: destinations.get(id).value || null, expected_revision: tree.revision,
        } });
      }
      closeOverlay();
      toast(`已移动 ${items.length} 条用例`);
      refresh();
    } catch (problem) { error.textContent = problem.message + '。刷新后可查看当前文件夹位置。'; error.hidden = false; }
    finally { save.disabled = false; }
  } }, '移动用例');
  drawer({ title: '移动用例', subtitle: `将 ${items.length} 条用例移动到各自项目内的文件夹。`, body: [
    trees.map(({ id }) => h('div', { class: 'field' }, h('span', { class: 'label' }, `${id} · ${groups.get(id).length} 条用例`), destinations.get(id))),
    h('p', { class: 'hint' }, '用例编号、业务子项目、断言与已有执行记录保持不变。'), error,
  ], foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save] });
}
