# Jev 与 CPU 界面操作加速方案

调研日期：2026-11-13。已读取官方说明、实际推理代码和项目公开评测；本轮没有下载模型权重或运行本机模型性能测试。下面的方案属于选型建议，不是已接入 autoSkill 的功能。Google、Bing 和 Hugging Face 在当前环境返回代理 403，本轮通过可访问的 GitHub 仓库核对来源；权重本体和目标机器兼容性尚未实测。

## Jev 是什么，哪些部分值得借鉴

确认为 TypeSafe AI 的 Jev。其[官方说明](https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md)定义了对文字、图片和应用状态的 Choice、Noul、Score、Box 问题，返回结构化答案；Box 还可定位图片目标。公开接入以云 API 为主，本次未找到官方可下载的 CPU 权重。

[Browser Use / Jev Ultrafast](https://github.com/browser-use/jev-ultrafast)展示了具体 UI 加速方式：一次提取页面可操作元素，生成带编号的候选表；一次请求同时判断操作和目标；只有需要输入文字时再调用文本模型。默认循环不逐步传截图。它的[作者实验](https://github.com/browser-use/jev-ultrafast/blob/main/docs/performance.md)在同一个 Google Flights 任务的三对运行中，将中位耗时从 9.450 秒降至 7.092 秒、浏览器协议调用从 1,092 次降至 101 次。该实验使用云 Jev，初始浏览器启动、导航和独立最终校验不在计时内，不是 CPU 性能证明，也不是通用成功率。

autoSkill 的确定性回放已经不需要 AI。可加速的是探索页面、录制新用例、寻找替代元素和维护用例；主要收益应来自减少宿主模型往返、重复快照和过多浏览器调用。无需在原本无需模型的回放每一步额外增加推理。

## 四个可选方案

| 方案 | CPU 和体积依据 | 适合承担的工作 | 实际限制 |
| --- | --- | --- | --- |
| **A：Verdict / multilingual-e5-small + ONNX INT8** | 118M 参数，项目提供 CPUExecutionProvider；作者称 INT8 权重约 120 MB，另需运行时 | 中文/多语元素候选检索、标签匹配、已积累样本的页面状态分类；作为轻量筛选层 | 双编码器不擅长联合理解复杂 JSON 字段与候选关系。不能把意图分类准确率等同浏览器任务成功率 |
| **B：Laya 多语或浏览器专项 checkpoint + CPU / ONNX** | 多语约 322M，英文/typed 约 421M；官方存在 CPU 和 ONNX 路径，英文 FP32 模型可到约 1.7 GB | 从 DOM 候选中选控件、判断局部状态；浏览器专项权重更贴近目标 | 通用 typed 权重零样本浏览器表现很差；专项训练后改善，不等于中文网站可直接用。衍生权重的实际下载、许可及 CPU 导出还需核对 |
| **C：Kev-0.8B 的 LoRA / pointer head + CPU 服务** | 源码明确 CPU FP32；仅参数字节粗算约 3.2 GB，实际内存还含依赖、加载峰值和上下文 | 狭窄、已有候选的英文语义选择，接近 Jev 的 Choice/Score/Noul 服务合同 | 主要英文；需要基座及专用 head，不能把普通 Qwen GGUF 当作同一模型。模型卡明确通用工具路由不在可靠范围内 |
| **D：AnyJev + llama.cpp + 小 Qwen GGUF** | 官方后端默认 n_gpu_layers=0；可从示例的 Qwen2.5-0.5B-Instruct 量化版开始 | 想复用通用 GGUF 运行时，比较小模型对短中文候选的效果 | 默认去偏对 K 个候选做 K 次 prefill，CPU 未必快；当前 Choice 实现最多 26 个选项，需要分批或自动交回宿主，不能据此限制用户任务 |

**推荐顺序：先用 A 做便宜的候选筛选，再比较 B 的局部 UI 决策；C、D 作为对照。** 如果要求完全离线自主规划，需要另行选择本地规划模型；上述小决策层本身不能替代完整任务规划和文本生成。

## 影响选择的证据

### Verdict

[源码与模型说明](https://github.com/Manavarya09/verdict/tree/76bc1fe9ba87ec5b746ed33faac48812f1d1a4ca)提供 CPU ONNX 路径、100+ 语言编码器和冻结 embedding 上的轻量分类头。代码和发布模型声明 Apache-2.0，基础 multilingual-e5-small 为 MIT。明确 CPU provider 的位置见 [onnx.py](https://github.com/Manavarya09/verdict/blob/76bc1fe9ba87ec5b746ed33faac48812f1d1a4ca/src/verdict/engines/onnx.py)。

不能直接拿 README 的毫秒数字当 autoSkill 指标：其中包含 Apple M5、batch 均摊、空闲与训练占用机器的不同测量。作者自己的 typed-decisions 表中，原始 JSON 的零样本双编码器仅约 0.323，训练后约 0.69–0.71；项目明确承认复杂字段联合推理弱于 cross-encoder。因此适合先筛选候选、少量标注后训练自己的状态分类，不应独自决定整条测试已通过。

### Laya

[Laya 原仓库](https://github.com/NandhaKishorM/laya/tree/3a38cbe83f6ef556f3125fb0065fdc5a55d05792)声明 Apache-2.0。作者 CPU 短邮件四问题热启动测量，多语中位 192 ms、英文 453 ms，设备为 Intel Mac Pro、28 线程；标题 33 ms 属于 GPU 路径，不能外推普通电脑或 UI 长上下文。

更重要的是[浏览器专项报告](https://github.com/NandhaKishorM/laya/blob/3a38cbe83f6ef556f3125fb0065fdc5a55d05792/docs/finetune_browser_agent.md)：通用 typed 权重零样本元素 top-1 为 0.10、16 个真实浏览器任务成功率 0%；专用微调后 top-1 约 0.63–0.66，322M 版本任务成功率约 62%。这些训练与浏览器时延来自 GPU，不能当 CPU 结果。文档给出 `cklxx/laya-browser` 权重链接，本轮未能直接读取 Hugging Face 的权重许可与文件。

因此“换成 Laya 就等同 Jev”没有依据。中文场景优先评估多语模型，并测试长页面、同名按钮、弹窗和多轮状态，而非只跑英文演示。

### Kev 与 AnyJev

[Kev-0.8B 模型卡](https://github.com/jaredpalmer/kev/blob/2074cc44256200bb37540103fa4572901faa10f3/docs/model-cards/kev-0.8b.md)及[CPU 路径](https://github.com/jaredpalmer/kev/blob/2074cc44256200bb37540103fa4572901faa10f3/kev/device.py)可核对：它是训练过的 pointer head，支持多问题共享状态计算，但质量验证主要为英文、8k 上下文，工具调用路由表现甚至低于随机。没有发现可直接引用为普通 CPU 中文 UI 延迟的证据。代码、基座及 head 声明 Apache-2.0。

[AnyJev 的 llama.cpp 后端](https://github.com/nokia-applied-research/AnyJev/blob/35311c58955716cb8955ce1368a665988bc63735/anyjev/backends/llamacpp.py)确实可 CPU 运行。其[去偏级别](https://github.com/nokia-applied-research/AnyJev/blob/35311c58955716cb8955ce1368a665988bc63735/docs/levels.md)默认需要多个旋转输入；llama.cpp 实现逐条计算并清理 KV，不能把 K 次计算当成一次。框架 Apache-2.0 不代表所有可选基础权重同一许可。它是一种读出框架，不是一份通用的“AnyJev 模型权重”。

[Simple Jev](https://github.com/featherless-ai/simple-jev/tree/26bedad52b713c5f53a376910ee6960e9bbb86c1)也支持 Qwen3.5-0.8B CPU，可以作零训练 HTTP 对照；其 Laya backend 固定旧版本依赖，不能与最新 Laya 的能力或测量混为一谈。

## 接入 autoSkill 的建议

1. 保留现有 agent-browser/CDP 的跨 tab、iframe 和录制能力；借鉴元素编号和批量观察。原 Jev Ultrafast MVP 明确不覆盖新 tab、frame、canvas 等，不能直接替换整套现有浏览器能力。
2. 宿主 AI 形成任务计划；本地小模型接收目标和精简 DOM 候选，处理匹配、选项、明确状态。已确定的字段值直接使用，需要生成内容或复杂规划时继续调用当前宿主模型。
3. 合并操作和目标判断，常驻模型并预热。候选表不需要把整页源码都送入模型；通过分层读取和补充上下文覆盖长页面，不给用户的项目或用例数量增加限制。
4. 执行前重新确认目标仍属于当前页面，动作后仍按独立需求断言检查真实结果。小模型输出 DONE 或高置信度不代表业务通过；不清楚时自动回到现有 AI 获取信息，无需增加人工审批步骤。
5. 作为可选本地组件接入，模型按需选择并固定版本/hash；不要默认给每个离线安装包再塞入几 GB 的模型。Windows x64、Linux x64、UOS ARM64 分别核对 ONNX/llama.cpp 运行库与 CPU 指令集。

不同实现的 confidence 不同：Simple Jev 常用 max(p)，Kev Choice 使用相对随机基线归一化，Laya 另有熵形式。Laya 源码还明确多语权重没有拟合温度。不能统一照搬 0.8 阈值或把“选择概率”当“操作正确率”；需在实际中文任务和候选规模下校准。

## 验证后再决定是否默认启用

使用 autoSkill 已有工单系统的新增、查询、修改、删除，加上登录、重复文本、遮挡、跨 tab、iframe、慢页面与失败恢复场景，比较当前宿主流程、纯小模型、混合流程。按页面/站点划分样本，保留失败，不用同模板训练集冒充测试集。

记录目标选择准确率、完整任务成功率、误操作、自动回退比例、冷启动和热推理 p50/p95，以及模型内存。端到端时间分开记录 DOM 提取、模型、网络、浏览器动作、页面等待和回退成本。当前没有本机实测，因此不能承诺这些 CPU 方案与 Jev 同样准确或一定更快。
