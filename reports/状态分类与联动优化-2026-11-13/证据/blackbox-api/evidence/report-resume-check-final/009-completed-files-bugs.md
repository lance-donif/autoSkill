# Bug 清单

- 项目：Blackbox report resume
- 任务：wf-d3359b6ee0064472bc3378e095ff4022；轮次：周期 cycle-707724aa170447abb4c114e87db6a35d；验证轮次 1
- 时间：2026-11-13T14:35:30.821+00:00
- 环境：被测地址：http://127.0.0.1:18882；任务引擎：http；Python：3.12.14 (main, Aug 25 2026, 14:00:49) \[Clang 22.1.3 \]
- 任务状态：violated
- 任务说明：未记录

| 分类 | 涉及用例版本数 |
| --- | ---: |
| 业务断言失败（待定位） | 1 |
| 执行或性能问题 | 0 |
| 失败记录（证据待核验） | 0 |
| 录制时的问题线索 | 0 |
| 历史失败（执行证据已删除） | 0 |

本轮共 2 个用例版本、2 条结果；问题按用例版本归组，不表示同一根因。
业务失败、执行问题和录制观察分别记录；未执行、已取消、未知或业务未验证不代表没有 Bug。

## BUG-001 · Wrong endpoint preserves business violation

- 类型：业务断言失败（待定位）
- 用例：TC-API-BB-WRONG；冻结定义 SHA-256：9cd70033c9f91ef5bdc18796acbea4f1f2ddf4987a13d2a9d595d830023a1a6d
- 问题摘要：字段 /status：期望 ready，实际 wrong
- 预期：\[{"检查": "字段 /status", "预期": "ready"}\]
- 实际：\[{"检查": "字段 /status", "实际": "wrong"}\]
- 复现线索：GET /api/value；参数及前置条件见同轮 Excel 中的冻结定义
- 执行历史：失败
- 证据：运行 run-f306302c4b9f；详细步骤、检查和证据引用见同轮 Excel 的执行明细

[查看同轮 Excel 与完整证据引用](%E6%B5%8B%E8%AF%95%E7%94%A8%E4%BE%8B-1e4a48cfe3aa21f1.xlsx)
