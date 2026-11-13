# 独立 CLI / HTTP API 最终复验

本轮隔离数据根：`workspace-final-retest`；session：`blackbox-api-final-retest`。时间为 2026-11-13 14:24–14:31 UTC。仅使用公开 CLI、HTTP API、公开状态规范及用例 schema、自建本地 HTTP fixture、自身输入和输出。未读实现、内部测试、其他 Agent 结果、凭据或模型配置。

固定 12 项结果：**Pass 11 / Fail 1 / Blocked 0**。机器判定和逐项原始证据见 [051-acceptance-results.json](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/051-acceptance-results.json)。

| ID | 结果 | 实测结论 |
| --- | --- | --- |
| BB01 | Pass | 公开 Python CLI 入口和独立 workspace 生效，初始未初始化。 |
| BB02 | Pass | 新项目身份正确，初始用例与知识为空。 |
| BB03 | Pass | 通过公开 workflow schema 保存两条可回放 HTTP 用例，预期独立且均保持 ready。 |
| BB04 | Pass | 通过运行的统一 state 为 completed / settled；业务通过，未配置的性能/质量不适用，证据有效、报告就绪、投递不适用。 |
| BB05 | Pass | deliberate wrong 仅业务失败，执行完成；Bug 分项为 business=1、execution=0。 |
| BB06 | Pass | 取消运行稳定为 cancelled、业务未验证；公开 HTTP 观测 Running 0.631秒、终态0.674秒。 |
| BB07 | Pass | 重跑创建新run，旧失败的输入、结束时间、结果文件哈希和冻结报告来源保持不变。 |
| BB08 | Pass | 工作流真实执行1通过/1业务失败，诊断和知识提交后 complete=true、conclusion=violated，统一阶段completed。 |
| BB09 | Pass | 已有真实失败后cancel/resume仍保留fail和同一执行收据；期间没有新增fixture请求。 |
| BB10 | Pass | 新周期最初pending/业务未验证/报告未生成；current/all准确隔离历史。第二周期真实创建新run、新结果，并只交付新run报告。 |
| BB11 | Fail | 首周期早期cancel/resume生成空报告；后来完成真实2例执行后，当前ready报告仍是原0行/0问题快照。普通第二周期报告正确。 |
| BB12 | Pass | 移除本任务自身Excel后返回failed；手动retry恢复ready，冻结执行及ready/wrong请求数不变。 |

唯一未解决产品问题详见 [final-retest-bugs.md](/workspace/autoskill-state-validation/blackbox-api/final-retest-bugs.md)。刻意返回wrong的本地fixture业务失败用于验证状态语义，不计为autoSkill产品Bug。

实时指标范围是公开HTTP接口观测。`/api/state-revision`共121个采样，最大响应66毫秒；Running/终态首次API响应分别距记录的底层时间0.631/0.674秒。并发Python回归期间，CLI首次终态观测为1.098、2.664、1.581、4.735秒，保留在原始证据中。浏览器paint、焦点、滚动未在本CLI/API范围内验证。

浏览器录制依然因缺随包agent-browser不可用；本轮通过公开workflow用例规范完成了同一用例创建与执行目标。该环境限制保留在005-record-start，不冒充录制通过。

基线6/4/2和本轮全部失败均保留。公开state-contract明确state为统一投影、原始status保留历史事实，因此本轮按state验收执行阶段，未把legacy Pass/Fail当主状态。
