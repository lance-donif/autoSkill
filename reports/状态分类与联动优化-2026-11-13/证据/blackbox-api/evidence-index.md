# 独立黑盒证据索引

| 内容 | 文件 |
| --- | --- |
| 冻结的12项独立任务 | [acceptance-plan.md](/workspace/autoskill-state-validation/blackbox-api/acceptance-plan.md) |
| 原始基线6/4/2 | [baseline-summary.md](/workspace/autoskill-state-validation/blackbox-api/baseline-summary.md) |
| 原基线问题与阻塞 | [baseline-bugs.md](/workspace/autoskill-state-validation/blackbox-api/baseline-bugs.md) |
| 最终复验11/1/0 | [final-retest-summary.md](/workspace/autoskill-state-validation/blackbox-api/final-retest-summary.md) |
| 逐项机器结果及证据label | [051-acceptance-results.json](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/051-acceptance-results.json) |
| 当前报告问题精确复现 | [final-retest-bugs.md](/workspace/autoskill-state-validation/blackbox-api/final-retest-bugs.md) |
| 公开状态规范和用例规范快照哈希 | [public-documents.json](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/public-documents.json) |
| 全套脚本入口与边界 | [reproduce.md](/workspace/autoskill-state-validation/blackbox-api/reproduce.md) |
| 新workspace整套复验 | [revalidate.py](/workspace/autoskill-state-validation/blackbox-api/revalidate.py) |
| 报告恢复问题最小复现 | [repro_report_resume.py](/workspace/autoskill-state-validation/blackbox-api/repro_report_resume.py) |
| 本地HTTP fixture | [fixture.py](/workspace/autoskill-state-validation/blackbox-api/fixture.py) |
| fixture原始请求记录 | [fixture-requests.jsonl](/workspace/autoskill-state-validation/blackbox-api/evidence/fixture-requests.jsonl) |
| HTTP状态实时延迟和周期筛选 | [046-api-assessment.json](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/046-api-assessment.json) |
| 报告丢失→failed→手动retry且未重跑 | [015-report-recovery-summary.json](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/015-report-recovery-summary.json) |
| 新周期真实执行与正确报告 | [047-cycle2-summary.json](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/047-cycle2-summary.json) |
| 正常报告三文件独立哈希和14列检查 | [050-cycle2-report-files.json](/workspace/autoskill-state-validation/blackbox-api/evidence/final-retest/050-cycle2-report-files.json) |

`evidence/<轮次>/<label>.json`保存完整argv/URL、workspace、UTC时间、退出码或HTTP状态、耗时及解析响应；同名`.stdout/.stderr`保留原字节。API版本采样另有121份原始响应。所有数据、输入、脚本和报告都仅位于本目录内；没有访问其他测试Agent目录。

追加修复复验：BB11已在新round `report-resume-check-final`通过，取消旧三文件保持原字节，完成后新报告含真实2例。首轮11/1/0和旧失败完整保留；[追加复验说明](/workspace/autoskill-state-validation/blackbox-api/report-resume-fix-retest.md)单独记录本次1/0/0，[每项最新证据合并结果](/workspace/autoskill-state-validation/blackbox-api/latest-acceptance-results.json)为12/0/0，其余11项沿用首轮通过证据。
