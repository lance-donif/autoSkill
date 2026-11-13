# 测试任务简报

- 项目：Blackbox report resume
- 任务：Blackbox HTTP lifecycle（wf-d3359b6ee0064472bc3378e095ff4022）
- 轮次：周期 cycle-707724aa170447abb4c114e87db6a35d；验证轮次 1
- 时间：2026-11-13T14:35:01.845+00:00 → 2026-11-13T14:35:30.821+00:00
- 环境：被测地址：http://127.0.0.1:18882；任务引擎：http；Python：3.12.14 (main, Aug 25 2026, 14:00:49) \[Clang 22.1.3 \]
- 执行/流程状态：violated

## 结果汇总

逻辑用例 2 条；执行结果行 2 条。

| 结果 | 数量 |
| --- | ---: |
| 通过 | 1 |
| 失败 | 1 |
| 阻塞 | 0 |
| 未执行 | 0 |
| 未知 | 0 |
| 业务未验证 | 0 |
| 已取消 | 0 |

## 主要问题

- TC-API-BB-WRONG：失败；字段 /status：期望 ready，实际 wrong

[查看完整 Excel](%E6%B5%8B%E8%AF%95%E7%94%A8%E4%BE%8B-1e4a48cfe3aa21f1.xlsx)
