# 状态分类与联动

运行、用例结果、工作流及宿主上下文的 `state` 是统一状态投影。原始 `status`、工作流 `conclusion` 和执行收据保存当时事实；界面与自动处理应读取 `state`，不能把原始 Pass 当成业务已经通过。

| 字段 | 含义 |
| --- | --- |
| lifecycle / phase | 生命周期与当前阶段；排队、执行、等待宿主、等待执行、恢复、暂停、已结束、已取消 |
| outcome | 综合结果：pending / pass / fail / blocked / cancelled / unverified |
| checks.business | 独立业务验证：pass / fail / unverified |
| checks.performance / quality | 性能、答案质量：pass / fail / unverified / not_applicable；没有声明的目标不适用 |
| evidence | 本次执行证据 valid / invalid / unverified；校验实际输入、尝试收据和附件 |
| report | not_generated / pending / generating / ready / failed / skipped；ready 必须三件套核验成功 |
| delivery | pending / dispatching / unknown / delivered / not_applicable；送达只证明宿主投递，不证明业务完成 |
| scope / revision | 项目、工作流、周期、运行、用例和尝试关联，以及本次投影的内容版本 |

列表以执行阶段为主，分项结果另列。业务通过但 SLA 失败时，阶段可为已结束，业务为通过，性能和综合结果为失败。没有独立断言、只有观察或复用会话探测时，业务保持未验证。

较早业务失败在重试成功或后续阻塞后仍保留；详情展示各次尝试与末次结果。当前用例定义、依赖或配置变化后，当前用例验证失效，`current=false`，同时保留 `historical_outcome` 和原运行事实。证据损坏不能继续声称当前通过。

## 查询与推进

`workflow status <工作流> --cycle current` 默认只列当前周期，`--cycle all` 显式读取历史；游标与查询范围和工作流版本绑定。状态读取不领取工作、不执行用例、不重试导出。后台协调器收集运行终态，推进确定性阶段；等待宿主的阶段仍由宿主 AI 处理。未知副作用进入恢复调查，不自动重播。

仪表盘 `/api/runs`、`/api/results` 支持 `phase/outcome/business/performance/report` 筛选和 `cycle=current|all`。筛选在分页前执行，统计与列表使用同一范围。当前通过数不含未验证结果；历史首轮和重试指标明确标为历史尝试统计。

`GET /api/state-revision` 返回轻量版本。宿主通过会话代理请求 `/api/host/context?revision_only=true` 获取 `host_session_id/state_revision`，变化时读取完整上下文。版本检查无领取或导出副作用。在线前台正常服务下，相关状态从底层提交到界面更新应在 1 秒内；刷新保留输入、焦点、选区、滚动和筛选。离线时明确显示同步异常，恢复可见后立即重新同步。

## 报告和恢复

工作流完成时先冻结当前周期快照，再后台首次生成 Excel、简报和 Bug 清单。进入新周期不会把旧报告改为新数据。导出进程中断会持久标记失败，后台重启也不会偷偷重新导出。

报告失败与测试结论分别展示。使用界面的“重试导出报告”，或 `POST /api/task-reports/<report_id>/retry` 手动重试；重试只使用对应冻结快照，不新增运行、不重放测试。生成文件被删除或内容发生变化后，读取会返回失败，不自动修复文件。
