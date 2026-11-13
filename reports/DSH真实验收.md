# DSH 真实宿主验收

日期：2026-11-13。最终程序源码为 `a2b0ddc3ef5f746306e4149011b45e69e8565772`。本轮使用官方 DSH、用户指定的真实远程模型和本 checkout 插件执行；历史协议替身测试不计入本轮实测。

## 最终结果

| 范围 | 实际结果 |
| --- | --- |
| 官方 DSH、远程模型、唯一无参技能命令 | 通过；`autoskill-use` 注入完整技能后，下一自然任务真实调用插件与 CLI |
| 插件网页冻结资产自测 | **104 个父测试全部通过**；另有 20 条成功子测试，共 124 条原生结果记录，0 失败、0 跳过 |
| 真实 DSH iframe 展示 | 12 页 × 3 种视图，共 36 个视图复测加载成功，0 pageerror、0 根布局横向溢出 |
| 默认隐藏浏览器 | 保存 `headed=false`、宿主没有 `AUTOSKILL_HEADED`、真实 `DISPLAY=:99` 时，普通技能任务实际启动 headless Chrome；显式显示窗口另有真实 X11 验证 |
| 菜鸟教程最终来源设计与 UI 回放 | **FT-JCYF-001 / TC-UI-004 / run-83be44ec7c93：Pass**，新录制的 6 条独立检查通过，当前证据有效 |
| 本轮文件交付 | 两个最终 DSH 任务均真实 `present` 三件原始报告，工具成功且轮次 completed；菜鸟稳定交付目录已建立 |

本轮没有重写旧失败。插件自测经历首版 101/104 通过、第二版机器中断和恢复后全量通过；各阶段分开列于下文。Selenium 官方站点专项的最新结果仍为 **12 Pass、5 上游连接 Fail**，不是上述插件自测的分母，见 [专项报告](Selenium官方测试页验证.md)。

## 宿主、技能与隔离边界

- 官方 DSH `0.2.2-rc.1`、Node `v24.22.1`，安装于仓库外。真实 DSH Web 为 `127.0.0.1:18880`；通过官方 `dsh plugin --profile web add link:/workspace/autoSkill` 加载本 checkout，插件实际启动仪表盘 `127.0.0.1:18887`。
- 模型采用用户指定的 `deepseek-v4.1-flash` 和网关，返回的实际模型标识为 `deepseek/deepseek-v4.1-flash-1017`。独立请求及真实 DSH Web 对话均成功；[连接截图](evidence/2026-11-13-dsh-live/model-connected.png)不是本地协议模型或预置回复。
- 冷加载后的命令目录只有本插件的无参 `autoskill-use`。真实 `skills.get`、`Agent.inject` 回执保留 `skill-invocation / instructions` 来源，注入完整技能与运行绑定；激活没有增加模型轮次，后续自然任务才实际执行。见 [激活证据](evidence/2026-11-13-dsh-live/use-activation.json)。
- 保持 DSH 的 `workspace-write` 策略，未修改系统安全策略。数据使用隔离根 `/workspace/scratch/dsh-acceptance-private/data`，模型连接配置位于该数据根之外，不覆盖用户数据。
- TaskDock、自动通知和强制宿主待办移除后，新最终菜鸟会话实际工具链为 Bash → read → stat → present，**0 todo_write**。用户主动打开的仪表盘和报告保留；本报告最后的代表图由验收者主动打开报告后拍摄。
- 公开证据只保留脱敏摘要与审阅过的图像。含私有配置的旧诊断会话原件、认证 URL、浏览器认证存储和原始宿主日志仅在仓库外私有目录保存，不进入 Git 或稳定交付目录；该诊断会话没有交付截图。最终干净会话没有继承它的上下文。

## 插件网页真实 DSH 自测

预先编写的仓库测试和合成 fixture 被复制成不可变原生资产；不声称这些测试由模型自主设计。准备阶段仅编译和收集，**没有先在宿主外跑 unittest 充当 DSH 验收**。真实远程 DSH 模型在无参激活技能后，实际执行 `asset import` 和异步 `native run`；原生驱动随后运行真实 Chromium、HTTP 服务与执行器。

V2 资产保持相同的 103 个原测试和 1 个完整性检查，逐项验证文件 size/SHA、冻结来源导入和隔离数据根。驱动不清除 `NO_PROXY`，本轮使用产品修复后的真实 Python 子进程环境。

| 阶段 | Git 源码 | 运行 | 真实结果 |
| --- | --- | --- | --- |
| 首版 | `bab27360c335f1db8adf38aba44d5ad2770a3c81` | `run-a572f263974a` | 104 父测试：101 Pass、3 Fail；另 20 条成功子测试，合计 124 条记录；原三报告保留 |
| V2 首次启动 | `a2b0ddc3ef5f746306e4149011b45e69e8565772` | `run-82a5ccc545e0` | 机器中断前只看到 1 条完整性检查成功输出，没有完整 unittest/outcome；恢复后正常 kernel 回收为 Blocked |
| V2 明确恢复复验 | 同上 | **`run-bafb6788bda0`** | **104/104 父测试通过**，20 条子测试通过，共 124 条原生记录全 Pass；stderr 为 `Ran 104 tests in 296.134s`，三报告真实 present，轮次 completed |

首版三个失败是两条多轮对话页面链和原生取消链。HTTPX 在目标请求发出前遇到 `Invalid port: ':1]'`：DSH 为 Node/undici 使用的括号 IPv6 绕过项被 Python 继承。修复只为 Python 子进程规范化，并由 AI 传输读取入口兜底，保留宿主 undici 环境；没有靠验收参数清空代理环境使测试变绿。

V2 中断时所有原执行进程都已消失。先只读保存 SQLite 行、输入清单、原始日志和出生身份，再启动正常服务。kernel 只将遗留行的 `status / reason / finished_at` 改为恢复终态，未自动重放未确认操作；输入、255 条冻结文件引用及原 stdout/stderr 等字节均未改变。随后真实 DSH 对同一固定资产发起新的明确复验，没有重复导入或重复执行旧 start 脚本。

V2 的身份须分别理解，不能把 domain head 当作 Git commit：

| 身份 | 完整值 |
| --- | --- |
| Git source commit | `a2b0ddc3ef5f746306e4149011b45e69e8565772` |
| Domain / native asset head | `71d65853732aaeaec806b525168f476f0bef96c9ffceee03d860e16697ead00a` |
| Asset version | `deac2102fb3a21fbeabc12ab1957bd69a35f0f0e42f3aa2278a63cb2783c5e14` |
| Snapshot manifest SHA-256 | `58022a9f54807840e2cbb1a75dc77dea83734edb5d3270ababb06044257a363d` |
| 恢复运行 input hash | `7bc7071695f883d10103e65ed4bf0b4965ffef50452451b5d359a1e791862644` |

实际覆盖压测选择与取消、仪表盘筛选稳定性、嵌套目录和批量移动及递归导出、知识资料和分享导入、原生资产及报告/取消、多轮会话、项目设置和凭据、设计表与历史、调度窗口偏好、数据导入导出与删除恢复、100→20 需求影响与维护、真实录制兼容及遥测分类等。逐项测试映射保留原来的覆盖边界，不把局部测试扩大成所有可能分支的保证。

证据：

- [首版失败摘要](evidence/2026-11-13-dsh-live/native-selftest-v1/summary.json)
- [中断与恢复前后字节审计](evidence/2026-11-13-dsh-live/native-selftest-v2-interrupted/recovery-input-audit.json)
- [最终终态、104 个父 ID 与真实交付回执](evidence/2026-11-13-dsh-live/native-selftest-v2-recovery/summary.json)
- [原始 unittest 结果](evidence/2026-11-13-dsh-live/native-selftest-v2-recovery/unittest.json)、[stderr](evidence/2026-11-13-dsh-live/native-selftest-v2-recovery/stderr.log)
- [冻结来源清单](evidence/2026-11-13-dsh-live/native-selftest-v2-snapshot-manifest.json)、[功能覆盖与边界](evidence/2026-11-13-dsh-live/native-selftest-v2-coverage-map.json)

## 菜鸟教程：原 Skill、规范编号与新版本真实回归

真实 DSH 先调用用户上传并适配的 `test-cases` Skill，读取 `SKILL.md`、`references/workbook-rules.md`、`ADAPTATION.md`、`source.json`，核对原 RAR 及规范文件 SHA-256，再写出 [完整功能设计原稿](evidence/2026-11-13-dsh-live/runoob-functional-design.json)。功能说明、必要条件、四项真人步骤、逐项预期和三级业务路径均有来源。

最终版本通过正确的最小 `PATCH` 将原设计行 `FT-JCYF-001` 的 `prefix` 明确为 `JCYF`，D–G、business_path、source 和原备注保持不变。随后在新的干净真实 DSH 会话中，无参激活 autoSkill，再由一次 Bash 调用预先审阅的顺序控制脚本，实际经 autoSkill CLI 完成新录制、snapshot/describe、真实点击、独立 check、`record stop --plan` 和回放。控制脚本由验收者提供；它不直接使用手写 Playwright 代替录制。

最终 `TC-UI-004 / run-83be44ec7c93` 为 Pass、business_verified=true，当前证据有效。六项检查是教程地址、章节导航、基础语法标题、编码、标识符、Python 保留关键字；与前一个功能版保持同一业务预期，没有为编号补全改变 oracle。真实浏览器动作是打开首页、进入 Python3 教程、打开基础语法章节。

实际新 Excel 的 A 列为 **FT-JCYF-001**，B 为 `菜鸟教程-Python3 教程-基础语法`，C 为 `基础语法`；D–G 与完整来源设计逐字一致，没有使用 GET 地址、CSS 或 `@e` 记录冒充真人功能步骤。H 保留未知编制人的待确认标记，J 来自真实通过结果。原 RAR 校验器对实际原件返回 1 功能项、1 用例、3 层、0 错误、0 警告。没有单独渲染 Excel 页图；网页报告代表图不是 Excel 渲染证明。

三个本轮原件均 ready、可由宿主读取，真实 `present` 工具交付成功且轮次 completed。稳定目录为 `/workspace/shared/autoSkill-交付/菜鸟教程/`，README、用例、报告、证据和 SHA-256 清单共 9 个文件；上传用 Excel 和报告原 Excel 同字节。

- [最终来源、A–G 读回、case/input hash、六项检查与交付回执](evidence/2026-11-13-dsh-live/runoob-jcyf-final/summary.json)
- [正式 Excel](evidence/2026-11-13-dsh-live/runoob-jcyf-final/测试用例-de47f08dc944e1dd.xlsx)、[简报](evidence/2026-11-13-dsh-live/runoob-jcyf-final/测试简报-de47f08dc944e1dd.md)、[Bug 清单](evidence/2026-11-13-dsh-live/runoob-jcyf-final/Bug清单-de47f08dc944e1dd.md)
- [原 Skill 校验结果](evidence/2026-11-13-dsh-live/runoob-jcyf-final/skill-validation.json)、[真实回放截图](evidence/2026-11-13-dsh-live/runoob-jcyf-final/replay.png)、[真实 DSH 报告界面](evidence/2026-11-13-dsh-live/runoob-jcyf-final/dsh-report.png)

保留的历史不与最终版本混同：

| 版本 | 结果与边界 |
| --- | --- |
| V1 `TC-UI-001 / run-def65ea1b7b9` | 实际录制、3 检查与回放通过；尚未按本轮最终功能设计和编号要求交付，原件保留 |
| V2a `TC-UI-002` | 验收者给出的标题字面检查过严，要求“Python3 教程”，实际标题在 Python 与 3 之间有空格，为“Python 3 教程”；Fail 原样保存，未宣称网站业务缺陷 |
| V2b `TC-UI-003 / run-d6ae0ac423e0` | 新录制 6 检查和功能回放通过，但 prefix 留空导致正式 Excel 为 WQR 待确认编号；结构校验通过不等于最终命名规范完成 |
| 最终 V2c `TC-UI-004 / run-83be44ec7c93` | 补全来源命名输入后重新录制和真实回放；FT-JCYF-001 与完整功能设计统一通过 |

本轮还保留了网关 503 中断、早期 Chrome 控制失败和一次验收脚本误用新增接口而被 400 拒绝的记录。没有覆盖旧结果或手动改 Excel 使其变绿。新录制按 `business-traffic-v2` 排除 RUM 等遥测业务候选，同时保留完整原始 HAR/CDP 来源；旧 RUM 资产未改写。WordPress 接口仍只是观察候选，本轮没有把它们计作已验证 API 业务覆盖。范围是公开教程导航，不是菜鸟全站全部业务。

## 真实 iframe、隐藏窗口与实际下载

实际在 DSH 侧栏 iframe 中巡检仪表盘、项目与用例、压测、资料、原生资产、会话、工作流、设计用例表、执行记录、调度、数据管理和诊断，共 12 页。使用 1440×1000 浅色、1440×1000 深色、768×1000 浅色三种规格。

首轮发现项目与用例及执行记录窄屏根布局溢出。修复后同 36 视图全部加载，0 pageerror、0 根布局溢出，两页窄屏根宽均与 iframe 的 712 像素一致；表格在自身容器横向滚动。见 [首次巡检](evidence/2026-11-13-dsh-live/iframe/inspection.json) 与 [最终复测](evidence/2026-11-13-dsh-live/iframe-final/inspection.json)。72 张原图中的 63 张按原字节归档到仓库外，保留代表图和首败图，两个 inspection JSON 未改；[索引](evidence/2026-11-13-dsh-live/screenshot-archive-index.json)保留每张原路径和 SHA。

普通 DSH 技能任务在真实 DISPLAY 环境、宿主不设置 HEADED 时读取保存的隐藏偏好。预置工单 UI 运行 `run-fc3822e16f22` 实际通过且 Chrome 带 headless，三报告交付；[证据](evidence/2026-11-13-dsh-live/hidden-ui-run-summary.json)明确它是预置 fixture，不声称该模型自行录制。默认录制和调度/重启窗口选择也纳入本轮实际测试；显式可见模式的独立窗口验证通过。

真实 DSH 曾因受限 `data status` 本地访问活动 SQLite 失败。修复后同 `workspace-write` 策略和运行绑定的 [宿主工具回执](evidence/2026-11-13-dsh-live/data-status-fixed-event-summary.json)通过，没有使用 immutable/WAL 绕行。

真实 iframe 下载还覆盖 Selenium 项目 17 条、父目录“表单”递归 14 条、叶目录“文本输入”5 条：三个正式文件的 D–G 全文及业务路径/追踪与已保存设计一致，原 Skill 校验均无警告。见 [实际下载与逐条比较](evidence/2026-11-13-dsh-live/iframe-downloads-v2/downloads.json)。历史 HTML 报告也实际下载并离线读取，0 外网请求、嵌入截图正常；[记录](evidence/2026-11-13-dsh-live/iframe-downloads/offline-readback.json)不冒充后续新版业务文案验收。

这些合成站点与冻结回归证明所列页面和执行链的实际行为；不据此宣称生产压测容量、远程模型准确率、Windows/UOS 设备验收或未安装第三方执行器已运行成功。详细功能边界见 [DSH 覆盖映射](DSH自测覆盖映射.md)。

稳定交付入口：[菜鸟教程交付说明](/workspace/shared/autoSkill-交付/菜鸟教程/README.md)、[Selenium 官方测试页交付说明](/workspace/shared/autoSkill-交付/Selenium官方测试页/README.md)。公开文件、压缩包内文件和本轮公开截图的检查结果见 [敏感信息扫描记录](evidence/2026-11-13-dsh-live/public-artifact-scan.json)。原始私有诊断日志不在扫描或提交范围内。
