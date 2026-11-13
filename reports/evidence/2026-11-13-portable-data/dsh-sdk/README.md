# 最终冻结源码：真实 DSH SDK 验收

2026-11-13 06:18:41–06:18:58 UTC，最终数据根、路径与运行恢复接线冻结后运行。9 项全部通过：0 失败、0 跳过、0 取消。TAP 总用时 16.03 秒，两个 SDK 测试文件的 Biome 检查通过。没有修改仓库源码。

执行命令：

```bash
AUTOSKILL_DSH_SDK_ROOT=/workspace/autoskill-linux-tools node --test --test-reporter=tap tests/dsh-sdk-tools.test.mjs tests/dsh-session-context.test.mjs
npx --offline --yes @biomejs/biome@2.6.7 lint tests/dsh-sdk-tools.test.mjs tests/dsh-session-context.test.mjs
```

环境为 Node v24.22.1，实际安装的 DSH 包版本全部固定为 0.2.2-rc.1。测试执行真实 Cordis、ToolsService、ReactLoopAgent、native executor、通过 run_code 嵌套调用的 Node PTC 子进程、CommandsService、Connection owner-peer Fetch registry 以及 Present，配合真实独立 Python HTTP、SQLite 和三文件报告。

覆盖：native/PTC 同请求幂等与业务 Fail 保留；run.start 冻结输入后仅执行一次；slash 不创建模型 turn、不假称 present；真实 owner-peer 路由与精确报告字节；参数无效与 dispatch 前 abort 零操作；接受后 abort 的诚实回执与读取恢复；真实可访问的 Excel/简报/Bug Markdown 在 owning Agent 开放 turn 内 present；只读绑定与 A/B 隔离；真实 pre-step waterfall 的上下文保留、拒绝、已接纳消息去重及零业务状态写入。

`metadata.json`、`source-sha256-before.json`、`source-sha256-after.json` 记录 126 个 plugin、Python、UI 与两项 SDK 测试源码文件，运行前后哈希完全相同。测试临时目录与带测试 workspace 标识的进程，在运行前后均为零。`ui_evidence_carry_forward.changed_owned_sources` 为空，因此此前数据 UI 6/6 与既有 UI 17/17 的拥有文件未变化；这些 UI 测试没有在本 SDK 命令中重新计数。

完整 stdout/stderr 位于 `sdk-full.log`；Node 的实验性 TypeScript stripping 提示原样保留。静态检查日志位于 `biome.log`。可用本目录 `run-validation.py` 重现命令与哈希检查。

本证据不是远端模型或 DSH Web 设备验收。Connection 覆盖实际 SDK owner-peer registry，不扩大到浏览器认证及完整 HTTP carrier。此前远端模型 CONNECT 403 仍属于独立的 Blocked 边界。此前未冻结阶段的记录继续保留在相邻目录，没有替换或隐藏。
