// One parameterless human entry loads the registered skill into the owning Agent's next step.
import { randomUUID } from 'node:crypto';

export function registerCommands(ctx) {
  const definition = {
    name: 'autoskill-use', description: '加载 autoSkill 技能，随后直接对话描述任务',
    handler: async ({ agent, rawInput, signal }) => {
      try {
        if (rawInput.trim()) return { kind: 'error', text: 'autoSkill use 无需参数；直接调用后，在对话中描述任务即可。' };
        if (typeof agent?.inject !== 'function') throw new Error('当前 DSH 宿主不支持技能上下文注入，请检查宿主版本。');
        const cwd = await ctx.workingDirectory.ensure(agent, signal);
        const skill = await ctx.skills.get('autoskill', { scope: agent, cwd, signal });
        signal?.throwIfAborted();
        if (!skill?.content || skill.invocation?.userInvocable === false) throw new Error('autoSkill 技能尚未加载，请检查插件是否已启用。');
        // Agent.inject is the supported non-waking context path; the following user turn consumes the complete skill body.
        agent.inject({ id: randomUUID(), role: 'user', content: [{ type: 'text', text: skill.content }],
          source: { kind: 'skill-invocation', name: 'autoskill', form: 'instructions' } });
        return { kind: 'success', text: '已加载 autoSkill 技能。请直接描述要完成的任务。' };
      } catch (error) { return { kind: 'error', text: String(error.message ?? error) }; }
    },
  };
  ctx.effect(() => ctx.commands.register(definition), 'autoskill: /' + definition.name);
}
