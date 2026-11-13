# Bug与验收边界

以下按问题行为归并，不把故意构造的业务错误或SLA失败算作autoSkill产品Bug。首次失败原件、修复中间轮次和当前复验同时保留；“已修复”不抹去原始失败，也不把首轮描述成通过。

| 编号 | 已确认问题 | 修复行为 | 最新状态 | 复验证据 |
| --- | --- | --- | --- | --- |
| B01 | 真实失败收据可被矛盾Pass元数据借用 | 逐项绑定attempt identity、status、business、assertions与真实收据。 | 已修复并复验 | [证据](证据/audit/receipt-binding-result.json) |
| B02 | 原生manifest被篡改后仍显示有效 | 验证冻结manifest摘要、项目、引擎及真实outcome/附件。 | 已修复并复验 | [证据](证据/audit/native-state-result.json) |
| B03 | 读后并发替换outcome污染后续证据缓存 | 缓存绑定实际读取内容，核验前后metadata变化时不缓存；3次后续读取均降级。 | 已修复并复验 | [证据](证据/audit/cache-race-result.json) |
| B04 | SLA失败漏分性能或错算业务；未测目标错标不适用 | 独立业务/性能/质量；声明但未测为unverified，没有声明才not_applicable。 | 已修复并复验 | [证据](证据/audit/blocked-target-result.json) |
| B05 | 查询驱动执行/报告或失败后自动重导出 | 纯读取投影，后台协调器推进；报告失败只显式手动重试。 | 已修复并复验 | [证据](证据/audit/readonly-result.json) |
| B06 | 取消最后用例但done=total时仍发布候选 | 取消生命周期优先拦截发布，保留真实旧失败。 | 已修复并复验 | [证据](证据/audit/workflow-state-result.json) |
| B07 | 宿主取消状态不一致、pre-step丢失新状态轴 | 宿主与工作流同一投影，并保留完整state。 | 已修复并复验 | [证据](证据/audit/host-prestep-state-result.json) |
| B08 | 外部认证/来源变化未联动，或发布镜像使新验证立即失效 | 监听完整输入，执行冻结前同步需求镜像，发布相同字节不重写。 | 已修复并复验 | [证据](证据/audit/publication-scope-result.json) |
| B09 | 导出进程中断、协调器重启或并发等待导致悬挂/自动重试 | 持久owner与中断失败投影；run/recording/workflow均需手动恢复且不新增业务请求。 | 已修复并复验 | [证据](证据/audit/run-report-crash-result.json) |
| B10 | 取消恢复沿用旧空报告，当前完成仍显示零用例 | 同周期恢复新报告代次，原快照和三件套字节保持不变。 | 已修复并复验 | [证据](证据/blackbox-api/evidence/report-resume-check-final/013-targeted-assessment.json) |
| B11 | 原生资产界面重跑400，误报规范用例已归档 | 按原冻结资产、版本及参数重跑；界面只提供原生整资产入口。 | 已修复并复验 | [证据](证据/fresh-blind/retest-20261113/reports/latest-8-results.json) |
| B12 | 仅原生资产的首页为空、旧周期缺标记、未验证重跑数不一致 | 纳入原生运行统计，显式历史标记，未通过基于综合outcome。 | 已修复并复验 | [证据](证据/blackbox-ui/revalidation-results.json) |
| B13 | 刷新拆除正在编辑节点，丢失输入焦点/选区 | 保留原编辑节点及草稿，并采用新回调；正常导航独立复验通过。 | 已修复并复验 | [证据](证据/blackbox-ui/dashboard-bundle-input-results.json) |
| B14 | 读取项目反复重写索引，形成revision刷新自激循环 | 条件UPSERT只写真实变化；重复GET业务行、版本、闲置DOM均稳定。 | 已修复并复验 | [证据](证据/audit/readonly-result.json) |
| B15 | 前台列表/仪表盘超过一秒，单轮曾3558/3887ms、后续1344ms | 减少重复核验、轻量版本独立轮询、首页单次一致快照；最新列表570ms、仪表盘565ms。 | 已修复并复验 | [证据](证据/blackbox-ui/dashboard-bundle-quiet-results.json) |

缓存竞态首件见[原始失败](证据/audit/cache-race-before-fix-read-hook.json)，项目GET写入首件见[原始失败](证据/audit/readonly-projects-before-fix.json)。首次完整Python的14失败/6错误包含测试夹具适配问题，分类说明见[详细报告](详细报告.md)，原日志完整保留。

真实DSH投递和完整录制器是当前环境阻塞，不归并为已修复产品问题；多组独立记录中可能重复出现。远端模型质量及生产容量不在本轮验收结论内。离线提示的最小补证见新独立组的最新记录，不把检测器未匹配正文直接算作产品失败。
