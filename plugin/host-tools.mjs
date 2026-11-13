// Native and PTC executors consume these same public ToolDefinitions.
import { object, problem, READ_RESOURCES, TASK_OPERATIONS } from './host-operations.mjs';
import { hostSessionId } from './host-service.mjs';

const text = (value) => [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) }];
const canonical = (value) => JSON.parse(JSON.stringify(value));
const output = {
  schema: { type: 'object', properties: { version: { type: 'integer' }, request_state: { type: 'string' },
    operation: { type: 'string' }, identity: { type: 'object' } }, required: ['version', 'request_state', 'operation', 'identity'], additionalProperties: true },
  render: (_args, value) => text(value),
  presentationMeta: (_args, value) => ({ autoskill: { version: 1, ...value.identity, operation: value.operation,
    request_id: value.request_id, request_state: value.request_state } }),
};

function validate(args, field, values, keys) {
  object(args);
  if (!values.includes(args[field])) throw problem('validation_error', '请使用工具声明的' + field);
  if (Object.keys(args).some((key) => !keys.includes(key))) throw problem('validation_error', '工具包含未声明的参数');
  if (args.binding_id !== undefined && typeof args.binding_id !== 'string') throw problem('validation_error', 'binding_id 需要是文字');
}

function presentation(args, result) {
  const meta = result?.meta?.autoskill;
  const operation = meta?.operation ?? args?.operation ?? args?.resource;
  if (typeof operation !== 'string') return undefined;
  const title = result ? (result.isError ? 'autoSkill 调用未完成：' : 'autoSkill 回执：') + operation : 'autoSkill：' + operation;
  return { card: 'generic', title, ...(result ? {} : { kind: 'other', rawInput: { operation, binding_id: args.binding_id } }) };
}

function cancellationContent(exec, result, observed) {
  const code = result.error?.info?.code;
  if (code !== 'ABORTED' && code !== 'ABORTED_BEFORE_DISPATCH') return undefined;
  const requestId = exec.arguments?.request_id;
  return [...result.content, ...text({
    notice: code === 'ABORTED_BEFORE_DISPATCH' ? '本次工具未派发；业务状态未被改变为取消。'
      : '已停止本次宿主等待；这不代表业务已取消。已接受的运行仍由内核管理，不要换键重复提交。',
    request_id: requestId ?? null, observed_receipt: observed ?? null,
    recovery: requestId ? { tool: 'autoskill_read', resource: 'operation', query: { request_id: requestId } } : null,
  })];
}

export function toolDefinitions(service) {
  const observed = new WeakMap();
  return [{
    name: 'autoskill_read',
    description: '读取 autoSkill 当前任务、工作流、判定、运行或报告。context 是纯只读恢复摘要；正文按 cursor/head 分页取，报告读取会核验文件但不会调用 present。',
    parameters: { type: 'object', properties: { resource: { type: 'string', enum: READ_RESOURCES }, binding_id: { type: 'string' },
      query: { type: 'object', additionalProperties: true } }, required: ['resource'], additionalProperties: false },
    output,
    async execute(args, exec) {
      validate(args, 'resource', READ_RESOURCES, ['resource', 'binding_id', 'query']);
      hostSessionId(exec.agent);
      return canonical(await service.read(exec.agent, args, exec.signal));
    },
    isConcurrencySafe: (args) => args?.resource === 'context',
    presentCall: (args) => presentation(args),
    presentResult: (args, result) => presentation(args, result),
  }, {
    name: 'autoskill_task',
    description: '通过现有内核操作 autoSkill：binding.put/select 明确当前任务；workflow 与 judge 领取/提交沿原幂等键和租约；run.start 异步入队，run.cancel 显式取消；report.retry 只重试报告。request_id 每次新操作唯一，重试沿用原值。payload 保持对应 HTTP/CLI 合同，其他能力继续使用 autoSkill CLI。',
    parameters: { type: 'object', properties: { operation: { type: 'string', enum: TASK_OPERATIONS }, binding_id: { type: 'string' },
      request_id: { type: 'string' }, payload: { type: 'object', additionalProperties: true } }, required: ['operation', 'request_id'], additionalProperties: false },
    output,
    async execute(args, exec) {
      validate(args, 'operation', TASK_OPERATIONS, ['operation', 'binding_id', 'request_id', 'payload']);
      hostSessionId(exec.agent);
      const result = canonical(await service.task(exec.agent, args, exec.signal));
      observed.set(exec, result);
      return result;
    },
    finalizeContent(exec, result) {
      const value = observed.get(exec);
      observed.delete(exec);
      return cancellationContent(exec, result, value);
    },
    presentCall: (args) => presentation(args),
    presentResult: (args, result) => presentation(args, result),
  }];
}

export function registerTools(ctx, service) {
  for (const definition of toolDefinitions(service)) ctx.effect(() => ctx.tools.register(definition), 'autoskill: ' + definition.name);
}
