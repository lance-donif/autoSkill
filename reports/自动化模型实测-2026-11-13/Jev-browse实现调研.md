# Jev-browse 的执行方式与 autoSkill 接入建议

调研日期：2026-11-13。范围是公开技能文件和实际源码阅读；没有调用 Jev API，没有按这些新架构重跑 Laya / OpenDecider。前一轮 152 条正式实验记录及其失败、阻塞保持原样。

## 先区分三个来源

| 来源 | 已核对版本 | 定位 |
|---|---|---|
| [0x7067/jev-browse](https://github.com/0x7067/jev-browse/tree/d812e7aed89172de0d7d34226a469305b929852d) | d812e7a | 名字正好为 jev-browse 的社区项目；给一个有明确停止条件的目标，内部循环驱动浏览器 |
| [wy-coliney/jev-browser-use](https://github.com/wy-coliney/jev-browser-use/tree/54deed1598a34ab6455c4a240bc82cd6025dd269) | 54deed1 | 宿主 Codex 负责规划、文字输入与验证，Jev 负责部分机械浏览器动作 |
| [TypeSafe 官方集成技能](https://github.com/typesafe-ai/skills/blob/302eea662c54bca6730873fe245bec1d69ebc23d/skills/typesafe-ai/SKILL.md) | 302eea6 | 通用 System One 集成指导，不是单独的官方浏览器产品 |

另外核对了 [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast/tree/cf3c406f0fc4becff8986ed4d4d37b7b9f102e45) 的 Python 循环及问题模板，其动作/目标并行、字段文本辅助和历史设计与第一种有相近之处。

## jev-browse 实际怎样处理

### 1. 给出有限目标和可观察的结束条件

[技能入口](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/skills/jev-browse/SKILL.md)接受 `goal`、起始 `url`、执行引擎和步数预算。它适合“填写这些内容，提交后看到结果即停止”这类目标；不是一个没有边界的通用网站探索器。

大模型并非在这个实现里每一步都负责规划。宿主提供目标，Jev 参与内部的有限目标执行循环；另一个 `jev-browser-use` 才明确把规划、文字和最终验证都留在宿主。

### 2. 每一步重新观察真实元素和当前值

它生成编号元素表，保留真实 DOM 节点绑定、标签、角色、当前 value、checked、selected 等信息。模型选编号，执行器把编号还原为观察到的元素，而不是让模型生成 CSS 或坐标。

决策状态还包括：

- 当前 URL、标题、可见文字、视口、表格及省略标记。
- 最近 10 次动作，包含动作类型、实际输入文字、页面是否变化及 URL。
- 最多 8 份观测历史，保留能够说明此前进度的状态。
- 先前的目标复核结果和修复提示。

源码：[决策请求](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/model/decide.ts#L279)、[观测保留](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/agent/progress.ts)。可见区域以外、隐藏或尚未打开的内容明确不在观测中，不能把没看到当成不存在。

### 3. 一次请求并行回答多个具体问题

[chooseOnce](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/model/decide.ts#L173)在同一请求里构造：

- `goal_progress`：目标已满足、不完整，还是证据不确定？
- `operation`：点击、输入、选择、滚动、等待、完成或阻塞？
- 各种 `*_target`：如果执行该种操作，应该选当前哪个元素？
- `follow_up`：执行后是否有可预测的直接后续动作？

各问题不能读取彼此答案。因此目标问题按“如果操作是 CLICK，应选哪个目标”的条件独立提问，代码再读取 operation 对应的目标答案。它不是先请求动作、拿到答案后再请求元素的串行两次网络往返。

这仍然会让 Jev 判断下一种操作，但把“操作种类、目标、完成状态”分开，提供不同候选空间；不是把所有包含参数的完整动作放进一个选择题。

### 4. 文字生成另外处理

选中 `TYPE_TEXT` 后，[fieldText](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/model/text.ts)才调用 OpenAI 兼容文本模型，输入包含目标、选中字段、其他字段、当前页面和历史。没有输入动作时不用为字段文本调用它。

它会校验正文、JSON 和 text 字段，空正文产生明确的文本响应错误，不直接对 null 调用字符串方法；缺少已知字段值时允许返回 null，记录字段暂不可填并修复，而不是编造值。输入值缓存只在上下文未变化时复用。

这正好对应本轮空正文阻塞暴露的适配器缺口，但不能据此认定对同一服务请求它不会得到空响应：差别在于能明确处理并保留错误。

### 5. 用状态机推进，执行前检查页面是否过期

主循环是 `observe → decide → act → settle`。执行前检查观察版本；页面已经变化则丢弃旧决定、重新观察。一次决定在执行前就被消费，避免直接重用旧决定重复操作。动作记录先于下一次页面观察写入历史，后续观察失败不应擦掉已执行动作。

源码：[执行阶段](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/agent/steps.ts)。当前实现也包含一次 DOM 备用执行等恢复机制；autoSkill 的操作意图与收据语义需要继续保留，不能把不确定副作用简单视为可重试。

### 6. 防循环不只检查一次页面是否变化

[fusedNow](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/agent/fuses.ts)检查连续 3 次非等待动作无变化、10 秒无进展、反复返回相同页面指纹，以及交替页面循环。触发后先给一次换方法的修复提示；再次触发才结束为 no_progress。

失败多次的点击目标和暂时无法填写的字段可从当前候选中暂时排除。模型声称 BLOCKED 时还会按页面请求状态做短时观察，避免把仍在加载当成无路可走。

这与本轮“完整观测字符串重复就切换 DeepSeek”的实验策略不同：循环诊断、保留进度、修复提示、加载观察都是独立机制。

### 7. DONE 后重新核对结果

[checkCompletion](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/agent/completion.ts)在 DONE 后重新观察：

- 提供 `--expect` 时，按显式 URL、文字、字段状态和 frame 模式逐项匹配，要求全部满足。
- 没有显式条件时，另发请求判断目标是否全部满足，并判断依据来自当前状态、历史观测、明确要求的动作，还是没有证据。
- 完成声明未成立会给修复提示；同一页面反复提出无法证实的完成声明会结束为阻塞。
- 需要生成文字答案时，还会从观察中生成、审阅答案。

独立的第二次模型判断仍可能出错。autoSkill 已有需求来源和断言，应优先使用自己的真实业务校验；模型停止不自动等于用例通过。

### 8. 能预测的后续操作，减少再问一次

[resolveFollowUp](https://github.com/0x7067/jev-browse/blob/d812e7aed89172de0d7d34226a469305b929852d/src/agent/followup.ts)处理输入后出现的匹配候选、按 Enter 等后续动作。先根据新页面重新解析目标，条件成立才直接执行，省去一次决策请求。预测“操作后完成”仍须经过完成复核。

## 另一款技能的分工更接近 autoSkill

[jev-browser-use 的技能](https://github.com/wy-coliney/jev-browser-use/blob/54deed1598a34ab6455c4a240bc82cd6025dd269/skills/jev-browser-use/SKILL.md)和 [bridge](https://github.com/wy-coliney/jev-browser-use/blob/54deed1598a34ab6455c4a240bc82cd6025dd269/skills/jev-browser-use/bridge.mjs)采取另一种方式：

- Codex 负责规划、所有文字输入、视觉解释和最终验证。
- Jev 从当前可用的非文本动作中选点击、开关、滚动等；不会自己生成选择器、坐标或输入内容。
- 同一个 session 跨多次宿主接手保留历史、调用与执行指标。
- 返回 `needs_verification`，由宿主重新验证，不返回一个能直接当成业务通过的结果。
- 加载等待由确定性代码处理，不为每次等待花费模型调用。

它依赖特定 Codex Computer Use 运行时。autoSkill 可借鉴分工和历史机制，现有 agent-browser / Playwright 能力仍由项目自己的接口提供。

## 对前一轮结果的解释

本轮实测仍然成立：在已测的单个完整动作选择题、当前状态输入和简单回退策略下，没有观察到可推荐默认启用的净提升。

但它没有复现上述多问题、观测历史、修复和后续动作机制，也没有比较主模型先拆成原子任务、小模型只匹配元素的方案。因此不能从 0/32、3/32 或 5/32 推出这些模型采用 Jev-browse 风格接法后仍然没有提升。原报告已经列出该未测范围，此处给出更具体的源码依据。

[官方技能](https://github.com/typesafe-ai/skills/blob/302eea662c54bca6730873fe245bec1d69ebc23d/skills/typesafe-ai/SKILL.md#judgments-from-jev-decisions-in-code)也建议先拆成具体判断，让代码组合决策，而不是用一个泛化问题承担整个工作流。社区实现和官方原则并非完全相同，不能把某个 demo 的方法称作唯一官方方案。

## autoSkill 下一轮合理的对照

固定用户指定的 DeepSeek 主模型，保留现有确定性回放；只在探索、录制和未知元素解析环节比较：

1. 主模型完成整个原子操作。
2. 主模型定义当前原子目标，本地模型只在真实 DOM 候选中选择目标。
3. Jev-browse 式多问题选择：进度、操作、条件目标、后续动作，加真实历史与修复逻辑。

保持站点、需求、断言和页面观测相同，新增配置单独冻结协议；逐项测量首次成功、回退、误操作、完整耗时与内存。不能把新结果覆盖旧结果，也不能用调过的同一小组任务证明泛化能力。

一次 HTTP 请求不等于本地 CPU 只编码一次。当前 OpenDecider / Laya 的多问题 SDK 会形成多行输入或批处理，新状态与历史也可能超过其上下文预算。必须同时记录问题数量、token、截断、批处理耗时和端到端效果，不能直接套用云 Jev 的加速数字。

本调研没有证明上述新接法已提升性能；它指出了值得重测的结构差异。

原始交付：[实测报告](模型实测分析报告.md)、[Excel](自动化模型测试用例.xlsx)、[简短汇总](测试汇总.md)、[问题清单](问题清单.md)。
