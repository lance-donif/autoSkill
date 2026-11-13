# DSH SDK 验证记录（2026-11-13）

固定 DSH 0.2.2-rc.1，Node 24.22.1。真实 SDK **9/9 通过，0 跳过**，两测试文件 Biome 通过。测试使用隔离的真实 Python HTTP、SQLite、任务执行与报告；未调用远端模型。

本轮直接运行真实 Cordis、ToolsService、ReactLoopAgent、native executor、Node PTC 子进程与 run_code 嵌套、CommandsService、Connection owner-peer Fetch 注册表及 present。

覆盖同请求跨 native/PTC 只产生一次内核操作；稳定请求键冲突；中文身份与多会话隔离；异步 run.start 只产生一个冻结运行并保留业务 Fail；非法参数和派发前 abort 零操作；接受后 abort 不取消业务并读回原收据；slash 不创建模型 turn 或伪称 present；明确绑定的纯只读 context；真实 pre-step waterfall 保留其他插件、外层 reject 重试、已接纳摘要去重；数据库/过期租约/报告零意外变动。

文件交付验证真实临时 Excel、简报 Markdown、Bug 清单 Markdown 三份文件。缺 Agent、关闭 turn、缺文件、目录、三份中缺第三份、post-execute 拒绝均不产生 deliverables/presented。旧 ready 收据缺 bugs 字段返回 unavailable，不能伪造第三份文件或部分交付。

`metadata.json` 保存所有收集源文件执行前后 SHA256 和版本。第二轮只有未被这两套 SDK 测试执行的 `plugin/client.js` 在运行中变化，实际 SDK 后端、Python 内核和测试源均稳定；`scope-assessment.json` 明确保留该差异。原包装脚本因此 exit 1，SDK 与 Biome 各自 exit 0。此记录不称整个产品源码完全冻结。第一轮记录保存在 `attempt-1/`，其间 host-routes 修改，故没有用作固定 SDK 源证明。

本验证不等于 DSH Web/浏览器设备验收，也不覆盖完整 HTTP 浏览器认证；Connection 通过真实 SDK owner peer 路由。远端模型 CONNECT 403 仍是另一路验收阻塞。之后的数据目录迁移和接线修改需要相应复验。
