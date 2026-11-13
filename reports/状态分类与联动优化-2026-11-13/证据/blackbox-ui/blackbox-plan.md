# 独立黑盒验收计划

记录时间：2026-11-13。只使用公开 SKILL.md、commands.md、CLI --help、实际 dashboard 和公开 API。禁止读取实现源码、内部测试、其他 Agent 结果及既有工作区数据。

## 验收用户任务

1. 在新隔离数据目录启动真实 dashboard，建立本地 HTTP 测试项目，用真实录制和回放构造通过、业务失败与执行问题。
2. 从 UI 发起/观察任务，核对主状态只表示执行阶段，并独立展示业务结果、性能结果、报告结果；活动执行不提前显示为业务通过。
3. 从任务列表进入详情并对照统计，验证同一任务的状态、各结果与计数一致。
4. 在线前台保留页面，后台经公开 CLI 完成任务；测量结果发布到 UI 可见变化的延时，要求不超过 1 秒。
5. 自动刷新前设置筛选与未提交输入，任务更新后核对筛选与输入仍保留。
6. 对同一维护/工作流执行两个周期，核对当前周期与历史周期有清晰区分，历史结果不改写当前结果。
7. 如公开可操作方式能使报告导出失败，核对任务执行结果仍保留、报告单列失败，并通过 UI/公开 retry 仅重试报告。
8. 真实 DSH 宿主及主模型调用环境不可用时单列阻塞，不以本地 dashboard 或模拟宿主冒充实测。

## 基线

公开入口：`/workspace/autoSkill/skills/autoskill/autoskill`。

首次隔离运行 `--help` 返回 exit 3：`Linux runtime not found. Run sh setup-linux.sh in the matching x64/ARM64 package, or follow docs/Linux-DSH.md.` 尚未启动产品或创建数据。

## 证据

将公开命令 JSON、真实 UI 截图、浏览器可见文本、请求/响应时间线存放于本目录；预期依据为本任务验收目标与公开用户文档。替代环境仅验证产品 dashboard，可验证范围与 DSH 阻塞分开报告。

## 基线构造与观察（候选版本冻结前）

- 经主任务提供的公开系统 Python 模块入口成功启动真实 dashboard（18782）与本地独立订单站（18783）。
- 真实录制因缺少随包 agent-browser 阻塞；Chromium/Playwright 均可运行，故原生 unittest 使用真实 HTTP 业务返回进行独立断言。通过 asset import 创建资产 asset-864fcbae1cad。
- 原生通过运行 run-109d89264273：1 通过、0 失败、报告 ready；HTTP 200 但 message=订单失败 的真实业务失败运行 run-7fae166ab28f：0 通过、1 失败。
- 仅删除自己通过运行的导出 Excel（不改执行收据或数据库），公开 task-report 返回 failed/retryable，真实 UI 出现「报告导出未完成」「重试导出报告」。保留未重试状态以便候选版本验收。相关证据：report-before-missing.json、report-after-missing.json、report-baseline-runs_run-109d89264273.png。
- 公开 workflow start/next/submit 完成需求来源、需求、设计与 materialize；实际执行 run-e8c9e3e0d741 为 Pass。随后 next 返回 validation_error「用例关联未知需求」，阻碍完整周期收敛；requirements 公开读接口仍包含该 ID。证据 33～40。
- 通过公开 workflow revise 开启第二周期 cycle-f8e0f09686ee4490855cea8c3bef6bce，当前等待宿主/未验证；旧周期 cycle-d40fb808592f4eb6bfd04fb3423e388b 与真实通过执行保留，可用于历史筛选验收。证据44、45。
- 开发期间 UI 已变化而原后台进程尚未重启，出现 /api/state-revision 404、服务已断开和状态未同步。该前后端混用现象不作为冻结版本的最终缺陷结论；待通知后重启后台复验。
- 仅使用公开 pack 导出的自己新建空项目制作接口统计导入包，因公开需求格式不足返回「需求必填字段缺失」而跳过导入；没有直接写项目数据。该无效输入不算产品业务失败。
- asset init/map 均不在实际公开 CLI 支持的动作中；真实性能阈值案例缺少完整公共格式，不猜实现，暂列阻塞。可验证已有原生运行的性能维度为不适用。

## 候选版本复验脚本

- realtime_ui.py：前台详情与列表的完成自动更新时延、筛选保留、未提交项目输入保留。
- report_retry_ui.py：真实 UI 手动重试报告、三个文件下载校验、同一冻结快照、执行 ID/结束时间/结果/运行数量不变。
- consistency_ui.py：列表详情状态与计数、当前和历史周期过滤/标识、仪表盘执行统计。
