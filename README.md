# autoSkill

**从需求到回归，测试过程有据可查。** autoSkill 通过技能引导宿主 AI 理解需求、设计带风险优先级的用例、真实执行并维护测试资产；确定性回放（Playwright、Selenium、HTTP）与定时巡检无需调用 AI 模型。

它是一个不绑定宿主的通用 Skill：任何能读文件、执行命令的 AI 智能体均可使用，并提供 Windows x64、Linux x64、Linux ARM64（统信 UOS ARM64）三个离线运行时包。Linux 需浏览器系统共享库，UOS 真机尚未验收，见 [平台与第三方支持](docs/平台与第三方支持.md)。

可在 GitHub Pages 查看用例、环境与报告模拟效果（刷新恢复示例数据）：[打开交互预览](https://lance-donif.github.io/autoSkill/preview/dashboard.html)

## 核心能力

| 场景 | 能力说明 |
| --- | --- |
| 需求即出用例与 `autoSkill交付/` | 对话中明确提出需求或给出需求文档时，自动生成带 `P0`/`P1`/`P2` 优先级的正式 14 列用例 Excel；单需求平铺、多需求按子目录归档，仅保留最新 Excel，报告按分钟生成并在冲突时自动加 `-2`/`-3` 序号；检测 Windows Excel 占用与人工改表差异后再确认合并 |
| 录一次，到处回放 | `agent-browser` 录制页面操作与真实接口流量，一次沉淀页面用例与接口用例（关联 `{{变量}}`）；支持导出独立 Playwright/Selenium 脚本与 Postman 集合，缺失未解析变量在运行前和导出脚本中直接报错拦截 |
| 环境变量与平台多账号 | 项目级环境管理支持基础地址、数十个自定义环境变量（表格编辑与 `KEY=VALUE` 批量文本切换）、登录开关、平台与多账号绑定；各环境平等按配置决定是否登录，按 `平台 + 环境 + 账号` 隔离登录态并核对入口，同账号用例串行排队、全局默认并发 2 |
| 跨环境单 ZIP 同步与冲突拆分 | 仅同步通过状态用例，产出带分钟时间戳的单 ZIP 同步包，自动延迟处理未传完的半截包，用不可变内部 ID 避免多人撞车；中心环境与账号默认保留本地，冲突弹窗支持将「业务用例」与「环境/账号」分开勾选或导出双版本对比 |
| 失败单 ZIP 错误包与完整计数 | 失败或阻塞运行在本地错误目录与「执行记录」页面统一生成并下载无明文密码的单 ZIP 错误包；报告与统计完整保留 Pass / Fail / Blocked / NotRun / Flaky / Unverified 六类状态，不以绿灯掩盖未验证或缺陷 |
| 原生资产、AI 问数与接口压测 | `asset import` / `native run` 复用现有 pytest/unittest、Postman/Newman、Apifox 资产；`conversation` 支持 HTTP/SSE/WebSocket 多轮对话与宿主判定；内置 Locust 固定并发/阶梯压测与 JMeter 5.6.3 指标图表 |
| 定时巡检与多会话并行 | `schedule` 按任务独立保存目标环境、账号与浏览器显示模式并持久化调度；多个 AI 任务通过 `--session` 完全隔离浏览器与录制现场 |

完整离线包自带 Python 3.12、Chrome for Testing、chromedriver、agent-browser 及回放依赖；第三方 CLI（pytest、Newman、Apifox）实际可用状态以 `autoskill engine probe` 为准，见 [原生资产命令](skills/autoskill/references/commands.md#原生资产会话捕获与宿主判定)。

## 源码目录

| 路径 | 内容 |
| --- | --- |
| `skills/autoskill/` | 产品本体：`SKILL.md`（AI 使用契约）、CLI、Python 内核、仪表盘前端及示例 |
| `skills/autoskill/references/` | 命令手册、常见问题、工作流阶段规范与离线浏览器指南 |
| `skills/autoskill/examples/ticket-desk/` | 本地合成工单与问数示例站，日常练习见 [Ticket Desk 验收指南](skills/autoskill/examples/ticket-desk/ACCEPTANCE.md) |
| `plugin/` | DeepSeek Harness（DSH）插件：注册 Skill、后台服务桥接与侧边栏仪表盘/定时任务面板 |
| `docs/` | 安装部署、升级迁移、设计与需求维护、接口压测、平台支持与版本发布说明 |
| `scripts/` | 维护者工具：静态/约定审查、资产拉取、多平台离线包构建、Pages 预览生成与发布校验 |
| `tests/` | 自动化回归测试（真实浏览器、HTTP、SQLite、同步包、多账号排队及 Chromium UI） |
| `reports/` | 交付验证台账、最新 Release 校验元数据（`release-status.json`、`offline-package.json`）及发布证据 |

## 快速开始（离线包）

Windows 离线包解压即用；Linux / UOS 先按 [Linux-DSH.md](docs/Linux-DSH.md) 执行 `sh setup-linux.sh`。

```bat
autoskill\autoskill.cmd dashboard
```

浏览器打开 <http://127.0.0.1:8787>（Linux 为 `autoskill/autoskill dashboard`）。日常自检：`autoskill.cmd verify`（校验随包文件清单）与 `autoskill.cmd doctor --launch`（真实启动浏览器）。

在 AI 对话中使用时，把 `autoskill` 目录交给智能体；在 DSH 中先输入无参数的 `/autoskill-use` 加载技能，再描述网址与需求文档即可自动推进设计、录制、执行与交付。核心 CLI 流程（同一任务始终携带独立 `--session`）：

```bash
autoskill --session "订单测试" project create --name "订单中心" --url http://10.0.0.8:8080
autoskill --session "订单测试" plan write <项目> --file 功能用例.json --requirement "订单回归"
autoskill --session "订单测试" record start <项目>
autoskill --session "订单测试" browser click @e5
autoskill --session "订单测试" check contains testid=toast "已创建"
autoskill --session "订单测试" record stop --title "新建订单" --plan DESIGN-001
autoskill --session "订单测试" run-status <run_id> --brief
```

需求阶段暂无测试地址时可省略 `--url` 先建项目交付 14 列设计表，待开发完成后用 `project update` 补齐地址或切换环境账号直接回归，详细流程见 [设计与需求维护](docs/设计与需求维护.md) 与 [命令手册](skills/autoskill/references/commands.md)。

## 仪表盘与多环境管理

- **总览**：汇总项目与周期通过率、最近批次状态（含完整 6 类状态分布）及失败明细下钻。
- **测试用例**：按业务目录管理与运行用例，各项目标题旁可直接切换独立**环境**与**账号**（按登录配置动态显隐），运行前自动扫描缺失 `{{变量}}` 并提前提示。
- **项目设置侧栏**：顶部切换环境，提供「基础与登录」与「变量 (数量)」双标签页（支持变量名搜索、只看空值筛选、紧凑行内编辑、复制已有环境变量名及表格/批量文本 `KEY=VALUE` 互转）。
- **执行记录 & 压测记录**：展示实际运行的项目、环境、账号、断言详情、截图证据及单 ZIP 错误包下载；子入口提供固定并发与阶梯压测报告，见 [接口压测操作指南](docs/接口压测使用.md)。
- **定时任务**：独立绑定目标环境、账号与窗口显隐偏好，支持一次性与周期巡检。
- **数据管理**：查看数据目录、导入旧数据、导出完整数据包，以及扫描同步包与拆分处理「业务用例 / 环境账号」同步冲突。

## 程序与数据分离、分享及离线安全

所有运行数据统一保存在程序外独立目录（Windows 默认 `%USERPROFILE%\autoSkill-data`，Linux/UOS 默认 `$HOME/autoSkill-data`）。更新版本只需替换程序目录，无需迁移脚本；首次从旧版迁入或整库备份使用「数据管理」或 `autoskill data export` / `data import`，见 [升级与数据保留](docs/升级与数据保留.md)。

- `autoskill pack` 导出项目用例、知识、原生资产与版本来源（可选 `--include-secrets --passphrase` 加密携带引用凭据）；`autoskill pack --full` 携带运行时与已保存凭据/登录态（保留本地运行历史请用 `data export`），详见 [项目资料与分享](docs/项目资料与分享.md)。
- 凭据与登录态按明文保存在 `.local/` 并在项目设置中可见可改，错误包与公开报告绝不包含明文密码。仪表盘仅监听 `127.0.0.1` 并拦截跨站写请求；测试期间不联网下载依赖、不上报遥测，也不自动更新。

## 开发与验证

需要 Python 3.12+、`requirements.txt`（Linux 为 `requirements-linux.txt`）、Node 22+ 以及 Chromium/ChromeDriver（通过 `AUTOSKILL_CHROMIUM`、`AUTOSKILL_CHROMEDRIVER`、`AUTOSKILL_AGENT_BROWSER` 指定）。维护约定与架构契约见仓库根目录 `CLAUDE.md` 与 `AGENTS.md`。

```bash
python -m unittest discover -s tests -p "test_*.py"             # Python 回归测试
node --test tests/*.test.mjs                                    # 插件与预览契约测试
ruff check .                                                    # Python 静态检查（ruff.toml）
npm_config_offline=false npx --yes @biomejs/biome@2.6.7 lint    # JS / CSS 静态检查（biome.json）
npm run lint:landmines                                          # Landmines JS 规则检查
python -m unittest discover -s tests -p test_conventions.py     # 项目约定与 CSS tokens 检查
python scripts/code_review.py check --rule repository           # 仓库闭环与 CI 引用检查
```

离线包由 `.github/workflows/release.yml` 在 Windows x64、Linux x64、Linux ARM64 原生环境构建并校验 ZIP/CRC/SHA-256，Linux x64 同时在虚拟桌面下跑通包内运行时浏览器与回归验证。历史边界与验证记录见 [`reports/交付验证报告.md`](reports/交付验证报告.md)，构建状态见 `reports/release-status.json`。[最新 Release](https://github.com/lance-donif/autoSkill/releases/latest)
