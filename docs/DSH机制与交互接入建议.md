# DSH 机制与交互接入建议

核对日期：2026-11-13。范围：本机 DSH `0.2.2-rc.1` SDK 和官方 commit `66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db`。以下新增交互是后续提案，未加入 v2.2.3，也没有声称已通过真实 DSH 模型会话验收。

| 机制 | 已核实入口 | autoSkill 现状与可以增加的交互 |
| --- | --- | --- |
| 原生工具 | `ctx.tools.register`；输入/输出 Schema、execute、取消信号 | 当前走 Skill + shell/CLI。可把领取任务、读取状态、提交判断、读取报告接成少量原生工具，仍调用同一内核 |
| 工具卡片 | `presentCall/presentResult`；`tool.call.toolview` React 插槽 | 当前没有专属工具卡。可展示实际任务、阶段、数量和报告。通用卡没有任意 actions 字段；按钮必须由自有 React 组件连接真实业务 API |
| 动态上下文 | `ctx.on('agent/pre-step', …)` waterfall | 可注入当前绑定任务的简短状态、版本和读取引用；调用 next 保留其他插件内容，摘要未变时不重复注入 |
| Skill 按需发现 | `ctx.skills.register`、`registerProvider` 的 list/get 与失效通知 | 已注册产品 Skill、上传的用例规则 Skill 和存在时的用例 Skill。可进一步按工作区提供动态项目入口，不把几千用例全部展开进目录或正文 |
| Slash 命令 | `/<Skill 名>`；另有 `ctx.commands.register` | 前者让模型读 Skill；后者直接处理人类命令。可增加确定性的进度、报告和恢复快捷入口，复用现有持久状态 |
| 原生进度 | `todo_write` 完整替换清单 | 已有宿主调用指导。下一 turn/start 会清空展示，需要根据内核状态重建；completed 仅表示阶段完成，不表示业务 Pass |
| 输入框交互 | `conversation.input.dock/activity`；`SessionInput.notify` | 可展示当前任务、运行数量、待判断数量和报告入口；通知精确落在所属会话。已核实的是输入框通知，不是系统桌面推送 |
| 文件交付卡 | `present({files:[{path,description}]})`；`conversation.chat.turnTail` | 已有 Excel/MD 文件、receipt 与宿主指导。仍要在真实 DSH 验证路径可达性和原生文件卡；present 不复制文件、不校验 Excel 内容 |
| 会话投递 | `sessionController.create/resolveAgent/prompt`、`sessions.flush` | 已接入持久任务投递。可增加会话绑定与恢复展示。prompt 接受表示进入 inbox，不等于测试完成；取消会话与取消具体 run 是不同操作 |
| 调度 | DSH `ctx.schedule`；autoSkill 自有 scheduler/outbox | 当前由 autoSkill 负责调度，DSH adapter 消费任务。不要为同一任务再创建第二个 DSH 时钟；DSH 进程需要运行才能处理宿主 AI 工作 |
| 插件面板 | `main`、`sidebar.panellist` | 已有仪表盘和定时任务 iframe。可让对话中的任务卡直接打开对应 project/run，而不是只到首页 |

## 建议实施顺序

1. **会话绑定与恢复摘要**：明确当前对话属于哪个 workspace、task session、workflow/cycle，跨轮按真实持久状态继续。
2. **原生工具薄适配和任务卡**：选择少量高频入口，保留通用 CLI；异步执行先返回 run_id，避免宿主同步等待本来需要它处理的 judge。
3. **任务 dock 与会话通知**：按业务状态显示进度，按钮复用现有服务，打开准确的运行详情。
4. **真实文件交付验收**：先核实 DSH 会话能访问本轮文件，再实际验证 Excel/MD present 卡；HTML 若需自动交付，应先保存为可访问的任务文件。
5. **确定性 Slash 入口**：查询进度、打开报告、恢复指定任务；调度继续复用现有 scheduler/outbox。

## 保持通用 Skill 架构

Skill 负责测试方法和命令约定；Python 内核负责任务、用例、证据、执行、报告和调度；DSH adapter 负责工具展示、宿主会话绑定、上下文与文件交付。其他宿主继续使用同一 CLI/HTTP，不复制另一套业务内核。

分开保存 DSH host session、autoSkill task session，以及 workflow/item/attempt 身份。同一会话有多个任务时使用明确绑定，不猜“最新任务”。pre-step 仅查询摘要，不调用会领取租约的 workflow next，也不自动重播业务动作。用例和知识继续分页/按引用读取，避免把几千条定义一次塞进模型上下文。

输入框进度不使用 /plan 审批模式替代；新增这些展示和适配不需要再加一层用户审批。宿主模型、沙箱与权限设置仍由 DSH 管理。真实 DSH 默认沙箱、模型执行、原生卡片链路尚未完成验收，现有本机服务和 SDK 替身测试不能代替该结论。

## 核对依据

- [DSH 工具合同](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/core/tools/src/contracts.ts#L15)、[展示协议](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/core/tools/src/presentation.ts#L146)、[工具卡插槽](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/client/ui-tool/src/client/contract/slots.ts#L15)。
- [pre-step 事件](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/core/agent/src/runtime-types.ts#L338)、[官方 Skill 上下文注入](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/skill/tool-skill/src/index.ts#L217)、[Skill Provider](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/skill/skill/src/index.ts#L246)。
- [输入框插槽](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/client/ui-conversation/src/client/contract/slots.ts#L292)、[会话通知](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/client/ui-conversation/src/client/contract/input.ts#L201)、[present](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/deliverables/tool-present/src/index.ts#L38)。
- [会话投递协议](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/api/session-controller/src/types.ts#L265)。Slash 与 schedule 另核对本机同版本包 `@deepseek-ai/dsh-commands/lib/types/index.d.ts:18`、`@deepseek-ai/dsh-schedule/lib/types/index.d.ts:50`。
- autoSkill 当前入口：`plugin/index.mjs`、`plugin/client.js`、`plugin/host-adapter.mjs`、`plugin/host-guidance.mjs`；持久分页/绑定：`context_service.py`、`workflow.py`。原生进度和文件交付协议详见仓库 `docs/DSH对话进度与Excel交付.md`。
