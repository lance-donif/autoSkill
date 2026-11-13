# autoSkill

**从需求到回归，测试过程有据可查。** autoSkill 通过技能引导宿主 AI 理解需求、设计和审查用例、真实执行、分析缺陷并维护测试资产。内核保存来源、版本、工作进度与执行证据；模型选择沿用宿主配置。

原有的“录一次，到处回放”仍是执行基础：录下页面动作和实际接口流量，生成 Playwright、Selenium 或 HTTP 用例，后续确定性回放不需要 AI。已有测试项目、接口集合和负载计划也可纳入统一管理。

它是一个 Skill：任何能读文件、能执行命令的 AI 智能体都可以使用，不绑定某个平台。提供 Windows x64、Linux x64、Linux ARM64（统信 UOS ARM64）三个运行时包。Linux 需要浏览器依赖的系统共享库；UOS 真机尚未验收，见 [平台与第三方支持](docs/平台与第三方支持.md)。

## 它解决什么

| 以前 | 现在 |
| --- | --- |
| 每次回归都要 AI 重新理解页面、重新操作，慢且结果不稳定 | 常规回放由执行器完成；AI 在需求、业务判断、失败分析和变更维护时参与 |
| 页面测试和接口测试各写一套 | 录一次页面操作，同时得到页面用例和它背后的接口用例 |
| 每次都要提醒「先登录」 | 项目有登录用例后，录制和回放都自动先登录，登录态在本机复用，过期自动重登 |
| 用例散落在个人电脑上 | 用例、知识库、登录方式跟着 Skill 目录走，一条命令打包分享 |

## 本版本主要能力

| 目标 | 使用方式 |
| --- | --- |
| 从需求持续完成测试、补遗漏、维护变更 | `workflow` 保存阶段、来源关联、候选修改和真实结果；中断后可继续，需求变化形成新版本，旧失败不会被覆盖 |
| 多个 AI 同时处理不同任务 | 每个任务使用不同的 `--session`；浏览器、录制和临时文件独立，项目与资产共享 |
| 复用已有自动化和接口巡检 | `asset import` 保留 Python Selenium 的 pytest/unittest 项目、Postman/Apifox 集合或 JMeter 计划，`native run` 调用相应原生执行器；先用 `engine probe` 检查本机能力 |
| 验证 AI 问数与多轮对话 | 先在页面真实对话，再用 `conversation` 将捕获或 cURL/HAR 转为接口多轮资产；一条用例包含完整对话，使用独立事实或宿主判定验证回答 |
| 对正确的业务流程做负载测试 | JMeter 保留原有负载计划；对话引擎支持 HTTP、SSE、WebSocket 与并发会话，分别记录答案质量、流式完成和延迟 |
| 保留可追溯的失败 | 失败 check 仍保存期望与实际；缺少断言或只有观测基线时标记业务未验证，后续环境阻塞不会清空已经发现的业务失败 |
| 定时执行和维护 | `schedule` 统一安排回放、原生资产和维护任务；需要 AI 的工作通过宿主接续，状态与投递记录持久保存 |

完整离线包自带 Python、浏览器及 Playwright、Selenium、HTTP 回放的基础能力。**原生引擎适配器受支持，不代表第三方 CLI 已装入离线包**：pytest、Newman、Apifox、JMeter 的实际路径与可用状态以 `engine probe` 为准，资产自身的依赖也需要满足。具体调用见 [原生资产命令](skills/autoskill/references/commands.md#原生资产会话捕获与宿主判定)。

导入资产保留原文件与入口，不承诺把任意脚本无损改写成页面步骤。需要宿主判断的对话默认异步运行，宿主继续处理 `judge` 队列；确定性回放和定时执行不需要额外配置一套模型。

## 目录

| 路径 | 内容 |
| --- | --- |
| `skills/autoskill/` | 产品本体：`SKILL.md`（给 AI 的使用说明）、命令行入口、应用代码、仪表盘、示例 |
| `skills/autoskill/references/` | 命令参考、常见问题、随包的 agent-browser 离线指南 |
| `skills/autoskill/examples/ticket-desk/` | 本地合成工单与问数站点：完整 CRUD、需求版本、可控故障及 HTTP/SSE 多轮对话 |
| `plugin/` | DeepSeek Harness 插件：注册 Skill，并把仪表盘、定时任务放进侧边栏 |
| `scripts/` | 维护者用：下载运行时、组装离线包 |
| `tests/` | 自动化测试（真实浏览器、真实 HTTP、真实数据库） |
| `docs/`、`reports/` | 需求与验收清单、测试证据 |

## 使用（完整离线包）

Windows 包解压即用。Linux / UOS 先按 [Linux-DSH.md](docs/Linux-DSH.md) 运行 `sh setup-linux.sh`。给人看的入口是仪表盘，Windows：

```bat
autoskill\autoskill.cmd dashboard
```

浏览器打开 <http://127.0.0.1:8787>。第一次使用点「加载示例项目」，再点「运行全部」，就能看到完整效果。

给 AI 用：把 `autoskill` 目录交给你的智能体，例如：

> 使用这个目录里的 autoSkill 技能测试 <网址>，需求在 <文件>。请完成需求拆分、用例设计与审查、真实执行和报告；发现缺陷保留原预期。已有的 Selenium、Postman 或 JMeter 资产在 <目录>，请一起复用。

智能体读取 `SKILL.md` 后，按持久工作流处理需求和变更。用于检查安装、录制一个独立场景的基础命令如下；同一任务始终保留自己的会话标识：

```
autoskill --session "订单测试" project create --name "订单中心" --url http://10.0.0.8:8080
autoskill --session "订单测试" context <项目>
autoskill --session "订单测试" record start <项目>
autoskill --session "订单测试" browser click @e5
autoskill --session "订单测试" check contains testid=toast "已创建"
autoskill --session "订单测试" record stop --title "新建工单"
autoskill --session "订单测试" run <项目>
```

智能体的命令被宿主关在沙箱里时（DeepSeek Harness 默认如此：不能写会话目录以外的文件，不能启动浏览器），命令会自动交给在沙箱外运行的仪表盘进程执行，用法不变。所以要么启用 DSH 插件，要么先运行一次 `autoskill.cmd dashboard` 并保持窗口不关。

全部命令见 [`skills/autoskill/references/commands.md`](skills/autoskill/references/commands.md)。

问数测试默认按“页面体验 → 真实请求捕获 → 接口多轮验证 → 并发运行”推进。录制会分别保存原 HAR 与 CDP 来源，包含实际请求认证、会话字段和已捕获的流式正文；HTTP 200 或页面出现文本不等于答案正确。可用 [Ticket Desk 日常提示词](skills/autoskill/examples/ticket-desk/ACCEPTANCE.md) 练习这条流程。

自检：`autoskill.cmd verify`（按清单校验随包文件）、`autoskill.cmd doctor --launch`（真实启动一次浏览器）。

回放默认弹出最大化的浏览器窗口。想在后台运行：`autoskill.cmd run <项目> --headless`，或在仪表盘顶部工具栏右侧的「回放设置」里关掉「显示浏览器窗口」。同一处的「使用本机 Chrome」默认打开：优先用你电脑上的 Chrome，找不到或启动不了时改用随包浏览器。

## 升级与数据保留

升级步骤、外置工作区处理及失败恢复见 [升级与数据保留](docs/升级与数据保留.md)。升级先准备并校验新包、备份实际使用的数据目录，再在停止写入后切换；旧目录和恢复记录保留用于回滚。SQLite 备份包含已提交的 WAL 数据，凭据、登录态、项目资产和独立用例表按实际配置识别，不只复制默认目录。

分享包用于迁移资产；升级备份还需要保留本机运行状态与历史，两者用途不同。各平台的实测范围以升级文档和验收记录为准。

## 仪表盘

- **仪表盘**：默认汇总全部项目，可按项目和时间范围筛选；筛选同时作用于所有数字、图表和列表，并写入网址。每个指标旁有统计口径说明，点击进入同一筛选条件下的明细。
- **项目与用例**：单条运行、勾选批量运行、整个项目运行；可选 Playwright 或 Selenium，浏览器默认显示最大化窗口，也可在回放设置里关闭。
- **执行记录**：实时进度、可取消、只重跑未通过的；查看逐条断言（期望与实际）、请求与响应，页面用例还保留步骤时间线和截图。
- **定时任务**：每天、每周、固定间隔、仅一次；可编辑、暂停、立即运行。本机队列驱动可独立处理计划；机器重启后需要重新启动驱动。需要 AI 的维护依赖在线宿主，已退出的 DSH 不会被自动启动。

统计口径：通过率 = 通过 ÷（通过 + 失败）。阻塞（没有验证到结果）单独展示，不进分母；为满足依赖而自动执行的前置用例不计入。

## 分享

```
autoskill pack                       # 项目包：用例、知识、原生资产及版本化来源
autoskill pack --full --out <目录>   # 完整包：运行时、项目、已保存凭据和登录态
autoskill import <分享包>            # 对方导入
```

普通项目包不额外加入本机凭据库和登录态；`--include-secrets --passphrase <口令>` 可将用例引用的凭据加密随包携带。`pack --full` 直接包含已保存的明文凭据与登录态，不需要额外口令。项目内的原始资产和请求来源按原内容分享，其中已有的认证信息也会保留。

两种分享包都不携带本机运行历史和截图。导入的资产与历史来源不冒充本机当前版本的验证结果，仍以实际执行证据为准。

## 安全与离线

- 浏览器与接口按用例访问站点，导航、跨站跳转和删除等业务操作正常执行，不需要额外访问范围或操作审批。
- 本机凭据库和登录态保存在 `workspace/.local/`，项目设置里可直接查看和修改；用例可引用凭据名称，完整分享包会携带已保存的值。
- 仪表盘只监听 127.0.0.1，拒绝跨站写请求。它代沙箱里的命令行执行 autoskill 命令的接口只接受命令行请求，网页无法调用；只执行 autoskill 自己的命令，输出文件不能落在数据目录之外。不需要时用 `AUTOSKILL_BRIDGE_SERVER=off` 关闭。
- 测试过程中不联网安装依赖、不自动更新、不上报数据。浏览器的后台联网、组件更新、崩溃上报均关闭；升级由用户主动发起。

## 已验证与未验证

实测结果、证据和仍未解决的问题见 [`reports/交付验证报告.md`](reports/交付验证报告.md)。没有真实运行过的项目不写成通过。

本版本的页面录制、CDP 捕获、接口多轮、并发会话及错答案回归证据见 [录制转对话与压测验证](reports/录制转对话与压测验证.md)。使用的是本机固定合成数据，不代表真实模型准确率或生产容量。其他协议、执行器和平台以各自的真实验收记录为准。

## 开发

系统分层、自动维护、既有资产迁移与可插拔引擎的审查结论见 [架构审查与演进方案](docs/架构审查与演进方案.md)。其中目标设计与当前实现分别标注。

需要本机 Python 3.12+ 和 `requirements.txt` 中的库，以及 Chrome for Testing 与 chromedriver（通过环境变量 `AUTOSKILL_CHROMIUM`、`AUTOSKILL_CHROMEDRIVER`、`AUTOSKILL_AGENT_BROWSER` 指定）。

```bash
python -m unittest discover -s tests -p "test_*.py"      # Python 测试
node --test tests/plugin.test.mjs                        # 插件契约测试
ruff check .                                             # Python 静态检查（规则见 ruff.toml）
npx --yes @biomejs/biome@2.6.7 lint                      # JS / CSS 静态检查（规则见 biome.json；本仓库 .npmrc 为离线，需加环境变量 npm_config_offline=false）
python -m unittest discover -s tests -p test_conventions.py   # 项目约定检查
python scripts/fetch-windows-assets.py --assets <仓库外目录>            # 维护者：从官方源下载运行时
python scripts/build-windows-bundle.py --assets <目录> --output <新目录> --zip   # 维护者：组装离线包
```

运行时包作为 GitHub Release 附件发布，不进入仓库历史。大小和 SHA-256 以同一 Release 的附件及 `SHA256SUMS.txt` 为准。

Linux / UOS 安装与 DSH Web 接入见 [Linux-DSH.md](docs/Linux-DSH.md)，仪表盘入口为 `autoskill/autoskill dashboard`。站点白名单与浏览器代理围栏已按使用者要求移除，旧配置不再限制网络访问。

下载：[最新 Release](https://github.com/lance-donif/autoSkill/releases/latest)。`.github/workflows/release.yml` 在三个原生平台上组装包，核对全部附件 ZIP 结构、CRC、上传大小与 SHA-256 后一起发布。当前按维护者要求跳过完整功能回归与浏览器启动测试。状态见 `reports/release-status.json`。
