# 独立状态协议审计（中间记录，非最终签核）

范围：`/workspace/autoSkill`，基线 `c116713`。只读取产品和现有测试；自编探针及数据均在本目录；未提交 git；未接收黑盒 Agent 结果。

## 已确认并已验证修正

- 结果元数据与真实执行收据矛盾：`execution_inputs.py:265`，现不再允许真实 Fail 收据支持 Pass 元数据。`receipt-binding-result.json`。
- 原生冻结 manifest 篡改：`task_state.py` 的 `_evidence` 现使用 `_frozen`，证据 invalid、综合 unverified。`native-state-result.json`。
- AI 多轮性能断言和工作流分项聚合：实际业务/质量 Pass、SLA Fail 保持独立，综合 Fail。`native-state-result.json`、`workflow-state-result.json`。
- 纯状态读取不渲染报告；普通 resume 不重试失败导出；手动 retry 不新增 HTTP 或 run，且 source_hash 保持。`workflow-state-result.json`。
- 三件套文件失效：canonical 和宿主报告摘要均降级 failed；独立 Excel/UTF-8/hash/size 读回通过，功能表 14 列。`trio-readback-result.json`。
- 执行收据删除后 run/workflow 综合均 unverified；历史观察保留。`workflow-state-result.json`。
- 当前周期默认查询、显式 all 历史查询、旧周期失败记录保留。`workflow-state-result.json`。
- 租约过期的旧 heartbeat 被拒绝为 stale_lease；未知操作仍 unknown，生成未领取调查，不重放。该未知收据为确定性恢复夹具，未声称真实远端副作用。`workflow-state-result.json`。
- 最后用例期间取消且 done=total：现 publication=interrupted，进入 recovering，未自动发布。`workflow-state-result.json`。
- 完成后取消的工作流与宿主主状态一致。`host-state-consistency-result.json`。
- 导出进程退出 37：无论协调器持续存活或丢失本地注册后重启，均只实际导出一次并转 failed。两个真实进程并发时，等待进程不会再次导出。`report-crash-result.json`、`report-concurrent-crash-result.json`。
- 宿主 pre-step 快照现保留统一 state，而非只保留旧 Pass/conclusion。`host-prestep-state-result.json`。
- 真实 set_auth 变更使当前用例 current true→false、综合 pass→unverified，并改变前台轮询 revision。`auth-revision-result.json`。
- 已声明 SLA 但连接失败：有 max_ms 的用例 performance=unverified，无目标用例 not_applicable。`blocked-target-result.json`。
- 早期取消后恢复生成新报告 head=1，真实两用例结果进入新三件套；旧报告及快照字节不变。执行前同步需求镜像后，刚发布的两用例均 current=true/outcome=pass；发布仍比较被冻结的需求摘要。`report-generation-result.json`。
- 最终 HTTP 状态 GET 共 10 次全部 200；全部 SQLite 业务行、执行/报告文件字节不变；render/spawn=0。`readonly-result.json`。
- 实际 Chromium + 实际本机 API，无 API 路由 mock，3 次外部短任务终态到 DOM 为 227、630、598 ms；缺 Bug 文件到 DOM 为 188 ms，无 pageerror。这是该本地夹具观察，不代表任意历史规模的容量承诺。`browser-sync-result.json`、`browser-sync.png`。

## 当前确认待修项

全部已独立复测闭环；最终分类、26/26 汇总及源码指纹见 `final-audit.md`、`final-checks.json`、`final-source-approved.json`。首次失败原件仍保留。

## 最终待测

- 等主 Agent 通知变更稳定后，按最终 diff 和文件哈希复核；本文及早期 findings 不自动视为最终代码结论。
- 复核最后两个修正后重新固化生产代码指纹；首轮候选冻结后仍有 9 个生产文件变更，已逐项追加阅读。
- 独立探针覆盖本地 HTTP、原生 unittest、AI HTTP 协议夹具、Chromium 和故障注入；不声称真实远端模型质量或 DSH 真机验收。
