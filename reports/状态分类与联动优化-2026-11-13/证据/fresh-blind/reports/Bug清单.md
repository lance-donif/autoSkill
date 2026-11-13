# Bug清单

## FB-001 原生运行详情的重跑按钮错误报告资产用例已归档

- 结果：Fail；影响原生ai-eval运行的“只重跑未通过的1条”。
- 原始运行：`run-84f885b6f65d`；原生资产：`asset-e4243c9eb456`；结果用例：`arithmetic-retry:1`。
- 独立预期42，原fixture故意返回41，随后修复fixture为42；资产从未归档。
- 复现：浏览器打开 `http://127.0.0.1:18890/#/runs/run-84f885b6f65d`，点击“只重跑未通过的1条”。
- 预期：可对该有效原生资产执行重试，并保留旧失败。
- 实际：`POST /api/runs/run-84f885b6f65d/rerun`，请求`{"failed_only":true,"headed":true}`，HTTP400，`这些用例已被归档，无法重新运行`；界面仍显示重跑按钮。
- 公开CLI佐证：`native run fresh-arithmetic asset-e4243c9eb456` 创建 `run-0e950776ad98`，真实HTTP请求后Pass，expected42/actual42。旧失败仍能从all结果中读取。
- 原始证据：[ui-retry.json](../evidence/ui-retry.json)、[操作前](../evidence/ui-retry-before.png)、[操作后](../evidence/ui-retry-after.png)、[CLI重新执行](../evidence/cli-1794580825280305189-native.json)、[CLI通过结果](../evidence/cli-1794580829776498573-native.json)。

## 未确认和阻塞

- 业务终态三次相对receipt finished_at绘制延迟1238/1190/788ms；精确DB提交时刻不可从公开入口确认，并有2核CPU配额节流。保留为严格SLA验收Blocked；不编造精确归因。
- 快速连续改两个筛选有一次all→current、焦点丢失；稳定各次HTTP响应后再次触发真实终态，筛选、焦点和滚动均保留。原始现象不删除，未列为确认Bug。
- 真实录制器缺少agent-browser，真实DSH/投递入口不可用，均Blocked，无模拟替代。
