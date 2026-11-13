# 独立黑盒问题清单

## BB-BUG-01：早期主状态混合执行阶段与业务结果

环境：公开系统 Python CLI，隔离 workspace，selenium-unittest 原生 HTTP 测试。

预期：执行已正常完成时主状态体现完成；业务 Pass/Fail 独立展示。业务失败不等于执行失败。

实际：`native status` 顶层 status 对通过用例为 `Pass`，对业务断言失败用例为 `Fail`。失败用例同时明确返回 `outcome.process.state=completed`、`outcome.execution_status=completed` 和 `outcome.business_status=Fail`；早期响应无统一 state。

复现：运行 `inputs/asset-ready.json` 和 `inputs/asset-fail.json`，对返回 ID 查询 native status。独立断言写在 `inputs/native-http/test_fixture.py`，wrong 返回值必须与 ready 比较，不能改变预期获得绿色。

原始证据：`evidence/baseline/018-native-ready-status.json`、`021-native-fail-status.json`。后期 `045` / `046` 已出现新 state，本项待冻结复验，不将响应变化当自动通过。

## BB-BUG-02：接受规范候选后，执行发布报未知需求并停在等待执行

预期：requirements 提交与 materialization 候选均已接受，同一工作流中合法引用该需求的候选能进入真实执行；若不能执行，应明确执行阻塞，不能只留下等待阶段。

实际：`workflow read --kind requirements` 中有 `BB-READY`；`workflow read --kind materialization` 中两条候选均引用 `BB-READY`，且 submit 返回 accepted。随后 workflow next 和 recover 均报 `用例关联未知需求`（validation_error、exit2）。项目 cases 仍为空，workflow status 却停在 waiting_execution 并给出 run_id `run-00bf0722aa71`。

复现：使用 `inputs/workflow-v2.json`、`requirements-v2.json`、`design-v1.json`、`materialize-v2.json`，依照公开 workflow next 返回的 schema 和 lease/attempt 依次提交。候选使用公开 `references/case.schema.json` 规范，未直接改用例文件或内部状态。

原始证据：`052-workflow-v2-materialize.json`（accepted）、`053-workflow-next.json`（错误）、`054-cases.json`（0用例）、`056-workflow-read-requirements.json`（需求存在）、`057-workflow-read-materialization.json`（关联一致）、`058-workflow-status-error.json`、`060-workflow-recover-error.json`。

## 环境与覆盖限制

- 随包 shell 入口缺 Linux runtime；公开 Python 入口可用，未安装依赖。
- `record start` 缺随包 agent-browser，录制场景 Blocked，不是业务失败。
- 工作流真实执行失败阻止了完整“已有业务失败暂停恢复”和“失败历史周期隔离”验收，这两项保持 Blocked。
- 1秒指标仅有一次CLI取消样本，浏览器实时推送未覆盖。
