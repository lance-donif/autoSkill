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

项目目录中的定义仍是索引来源。重复读取未变化的项目不会重写索引、更新时间或递增状态版本；真实定义变化才更新相关索引并触发刷新。

仪表盘 `/api/runs`、`/api/results` 支持 `phase/outcome/business/performance/report` 筛选和 `cycle=current|all`。筛选在分页前执行，统计与列表使用同一范围。当前通过数不含未验证结果；历史首轮和重试指标明确标为历史尝试统计。

仪表盘首页使用 `/api/dashboard` 一次返回同一读取快照的汇总、最近运行和失败列表；`current=true` 选择当前仍失败列表。页面在完整响应到达后一起更新，避免多轮请求重复核验及不同读取时刻的数据混用。

`GET /api/state-revision` 返回轻量版本。宿主可通过会话代理请求 `/api/host/context?revision_only=true` 获取 `host_session_id/state_revision`，需要时读取完整上下文。对话工具卡保持请求驱动，不创建任务dock、被动轮询或主动消息；嵌入仪表盘使用主站自己的250ms版本检查。版本检查无领取或导出副作用。在线前台正常服务下，相关状态从底层提交到界面更新应在 1 秒内；刷新保留输入、焦点、选区、滚动和筛选。离线时明确显示同步异常，恢复可见后立即重新同步。

同一次统计响应共用 SQLite 读取快照和证据核验结果，响应结束即清空。跨请求的证据缓存同时绑定实际读取内容和文件元数据；核验期间文件发生变化时不写缓存。证据、需求来源或配置变化仍会使当前验证失效。

## 报告和恢复

工作流完成时先冻结当前周期快照，再后台首次生成 Excel、简报和 Bug 清单。进入新周期不会把旧报告改为新数据。导出进程中断会持久标记失败，后台重启也不会偷偷重新导出。

取消时已经冻结的报告保持原样。同一周期恢复后使用新的报告代次，后续完成报告包含恢复后的真实运行；旧报告的标识、快照和三件套字节继续保留。

报告失败与测试结论分别展示。使用界面的“重试导出报告”，或 `POST /api/task-reports/<report_id>/retry` 手动重试；重试只使用对应冻结快照，不新增运行、不重放测试。生成文件被删除或内容发生变化后，读取会返回失败，不自动修复文件。
