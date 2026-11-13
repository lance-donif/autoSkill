# 最终复验问题：恢复完成后仍交付早期空报告

固定验收项BB11失败；其余11项通过。问题属于autoSkill报告快照与当前工作流周期的关联。

复现脚本：

```sh
python3 /workspace/autoskill-state-validation/blackbox-api/repro_report_resume.py --round report-resume-check-1
```

前提为自身fixture正在`127.0.0.1:18882`运行。脚本拒绝覆盖已存在的workspace；只调用公开CLI，不修改产品、数据库、执行收据或其他目录。

公开步骤：新建项目和workflow → 尚未形成用例时cancel → 等待空范围报告ready → resume → 提交独立requirements/design/两个规范API候选 → 实际执行 → 根据真实结果提交business_defect诊断 → knowledge → 完成并查询报告。

预期：完成后的当前周期报告应包含实际执行的2条用例、1通过/1业务失败和对应run。早期取消快照如需保留，应保持其历史身份，不能充当后续完成任务的当前结果。

实际：首周期`cycle-7ed65b4c991a4e339c09f0ee9c2b30d8`真实运行`run-26a5ae93d80b`，结果4通过、结果5因期望ready/实际wrong而失败。工作流随后`complete=true`、`conclusion=violated`、`state.lifecycle=completed`，但仍返回早期`generation=3`的ready报告：`cases=0`、`execution_rows=0`、`run_ids=[]`、`bugs_count=0`，所有summary计数为0。

报告ID：`workflow_3d354c29494a79e79444bb897374686967cea4dc50577c1f8c6ea94ba5fca5d6`。来源哈希仍为`8a86d2182c5b417e5456902b6d90696757dd5d859fb22ed04af638de90d979ab`；简报仍写unassessed及0例。原文件已复制到本轮证据中，未修改导出或收据来改变结论。

直接证据：

- [早期取消](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/018-workflow-cancel.json)与[恢复](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/019-workflow-resume.json)。
- [真实执行返回的两个结果](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/028-workflow-diagnose-next.json)与[项目中已发布的两条用例](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/029-cases-after-execute.json)。
- [完成后的状态和仍为空的ready报告](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/039-workflow-complete-status.json)。
- [实际空简报副本](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/045-stale-workflow-markdown.md)与[三文件哈希](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/045-stale-workflow-files.json)。
- [控制组第二周期](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/047-cycle2-summary.json)：相同独立预期真实重跑为新run、新结果，报告正确2例、1通过/1失败、business问题1、execution问题0，说明结果可实际进入正常周期报告。

原基线的主状态混用、未知需求阻塞已在本轮按公开统一state和真实执行复验通过；原始失败证据未删除。
