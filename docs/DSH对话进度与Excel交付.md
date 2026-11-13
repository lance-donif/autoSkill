# DSH 输入框上方原生计划：只读源码核对

核对日期：2026-11-13。核对版本：本机 npm `@deepseek-ai/dsh@0.2.2-rc.1` 及同版本 `dsh-tool-todo`、`dsh-client-ui-conversation`、`dsh-plan-mode`、`dsh-tool-present`。官方 Git tag `dsh-v0.2.2-rc.1`，commit `66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db`。这是明确固定版本，不声称它是最新版本（远端另有 rc.2 tag，本次未审 rc.2）。

本地 npm：`/workspace/autoskill-linux-tools/node_modules/@deepseek-ai/`。外部只读源码快照：`/workspace/autoSkill-release/dsh-plan-research/deepseek-harness/`。原研究阶段只读，没有启动真实 DSH 模型/UI 任务。以下协议核对和后续产品接入说明不是本轮原生 UI 验收；仓库既有历史 DSH Web 验收不等同新增计划/报告链路的实机验收。

## 结论与最小接入

用户描述的“对话框上方预计划步骤”对应 `TodoDock` / `TodoPanel`：宿主 AI 调用原生 **`todo_write`** 更新当前会话的任务列表，原生 Web UI 在输入框上方显示“任务”及已完成/进行中/待处理数量，默认折叠，点击展开逐项步骤。不是 Markdown 自动识别，也不是 autoSkill 页面自己画的列表。

autoSkill Skill 及插件送入宿主的任务提示要求宿主在实际工具可用时，开始任务/恢复当前 turn 后先写完整清单；取得阶段真实回执后再更新；最后包含“生成、检查并交付测试用例 Excel 和 Markdown 汇总”。在 DSH 使用 `todo_write`，PTC 由宿主按其可用工具调用协议调用，不在插件后台硬编码 `ctx.tools.execute` 发原生名字。现有 autoSkill CLI 负责真正的工作与导出，DSH 负责原生计划展示。

Excel 和 Markdown 生成并检查成功后，宿主可调用官方 **`present`** 声明两个文件，让 DSH 用原生交付卡显示；最终答复同时给可访问文件链接。不要在文件尚未生成时把交付阶段标为完成。GoFile 直连此前被代理 403 阻断，之后已从用户 Git 提交的 `test-cases.rar` 读取完整 Skill，来源 SHA-256 为 `08f913f3c8d4de28fac9c19acd9481355c78f6ccbb2e427788dc3ec7df72307f`。规则随产品放在 `skills/autoskill/references/test-cases/`，由通用内核输出 14 列 Excel；Markdown 只保留简单字段、数量汇总、主要问题和 Excel 链接。读取规则与本机导出成功仍不代表真实 DSH 卡片已验收。

## todo_write 的精确协议

调用示例（示意待执行步骤，不代表工作已完成）：

```json
{"todos":[
  {"content":"核对需求与已有用例","status":"in_progress"},
  {"content":"补充场景并执行测试，保存真实结果","status":"pending"},
  {"content":"生成、检查并交付测试用例 Excel 和 Markdown 汇总","status":"pending"}
]}
```

- 唯一参数为必填 `todos` 数组；每项只有 `content` 与 `status`，未知字段拒绝。状态只能是 `pending`、`in_progress`、`completed`，没有 `failed`、`blocked`、item ID、priority 或 activeForm。
- 每次**整表替换**，不能只传变更项。content 会 trim，空内容、重复内容拒绝。`todos: []` 会使列表不可见。
- 配置 `allowParallelInProgress` 必填；false 最多一项 in_progress，true 允许多项。官方 standard、ptc、cordis 预设使用 true。这个开关控制展示纪律，不调度后台任务。
- 正常模型工具调用先由执行器记 tool/call、tool/result，工具再向 **owning Agent 的 Session** 追加 `todo/write` 事件，payload `{todos:[...]}`。无 `exec.agent` 调用明确失败。子 agent 有自己的列表，不能把所有子任务各自写入误当作父会话的同一清单；父宿主负责聚合更新。
- 返回规范值含 `todos` 与 `counts:{pending,inProgress,completed}`；对模型文字为 `Updated todo list: … pending, … in progress, … completed.`。

源码：[tool-todo/src/index.ts:25-55,80-99,118-211](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/todo/tool-todo/src/index.ts#L118)，[类型与 todo/write 声明:23-46](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/todo/tool-todo/src/types.ts#L23)，[standard preset:114-117](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/bundle/web-app/presets/standard.patch.yml#L114)。

## 生命周期、展示与传输

1. `todos` projection 初值 null；收到 `todo/write` 取最新完整列表，last-write-wins。
2. `turn/end` 不清空；完成后的清单仍显示。**下一次 `turn/start` 会清空 projection 为 null**，即使老事件仍在历史中。跨轮/恢复工作流需要根据 autoSkill 持久状态重新调用 todo_write；不能假定历史清单始终挂在上方。
3. TodoPanel 初始 collapsed=true，空列表不渲染；展开显示逐项 content/status。组件只渲染和折叠，用户点复选状态不是业务执行入口，没有前端直接改状态功能。
4. TodoDock 读取 `useProjection('todos')`，注册到 session scope 的 `conversation.input.dock`，id=todo，order=0；ConversationContent 把此 dock 放在 inputBar 前面，正是输入框上方。
5. rc.1 的实际线协议：SessionFollowFrame 的 snapshot 带 `projections:{asOfSeq,values}`；SessionController control stream 先发 `{type:'baseline',value:{projections:{[sessionId]:…}}}`，再发 `{type:'projection',sessionId,seq,changes:[{key:'todos',val:[…]}]}`。客户端按 session 和 seq 接收投影，插件无需自己拼推送帧。某些 README 使用 `session/projection` 概括叫法，集成应以本版本 types/control 源码为准。
6. todo/write 是 log-only UI 状态，不再生成一条 derived model message。持久性来自宿主 session persistence；若自行通过 SDK 写入并需要耐久确认，`sessions.flush(session)` 才是官方检查点，内存 append 不等于落盘承诺。

源码：[投影 reducer:120-134](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/todo/tool-todo/src/index.ts#L120)，[TodoPanel.tsx:65-118](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/client/ui-conversation/src/client/skeleton/TodoPanel.tsx#L65)，[ConversationContent:164-170](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/client/ui-conversation/src/client/skeleton/ConversationContent.tsx#L164)，[wire types:554-593](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/api/session-controller/src/types.ts#L554)，[control 推送:20-49](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/api/session-controller/src/control.ts#L20)，[flush:1329-1358](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/core/session/src/index.ts#L1329)。

官方已有源码测试直接覆盖 next turn 清空和 end 保留：[projection.spec.ts:87-99](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/todo/tool-todo/tests/projection.spec.ts#L87)；UI 初始折叠、计数、展开：[todo-panel.client.spec.tsx:36-103](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/client/ui-conversation/tests/todo-panel.client.spec.tsx#L36)。本次阅读了测试，没有重新运行官方 suite。

## 插件与 Skill 能做什么

Skill 是注册给宿主 AI 的 Markdown 指令内容；注册 Skill 不会自动创建任务清单。现有 autoSkill `plugin/index.mjs:33-55` 构造 Skill content，`plugin/host-adapter.mjs:62-72` 构造投递提示，适合作为最小接入点，明确让宿主调用其原生工具并在恢复时重建清单即可。

DSH 官方 ToolsService 有 `execute({calls,signal,agent?,toolSet?,log?})`，可以由插件在**明确持有目标 Agent、工具已挂载、执行器协议匹配**的条件下调用。没有 owning Agent 的 standalone service 调 todo_write 会失败；没有专用 `ctx.todos.set` 或 SessionController `update_plan` Remote 方法。`ctx.tools.execute` 按该 Agent 选定的 native/PTC executor 分发；PTC 对裸 native 工具名的根调用会 UNKNOWN_TOOL，所以不应把调用方式写死成所有 DSH 配置都可用的 `execute(todo_write)`。

直接 `agent.session.append('todo/write', ...)` 在源码底层确实存在，但那是绕开 todo 工具校验/调用审计的事件写入，不是一个通用无上下文 setter；这里不建议 autoSkill 采用。更不要修改 JSONL、伪造 tool/call 或尝试浏览器 DOM 注入。一个会话只有一份整表清单，并行独立写入会互相覆盖；主宿主应作为唯一聚合写入方。

源码：[ToolBatchInput:18-47](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/core/tools/src/executor.ts#L18)，[ToolsService.execute:369-376](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/core/tools/src/index.ts#L369)，[PTC 根调用约束:190-199](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/core/tools/src/contracts.ts#L190)，[Skill 内容定义:86-100](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/skill/skill/src/index.ts#L86)。

跨宿主最小策略：先看本会话实际暴露的工具及其 schema；DSH 用 todo_write，若其他宿主提供 update_plan/TodoWrite 等则按其真实 schema 映射。没有原生计划工具时用简短文字清单说明进展，明确这是文字提示，不声称已显示原生计划 UI；不影响继续执行/导出。Skill 不安装新工具，也不臆造命令。工作项/运行/结果仍以 autoSkill 持久回执为准。

## 不要误用 /plan

`/plan`、`ctx.planMode.set/get` 和 `exit_plan_mode({plan:markdown})` 是另一套机制：进入规划模式、注入 plan:policy，然后由 exit_plan_mode 打开用户 review，选择 Approve 或 Keep planning。其 Web 组件注册在 `conversation.input.plan`，显示输入框内部的蓝色 Plan chip，并有右侧 Markdown 方案预览。它不等于上方任务清单；为实现自动显示进度不应自动开启它，更不应给本来已授权的自动任务新增审批。

源码：[exit_plan_mode schema:280-285 与审核:301-362](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/plan/plan-mode/src/index.ts#L280)，[plan chip:116-128](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/client/ui-plan/src/client/index.ts#L116)。

## Excel 原生文件交付

官方 `present` 参数：

```json
{"files":[{"path":"/宿主可访问目录/测试用例-本次任务.xlsx","description":"本次测试用例与真实执行结果"},{"path":"/宿主可访问目录/测试汇总-本次任务.md","description":"本次测试汇总"}]}
```

应先真正生成 `.xlsx` 并读回检查，再调用 present，成功后把最后一步完成并给链接。文件需是当前 Session filesystem 能访问的 regular file；相对路径基于 Session working directory，最终符号链接/缺失文件/目录/被 provider 拒绝路径会失败。工具要求 owning Agent 与 open turn；子 agent 生成的文件由父 agent 自行 present 才属于父交付。successful tools/result 追加 `deliverables/presented`。文件内容不复制、不校验 xlsx 格式，只记录路径，所以应使用本次任务独立文件名，不能删除/覆盖后仍声称历史卡内有原始附件。

工具和 UI 文档明示可能在 turn 结束才显示交付卡。若原生 present 不可用，仍交付实际 Excel 文件的可访问链接，不谎称已有 DSH 原生文件卡。桥接导出的文件路径若在宿主 filesystem 不可见，需要已有可访问 artifact 路径或下载机制；不能仅见 CLI JSON path 就认定 present 成功。

源码：[present schema/检查/事件:38-108](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/deliverables/tool-present/src/index.ts#L38)，[官方 README](https://github.com/deepseek-ai/deepseek-harness/blob/66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db/packages/deliverables/tool-present/README.md)。

## 接入后的最小验收边界

- 一次真实 DSH任务：开工出现完整 todos；更新后同一会话数量/状态改变；最终生成的 Excel 可下载/打开，present 卡在结束后出现。
- 下一 turn 恢复：服务状态仍存在，原生条先清空，再由宿主重建；不要把清空当服务工作流丢失。
- 并行任务：不同宿主 Session 不串清单，父清单由父宿主聚合。
- 故意业务 Fail：执行步骤可以完成，Excel 记录 Fail；不能用 todos completed 代替 business Pass。导出失败时交付步骤不得提前完成。
- 无 todo_write / 无 present / PTC preset：按实际可用能力执行、降级文字与文件链接，不进入 plan review。

上述是宿主建议验收项，**本次未执行真实 DSH 界面验收**。用户上传的用例 Skill 已另行读取和接入，模板及本机导出校验应按实际测试记录判断，不能外推为 DSH 原生文件卡通过。
