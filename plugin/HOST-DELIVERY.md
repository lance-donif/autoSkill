# 宿主派发边界

autoSkill 本机调度器是唯一时钟。到期维护任务和对话判定写入持久 outbox；DSH 插件只处理这些单次投递，不再注册一份 DSH 周期计划。

适配器依据本机安装的 DSH `0.2.2-rc.1` SDK 源码接入：`dsh-api-session-controller` 的 `resolveAgent`、`create`、`prompt({requestId, sessionId, mode:'queue', content}, signal)`，以及 `sessions.flush`。该版本的 `prompt` 用 rpcId 去重，既有会话可按 sessionId 激活。宿主已退出时插件也不运行，不能据此声称可以启动 DSH 进程。

每次投递先写本地准备日志，再调用 prompt，确认会话持久化后保存收据并确认 outbox。响应丢失时，通过已保存收据或相同 rpcId 的宿主消息恢复；缺少证据的 unknown 状态不会自动重发。调用 prompt 前的准备失败只释放自己的租约。已完成的对话判定使用 `method: already_reviewed` 收据满足投递，不再唤醒额外会话。

`--session` 隔离浏览器任务，`--workflow-binding` 关联持久工作项，`--host-session` 标识接受后续判定的宿主会话。三者不能互相替代。运行绑定固定插件工作区和后台端口，模型及权限配置由宿主管理。

`tests/host-adapter.test.mjs` 使用符合上述 SDK 形状的替身，验证持久化、响应丢失和恢复分支；这些测试不等同于真实 DSH 模型会话验收。源码部署或版本升级后仍需在目标 DSH 中验证实际插件加载与消息接收。
