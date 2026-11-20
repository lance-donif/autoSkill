# autoSkill

**从需求到回归，测试过程有据可查。** autoSkill 通过技能引导宿主 AI 理解需求、设计和审查用例、真实执行、分析缺陷并维护测试资产。内核保存来源、版本、工作进度与执行证据；模型选择沿用宿主配置。

原有的“录一次，到处回放”仍是执行基础：录下页面动作和实际接口流量，生成 Playwright、Selenium 或 HTTP 用例，后续确定性回放不需要 AI。已有测试项目、接口集合和负载计划也可纳入统一管理。

它是一个 Skill：任何能读文件、能执行命令的 AI 智能体都可以使用，不绑定某个平台。提供 Windows x64、Linux x64、Linux ARM64（统信 UOS ARM64）三个运行时包。Linux 需要浏览器依赖的系统共享库；UOS 真机尚未验收，见 [平台与第三方支持](docs/平台与第三方支持.md)。

在 GitHub Pages 中可以查看用例、模拟运行和修改设置。操作仅在当前页面有效，刷新后恢复示例数据。[打开交互预览](https://lance-donif.github.io/autoSkill/preview/dashboard.html)

## 它解决什么

| 以前 | 现在 |
| --- | --- |
| 每次回归都要 AI 重新理解页面、重新操作，慢且结果不稳定 | 常规回放由执行器完成；AI 在需求、业务判断、失败分析和变更维护时参与 |
| 页面测试和接口测试各写一套 | 录一次页面操作，同时得到页面用例和它背后的接口用例 |
| 每次都要提醒「先登录」 | 项目有登录用例后，录制和回放都自动先登录，登录态在本机复用，过期自动重登 |
| 用例散落在个人电脑上 | 全部数据保存在独立数据文件夹，更新程序不搬数据，整目录可分享 |

## 本版本主要能力

| 目标 | 使用方式 |
| --- | --- |
| 从需求持续完成测试、补遗漏、维护变更 | `workflow` 保存阶段、来源关联、候选修改和真实结果；中断后可继续，需求变化形成新版本，旧失败不会被覆盖 |
| 多个 AI 同时处理不同任务 | 每个任务使用不同的 `--session`；浏览器、录制和临时文件独立，项目与资产共享 |
| 复用已有自动化和接口巡检 | `asset import` 保留 Python Selenium 的 pytest/unittest 项目和 Postman/Apifox 集合，`native run` 调用相应原生执行器；先用 `engine probe` 检查本机能力 |
| 验证 AI 问数与多轮对话 | 先在页面真实对话，再用 `conversation` 将捕获或 cURL/HAR 转为接口多轮资产；一条用例包含完整对话，使用独立事实或宿主判定验证回答 |
| 对正确的业务流程做负载测试 | 仪表盘通过 Locust 对已有接口用例执行性能测试和阶梯压力测试；对话引擎支持 HTTP、SSE、WebSocket 与并发会话 |
| 保留可追溯的失败 | 失败的检查保留期望与实际值。执行中断时会记录原因，已发现的失败可在历史报告中查阅 |
| 定时执行和维护 | `schedule` 统一安排回放、原生资产和维护任务；需要 AI 的工作通过宿主接续，状态与投递记录持久保存 |

完整离线包自带 Python、浏览器及 Playwright、Selenium、HTTP 回放的基础能力。**原生引擎适配器受支持，不代表第三方 CLI 已装入离线包**：pytest、Newman 和 Apifox 的实际路径与可用状态以 `engine probe` 为准，资产自身的依赖也需要满足。具体调用见 [原生资产命令](skills/autoskill/references/commands.md#原生资产会话捕获与宿主判定)。

导入资产保留原文件与入口，不承诺把任意脚本无损改写成页面步骤。需要宿主判断的对话默认异步运行，宿主继续处理 `judge` 队列；确定性回放和定时执行不需要额外配置一套模型。

## 源码目录

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

> 使用这个目录里的 autoSkill 技能测试 <网址>，需求在 <文件>。请完成需求拆分、用例设计与审查、真实执行和报告；发现缺陷保留原预期。已有的 Selenium、Postman 或 Apifox 资产在 <目录>，请一起复用。

在 DeepSeek Harness 中，先输入无参数的 `/autoskill-use` 加载技能，随后直接对话描述任务。这个命令不接受 `task`、`mode`，也不会自行启动模型任务；模型选择继续沿用当前宿主配置。输入框上方不再显示 autoSkill 任务栏，运行状态与报告可在原生工具结果卡和仪表盘查看。

> 用多智能体续测 Bootstrap Accordion。复用上次“展开 Item #2”的用例，再点 Item #1，验证 #1 展开、#2 折叠，把新用例存到原目录并给我 Excel。

主会话负责分工和交付，执行方在可见浏览器里回放前置、验证新增操作并保存结果。以后想手动重跑，可以在「测试用例」里按业务目录找到它。

智能体读取 `SKILL.md` 后，按持久工作流处理需求和变更。用于检查安装、录制一个独立场景的基础命令如下；同一任务始终保留自己的会话标识：

```
autoskill --session "订单测试" project create --name "订单中心" --url http://10.0.0.8:8080
autoskill --session "订单测试" context <项目>
autoskill --session "订单测试" plan write <项目> --file 功能用例.json
autoskill --session "订单测试" record start <项目>
autoskill --session "订单测试" browser click @e5
autoskill --session "订单测试" check contains testid=toast "已创建"
autoskill --session "订单测试" record stop --title "新建工单" --plan DESIGN-001
autoskill --session "订单测试" run-status <record stop 返回的运行编号> --brief
```

先依据需求来源编写 `功能用例.json`，示例中的 `DESIGN-001` 应与文件中的设计编号一致。正式 Excel 主表保存功能目标、必要前置、实际操作和可观察预期；原始请求、浏览器动作和断言保留在附表。没有来源支持的设计明确标为待补，修改设计后需要对应版本的真实验证。具体字段和页面流程见 [设计与需求维护](docs/设计与需求维护.md)。

如果新需求从一个已有用例的页面状态接着做，将录制开始命令换成 `record start <项目> --reuse <用例编号>`。工具在本次可见窗口执行指定的前置版本，验证通过后从同一标签页录新动作。测试用例页回放这条新用例时也会先执行记录的前置。[同页前置实测](reports/DSH同页前置复用验收.md)

宿主将智能体命令放在受限沙箱内时，autoSkill 可将命令转交给沙箱外的仪表盘进程执行，用法不变。前提是沙箱允许访问 `127.0.0.1`，且 DSH 插件已启动服务，或用户已运行 `autoskill.cmd dashboard` 并保持进程运行。本轮已在真实 DSH 的 `workspace-write` 策略下验证绑定的数据状态查询及 UI 回放；具体任务、录制和原生自测的实际范围见 [DSH 真实验收](reports/DSH真实验收.md)。

Bootstrap 实测覆盖多智能体从对话生成用例、两层前置接续和从目录手动回放。[DSH 首轮协作与链式回放验收](reports/DSH首轮协作与链式回放验收.md) · [DSH 对话协作与可见窗口验收](reports/DSH对话协作与可见窗口验收.md)

命令参数和更多示例见 [命令手册](skills/autoskill/references/commands.md)。

问数测试默认按“页面体验 → 真实请求捕获 → 接口多轮验证 → 并发运行”推进。录制会分别保存原 HAR 与 CDP 来源，包含实际请求认证、会话字段和已捕获的流式正文；HTTP 200 或页面出现文本不等于答案正确。可用 [Ticket Desk 日常提示词](skills/autoskill/examples/ticket-desk/ACCEPTANCE.md) 练习这条流程。

自检：`autoskill.cmd verify`（按清单校验随包文件）、`autoskill.cmd doctor --launch`（真实启动一次浏览器）。

录制、回放、登录和 DSH 对话任务默认显示最大化浏览器窗口。Linux 显示窗口需要图形桌面，缺少桌面时会报告启动受阻。用户可在「回放设置 → 显示浏览器窗口」保存隐藏选择，也可用 `--headed` / `--headless` 或 `AUTOSKILL_HEADED=1/0` 临时覆盖。开启「使用本机 Chrome」时优先使用本机浏览器，找不到或启动失败时改用随包浏览器。

## 程序与数据分开

所有 autoSkill 数据保存在一个程序外的目录：Windows 默认 `%USERPROFILE%\autoSkill-data`，Linux/UOS 默认 `$HOME/autoSkill-data`。项目、用例、知识、录制、运行记录、截图、报告、凭据、登录态、设置和调度状态都在里面。仪表盘「数据管理」或 `autoskill data status` 显示实际位置。

**更新只换程序文件，继续使用这个数据目录。没有升级脚本。** 旧版首次迁入用「数据管理」的导入入口，将旧数据复制到空目标并保留原目录；后续每次升级无需再复制数据。DSH 重新加载插件模块或重启后使用新代码，详情见 [升级与数据保留](docs/升级与数据保留.md)。

分享完全相同的 autoSkill 数据，用「导出全部数据」或 `autoskill data export --out <完整数据.zip>`；也可以停止写入后压缩整个数据文件夹，包括隐藏的 `.local` 和 `data-root.json`。对方导入到空目录后选择它作为数据目录，历史证据按原字节保留。程序包、宿主模型配置和第三方执行器各自安装。

## 仪表盘

- **总览** 按项目和时间范围汇总测试结果，点击指标可查看对应明细。
- **测试用例** 按业务目录查找、运行和导出用例，支持单条、批量和项目回归。
- **执行记录** 查看运行进度、断言结果、请求与响应，以及页面用例的步骤和截图。
- **定时任务** 设置每天、每周、固定间隔或一次性回放，可编辑、暂停或立即运行。
- **数据管理** 查看当前数据目录，导入旧数据或导出完整数据包。

目标步骤全部完成、已配置检查通过且执行记录完整时，用例结果为通过。未配置业务断言时，按步骤执行结果判定。目标开始前取消或受阻记为未执行，开始后中断、检查失败或执行证据缺失记为失败。 运行详情保留具体原因，已经交付的历史报告可继续查阅。

从「执行记录 → 压测记录」可查看接口压测报告。两种模式均由随离线包安装的 Locust 调度。性能测试使用固定并发和启动集合点，填写持续时间、目标请求数或两者，两项都填时先达到的条件结束派发。压力测试按设置逐级提高并发。两个模式分别提供 JMeter 指标和图表，可以下载离线 HTML。压测结果独立于常规回归汇总和用例最新状态。[接口压测操作指南](docs/接口压测使用.md)

## 分享

```
autoskill pack                       # 项目包：用例、知识、原生资产及版本化来源
autoskill pack --full --out <目录>   # 完整包：运行时、项目、已保存凭据和登录态
autoskill import <分享包>            # 对方导入
```

普通项目包不额外加入本机凭据库和登录态；`--include-secrets --passphrase <口令>` 可将用例引用的凭据加密随包携带。`pack --full` 直接包含已保存的明文凭据与登录态，不需要额外口令。项目内的原始资产和请求来源按原内容分享，其中已有的认证信息也会保留。

项目/运行时分享包会携带项目目录中的报告附件，但不包含本机 `.local/` 中的运行历史数据库和截图。需要恢复完整历史与执行证据时使用 `data export`，不要用 `pack --full` 替代。导入的报告、资产与历史来源不冒充本机当前版本的验证结果，仍以实际执行证据为准。

## 安全与离线

- 浏览器与接口按用例访问站点，导航、跨站跳转和删除等业务操作正常执行，不需要额外访问范围或操作审批。
- 本机凭据库和登录态保存在 数据根的 `.local/`，项目设置里可直接查看和修改；用例可引用凭据名称，完整分享包会携带已保存的值。
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

`.github/workflows/release.yml` 在三个原生平台上组装包，核对全部附件的 ZIP 结构、CRC、上传大小与 SHA-256 后一起发布。两个 Linux 构建均验证安装配置，Linux x64 还使用包内运行时执行浏览器、中断恢复和接口压测回归。构建状态见 `reports/release-status.json`。 [最新 Release](https://github.com/lance-donif/autoSkill/releases/latest)
