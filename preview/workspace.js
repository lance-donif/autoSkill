export const workspaceIdentity = ({ db, fixtures }) => db.settings?.data_id ?? fixtures['/settings']?.data_id;

export function workspaceConflict(context) {
  const expected = context.params?.get('expected_workspace');
  if (expected == null || expected === workspaceIdentity(context)) return null;
  return Response.json({ code: 'data_changed', detail: '数据目录已变化，请刷新页面后重新选择要删除的内容' }, { status: 409 });
}
