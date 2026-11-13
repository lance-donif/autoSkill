# 独立 CLI / API 黑盒基线

范围：只读公开 Skill、commands、其公开 workflow 说明、公开 case.schema.json、CLI help 与自身输出。未读实现、内部测试、其他 Agent 结果、凭据或模型配置。全部业务请求发往自产本地 HTTP fixture `127.0.0.1:18882`。隔离数据根为 `blackbox-api/workspace`；session 为 `blackbox-api-baseline`。

执行时间：2026-11-13 14:07–14:12 UTC。注意共享产品在期间发生响应结构变化：早期 `018` / `021` 无 `state`，后期 `045` / `046` 已有 `state`。原始响应按命令时间保留，不能把本轮当作单一冻结构建；以收到稳定通知后的整套复验为最终依据。

严格按 12 项验收计数：**Pass 6，Fail 4，Blocked 2**。Fail 是验收项数，已确认产品问题为两类；本地 fixture 的 `wrong` 是刻意制造的业务失败，不计为 autoSkill Bug。

| ID | 结果 | 实际证据与边界 |
| --- | --- | --- |
| BB01 | Pass | 获准的公开系统 Python 入口可运行；data status 确认隔离路径。随包 shell 入口缺 Linux runtime，单独记录为环境限制（000、001、005）。 |
| BB02 | Pass | 新建 bb-api 后初始 cases=0、knowledge=0，无借用记录（006、007）。 |
| BB03 | Fail | 原生资产可保存独立通过/失败断言（014–016），但规范候选用例虽被 accepted（052），下一步发布失败且项目 cases 仍为0（053–057）。浏览器录制另因缺 agent-browser 被阻塞（009）。 |
| BB04 | Fail | 早期完成通过运行的主 status=Pass，未将执行阶段作为主状态；outcome 中另有 completed / Pass（018）。 |
| BB05 | Fail | 早期业务失败运行的主 status=Fail，而 process.state 与 execution_status 均 completed；报告正确分出 business=1 / execution=0（021）。 |
| BB06 | Pass | CLI 观察 Running 距 started_at 0.409s；取消后首读终态距 finished_at 0.718s。后续仍 Cancelled，业务 Unverified，无虚假通过（026 watch summary、045）。这是单次 CLI 观测，未验证浏览器推送。 |
| BB07 | Pass | 重跑产生新 run ID；旧失败的 input_hash、finished_at、outcome SHA256、报告 SHA256 保持不变（021、029、032、033）。 |
| BB08 | Fail | workflow start / requirements / design / materialize accepted；next 与 recover 均报“用例关联未知需求”，而公开 read 中 BB-READY 存在且所有候选引用它。状态停 waiting_execution 并列出 run_id；没有真实 HTTP 工作流执行结果（052、053、056–060）。 |
| BB09 | Blocked | 已验证待处理工作可 cancelled → active，完成工作项保留（034、035、041）；由于工作流无法完成真实执行，尚不能覆盖“已有业务失败恢复后不改写/未知副作用不重放”。 |
| BB10 | Blocked | 已验证新 cycle_id、conclusion=unassessed，旧周期报告保持 NotRun=2 和原哈希（042–044）；旧周期没有真实工作流业务结果，无法覆盖已失败周期的完整结果隔离。 |
| BB11 | Pass | ready 三文件实际存在且大小/SHA256匹配（023）；公开HTTP metadata和三个下载均200且哈希匹配（037–040）。 |
| BB12 | Pass | 仅移除本任务导出的Excel，查询返回 failed / retryable=true / exit2；手动retry恢复ready。fixture请求4→4，run finished_at、input_hash、outcome SHA256完全未变，证明未重跑（036 summary）。 |

主要复现与原始证据见 `baseline-bugs.md`。每条 CLI 都有 `.stdout`、`.stderr` 和包含退出码、耗时、完整 argv、workspace 的 `.json`；HTTP 响应体也原样保留在 `.stdout`。

报告/证据恢复操作仅修改本目录自产导出文件，未修改结果数据库、执行收据、产品文件或其他目录。
