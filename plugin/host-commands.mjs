// Human commands operate directly and do not open a model turn or declare file delivery.
import { identifier } from './host-operations.mjs';

function result(value) {
  return { kind: value.request_state === 'failed' || value.request_state === 'unknown' ? 'error' : 'success', text: JSON.stringify(value) };
}

function handler(action) {
  return async (invocation) => {
    try { return result(await action(invocation)); }
    catch (error) { return { kind: 'error', text: String(error.message ?? error) }; }
  };
}

export function registerCommands(ctx, service) {
  const definitions = [{
    name: 'autoskill-status', description: '查看当前会话明确绑定的测试任务，不领取工作', input: { hint: '[binding_id]' },
    handler: handler(({ agent, rawInput, signal }) => service.context(agent, rawInput.trim() ? { binding_id: rawInput.trim() } : {}, signal)),
  }, {
    name: 'autoskill-use', description: '明确选择测试任务；none 清空选择', input: { hint: '<binding_id|none>' },
    handler: handler(async ({ agent, rawInput, signal, commandId }) => {
      const selected = identifier(rawInput.trim(), 'binding_id 或 none');
      const context = await service.context(agent, {}, signal);
      return service.task(agent, { operation: 'binding.select', request_id: 'command:' + commandId,
        payload: { binding_id: selected === 'none' ? null : selected, expected_revision: context.selection.revision } }, signal);
    }),
  }, {
    name: 'autoskill-report', description: '核验指定任务报告并列出下载入口；不会调用 present', input: { hint: '<report_id>' },
    handler: handler(({ agent, rawInput, signal }) => service.read(agent, { resource: 'report', query: { report_id: identifier(rawInput.trim(), 'report_id') } }, signal)),
  }];
  for (const definition of definitions) ctx.effect(() => ctx.commands.register(definition), 'autoskill: /' + definition.name);
}
