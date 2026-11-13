# 独立代码审计结论

审计对象：`/workspace/autoSkill` 在 `c116713` 之上的本轮候选修改；最终生产代码和文档哈希见 `final-source-approved.json`。仅审计并创建本目录的独立探针；未修改产品、现有测试或提交 git；未采用黑盒 Agent 的结果。

## 确认缺陷

本轮发现的缺陷均已由实现方修正，并经独立复测闭环；本审计契约范围内未剩余已确认未修项。

| 原缺陷及影响 | 最终代码定位 | 独立证据 |
| --- | --- | --- |
| 真实 Fail 收据可支持矛盾的 Pass 结果元数据；现逐项绑定真实 outcome | `execution_inputs.py:265` | `receipt-binding-result.json` |
| 原生 manifest 被改后仍显示有效；现保留摘要、项目、引擎校验 | `task_state.py:209`、`native_run.py:118` | `native-state-result.json` |
| AI/SLA 失败漏分性能、工作流把性能错算业务；未测目标错标不适用 | `task_state.py` 的 `result/run/workflow` | `native-state-result.json`、`blocked-target-result.json`、`workflow-state-result.json` |
| 状态读取驱动报告；失败可能随普通恢复自动重试 | `workflow.py:111`、`task_reports.py` 的 `peek/_ensure` | `readonly-result.json`、`workflow-state-result.json` |
| 导出进程/协调器中断，或并发等待导出进程导致自动重试 | `task_reports.py:334`、`task_reports.py:358`、`server/jobs.py` | `report-crash-result.json`、`report-concurrent-crash-result.json` |
| 普通 run/recording 导出进程退出后长期生成中 | `task_reports.py:83` | `run-report-crash-result.json` |
| 取消最后用例但 done=total 时仍发布候选 | `workflow_execution.py:162` | `workflow-state-result.json` |
| 完成后取消的宿主与工作流主状态不一致；pre-step 丢失新协议 | `task_state.py` 的 `workflow`、`plugin/host-context.mjs:17` | `host-state-consistency-result.json`、`host-prestep-state-result.json` |
| 外部 auth 修改不触发前台；当前输入范围失效未及时刷新 | `task_state.py:135`、`task_state.py:153` | `auth-revision-result.json` |
| 取消/恢复沿用旧空报告；发布需求镜像令刚通过用例自行失效 | `task_reports.py:31`、`workflow.py:531`、`workflow_execution.py:42`、`projects.py:315` | `report-generation-result.json` |
| 未验证用例的未通过数量与后端选择不一致 | `ui/js/pages/runs.js:299` | `browser-sync-result.json` 的 `unverified_retry` |
| 项目列表读取反复重写无变化索引，触发 revision→刷新反馈循环 | `projects.py:200` | `readonly-result.json`；首次失败 `readonly-projects-before-fix.json` |
| 读后并发替换 outcome 污染 proof 缓存，后续请求持续误报有效 | `task_state.py:209`，内容身份入 key、验证前后采样 | `cache-race-result.json`；首次失败原件 `cache-race-before-fix-read-hook.json` |

## 正确行为与复核

- 统一 `state` 将执行阶段、业务、性能、质量、证据、报告、宿主投递分开；原始 status 和 conclusion 保留历史事实。无独立验证不会被当前通过数计入。
- 当前周期默认，显式历史查询保留旧失败；来源/配置变更使 current=false 而保留 historical_outcome。执行证据被删除使当前综合降为未验证。
- 租约过期拒绝旧 heartbeat；未知操作留在 unknown/调查，不自动重播。取消不会自动发布待验证候选。
- 列明的 12 次状态/项目 HTTP GET 均为 200，另做 3 轮 projects/revision 重复读取；业务行、证据字节、事实 hash 和修订号稳定，render/spawn 均为 0。
- 导出失败只手动重试；真实单/双进程退出 37 及协调器状态丢失均未自动再导出。run、recording 和 workflow 的手动重试均不重跑测试。
- 同周期报告新代次使用新 ID、宿主指向新 head；真实两用例报告 ready，旧三件套与 snapshot 字节不变，发布后两用例 current=true。
- 三件套独立 hash/size/读回通过；Excel 功能表 14 列。报告 ready 不被当作执行证据或 DSH 文件卡送达证明。
- 请求级 projection 的 finally 重置隔离路径/JSON/报告缓存；全局 proof key 绑定实际已读内容，metadata 改变后重核验。并发替换后 3 次独立投影均 invalid/unverified。

机器可读汇总为 `final-checks.json`，26/26。真实 Chromium 与真实本机 API 的最终同步数值、焦点/草稿/选区和未验证重跑计数见 `browser-sync-result.json`；最终 3 次终态到 DOM 为 172.25、216.835、392.45 ms，缺 Bug 文件 129.966 ms，焦点刷新 241.0 ms；空闲 700 ms 内 DOM 重建 0 次。全部原始探针及隔离数据根保留在本目录。

## 待测边界

- 本审计未执行真实 DSH 原生文件卡送达或远端模型质量验收；不使用其他 Agent 的结果代替这些检查。
- 一秒刷新结论是记录中的本地在线夹具实测，不是任意项目体量、故障网络或生产容量承诺。
- 若生产代码超出 `final-source-approved.json` 的哈希再次变化，需要按差异复核。本结论不声称仓库不存在任何未发现缺陷。


## 单响应仪表盘增量

新增 `GET /api/dashboard` 的只读代码与最小真实 TCP 复核为 13/13，无确认未修缺陷，见 `dashboard-incremental-audit.md`、`dashboard-bundle-result.json`。汇总、最近运行与失败列表共用同一 Scope SQL 快照；未弱化证据核验。最终指纹已更新包含接口、前端和协议文档。

此增量未重跑前述 26 项或新版浏览器时延测试；上述浏览器数值保留为上一候选的独立记录。新版前端一秒性能验收仍由后续安静窗口确认，不以旧数值代替。
