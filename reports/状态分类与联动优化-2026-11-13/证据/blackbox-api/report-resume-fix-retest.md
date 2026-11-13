# 报告恢复问题修复后追加复验

新round：`report-resume-check-final`；独立数据根：`workspace-report-resume-check-final`；workflow：`wf-d3359b6ee0064472bc3378e095ff4022`。使用原公开最小复现流程，仅增加文件快照、哈希及内容读回。没有读取产品实现或其他Agent结果，没有修改数据库、执行收据或旧证据。

**追加复验BB11：Pass 1 / Fail 0 / Blocked 0。** 首轮完整复验仍保留11/1/0；合并每项最新证据为12/0/0。其余11项沿用上一轮已通过的证据，本次没有宣称再次完整执行全部12项。

公开步骤仍为：新建项目/workflow → 用例形成前取消 → 等取消报告ready并实际读取三份旧文件 → resume → 需求/设计/两个API候选 → 真实执行 → 根据真实结果诊断与知识提交 → 完成 → 实际读取新报告及旧文件，并再次查询旧task-report。

| 检查 | 实际结果 |
| --- | --- |
| 取消报告 | 0例，Excel、简报、Bug清单均ready且实际文件大小/哈希匹配。 |
| 恢复时状态 | active / awaiting_host，业务unverified，报告not_generated。 |
| 完成报告身份 | 新report_id，与取消报告不同。 |
| 完成报告内容 | 真实run `run-f306302c4b9f`；2例、1通过/1业务失败；business问题1、execution问题0。 |
| 新三文件 | 大小/SHA256全部匹配；Excel包含真实run及两条原始case，简报写2例，Bug清单保留expected ready / actual wrong。 |
| 旧文件不变 | 取消时的三个旧路径仍存在，前后实际字节大小和SHA256逐一相同。 |
| 旧报告可读 | 完成后按旧report_id查询仍ready；source_hash和Excel哈希保持不变。 |

取消报告ID：`workflow_92b1464ab16f3b2ab0bb03ccbdc77e28a33851d019594712218fcc5c0f7341b1`。

完成报告ID：`workflow_13f20a6ce692dbe0b9ea2a39350f02e99d61b1e61091c82cdf3a84393a618a31`。

独立业务预期始终是JSON status=ready。wrong端点继续产生真实业务失败，完成阶段仍为completed、结论violated；追加验收通过表示报告关联与保留符合预期，不表示把业务失败改为通过。

证据：[定向判定](/workspace/autoskill-state-validation/blackbox-api/evidence/report-resume-check-final/013-targeted-assessment.json)、[完整前后比较](/workspace/autoskill-state-validation/blackbox-api/evidence/report-resume-check-final/008-report-comparison.json)、[取消时文件快照与哈希](/workspace/autoskill-state-validation/blackbox-api/evidence/report-resume-check-final/004-cancelled-files.json)、[完成后三文件检查](/workspace/autoskill-state-validation/blackbox-api/evidence/report-resume-check-final/009-completed-files.json)、[旧文件再次读回](/workspace/autoskill-state-validation/blackbox-api/evidence/report-resume-check-final/010-cancelled-files-after.json)、[旧报告公开查询](/workspace/autoskill-state-validation/blackbox-api/evidence/report-resume-check-final/011-old-cancelled-report-query.json)。

合并结果：[latest-acceptance-results.json](/workspace/autoskill-state-validation/blackbox-api/latest-acceptance-results.json)。首轮失败：[final-retest-bugs.md](/workspace/autoskill-state-validation/blackbox-api/final-retest-bugs.md)，及其全部原始文件均保留。
