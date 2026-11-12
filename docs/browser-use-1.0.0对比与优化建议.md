# browser-use 1.0.0 对比与优化建议

核对日期：2026-11-12。阅读 [1.0.0 发布说明](https://github.com/browser-use/browser-use/releases/tag/1.0.0) 及对应源码提交 `070be98109c03f04a95f704533e8f6e9868d0525`。本页是源码分析，没有在 autoSkill 上复现其性能基准。

## 可以借鉴什么

| 优化 | 对方的实际实现 | autoSkill 的现状及取舍 |
| --- | --- | --- |
| 分阶段测量 | 发布时将执行、上下文及模型供应商故障分别分析，而非只看总成功率。 | 我们已有每步耗时，仍可细分进程启动、连接、动作、快照，统计输出长度及命令次数；先取得基线，才知道持久连接和快照精简各能节省多少。 |
| 持久连接与工作进程 | 模型通过一个持久 Python REPL 操作 CDP，worker 保留会话；超时重启 Python 时仍保留浏览器与文件。 | 我们已有 agent-browser daemon，但包装层每步仍启动 CLI、短时连接 Playwright/CDP、读取页签和快照。下一步应评估让录制工作进程统一持有连接、事件与权限，保持通用 CLI 入口，DSH 仍只做适配。 |
| AX 优先、按需补 DOM | ultrafast 模式通过可访问性树的 backendNodeId 寻找元素，缺信息时再查询 JS 或截图；避免每步序列化整个 DOM。 | 适合减少录制时给 AI 的页面信息。AX id 只适合当前浏览器文档，不能直接成为跨机器回放定位器；录制仍需补充 testid/role/label/CSS 等稳定候选，并在导航、框架或标签页变化后失效缓存。 |
| 动作前检查 | AX helper 检查节点仍连接、未禁用/隐藏/inert、在视口内且点击点未被遮挡；遮挡时说明具体覆盖者。 | 我们本轮真实发现弹窗尚未关闭时 Playwright fill 返回成功但目标输入没有改变，已补输入前 actionability 等待及双导出实现。后续应继续对齐 native、Selenium 与导出的语义，避免只在一种引擎有效。 |
| 有界工具输出和历史 | REPL 限制单次文本输出，agent 保留最近两份截图图像并支持上下文 compaction。 | 本轮已验证 5000 条用例默认分页、知识摘要和生成 Skill 大小。仍可优化每步 snapshot 的重复文本：默认返回变化和当前必要上下文，保留主动获取完整快照的命令。宿主会话压缩由各宿主负责，通用 Skill 不能假定能改 DSH 内部历史。 |
| 分层等待预算 | 模型首响应和持续流分别管理，处理长推理与流结束；不是给所有步骤一个短固定超时。 | 本轮 SSE/WS 已有总等待、首事件、空闲和取消；UI 有单步长预算与稳定断言。它的模型请求时间值不应硬编码到我们的业务断言；对被测接口的完成条件仍由用例明确指定。 |
| 生命周期与可诊断错误 | 单独处理 worker、弹窗、keep_alive、网络空闲以及交付失败，保留恢复记录。 | 我们刚修复短时 CDP 连接退出会撤销权限，以及 state load/open 启动参数不同导致浏览器重启丢状态。统一连接所有权和启动参数比增加无差别重试更有价值。 |

## 保留我们的产品目标

browser-use 的目标是让模型即时完成浏览器任务；autoSkill 还需要把操作沉淀成可审查、可分享、无需模型的 Playwright/Selenium/HTTP 用例。它删掉动作 schema 和 DOM serializer 的路线不能完整照搬：我们仍需用例规范、稳定定位器、断言、变量关联、前置链和独立导出。

1.0 默认的云浏览器与模型网关也不适合直接变成我们的默认依赖。autoSkill 的离线包、本机和内网使用、独立 Skill 入口应继续保留，DSH 只是其中一种宿主。

对方删除约 80% 库代码，不等于我们的离线 ZIP 能减少 80%。我们包内仍有 Chrome、Python、驱动与二进制依赖；本次分析不建议因此更换浏览器或把 browser-use 整库引入。

其瞬态模型请求重试也不能变成业务请求重试。创建、修改、删除、发送语音等操作一旦状态不确定，应明确记录结果并核实，不能为追求成功率再次发送。

## 建议实施顺序与验收

1. 先完成当前输入前检查、持久权限、状态恢复和 CRUD 回归；这些已有真实失败依据。
2. 先记录现有各阶段耗时，再单独实验录制工作进程复用 CDP。使用相同本机夹具和动作序列，对比每步耗时中位数/P95、总时间、子进程/连接数量；同时核对录制 IR、页签/框架、HAR、弹窗和导出结果完全一致。
3. 实验 AX 与 DOM 混合快照及差量返回。测量发给 AI 的字符/token 数、获取时间和定位成功率；保留全量快照回退，不能以少返回信息换来误点或漏断言。
4. 再评估宿主可选的截图历史和会话摘要能力。先验证 DSH 插件接口是否提供相应扩展点，不把通用 Skill 绑定到特定宿主。

这些后续实验尚未实现，也没有可承诺的加速或成本下降比例。发布说明同时提醒：供应商故障会夸大原始质量差距，排除供应商错误后质量差异没有统计显著性；不能把宣传基准直接写成我们的收益。

## 源码依据

- [REPL 会话、输出预算及重启](https://github.com/browser-use/browser-use/blob/070be98109c03f04a95f704533e8f6e9868d0525/browser_use/repl/session.py)
- [持久 Python worker](https://github.com/browser-use/browser-use/blob/070be98109c03f04a95f704533e8f6e9868d0525/browser_use/repl/worker.py)
- [AX helper 的动作前检查](https://github.com/browser-use/browser-use/blob/070be98109c03f04a95f704533e8f6e9868d0525/browser_use/ax.py#L199-L228)
- [点击位置及遮挡诊断](https://github.com/browser-use/browser-use/blob/070be98109c03f04a95f704533e8f6e9868d0525/browser_use/ax.py#L704-L757)
- [agent 循环、流式预算与上下文处理](https://github.com/browser-use/browser-use/blob/070be98109c03f04a95f704533e8f6e9868d0525/browser_use/agent/loop.py)
- [1.0 迁移与行为变化](https://github.com/browser-use/browser-use/blob/070be98109c03f04a95f704533e8f6e9868d0525/MIGRATING.md)

AX 的这些检查适用于其 helper 路径；raw CDP/JS 仍可绕过，源码也不能证明它解决了所有跨进程 iframe 或检查后 DOM 再变化的问题。
