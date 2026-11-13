# 可执行化、验证与恢复

materialize/maintain 阶段产出候选用例或已有资产绑定，提交给工作流服务。不要先编辑当前用例正文再尝试通过 submit 补一个校验；候选提交和发布需要内容版本比较。真实执行使用固定输入及依赖快照。

录制时先确认业务预期，再操作页面。`check` 会保存期望与实际，失败也形成证据；可以保存带缺陷的测试。每个任务使用独立 session，保持 workflow/item/attempt 的关联。长任务通过 heartbeat 保存检查点；记录的动作存在未知结果时，先读取现场和操作日志，不能直接重播提交/删除等写操作。

复用 native 资产先 `engine probe`，保留入口 argv、fixture、数据文件及原断言。`native run` 默认异步并返回 run_id。轮询 native status、处理 judge next、继续其他可独立工作；队列驱动启动失败则 `native dispatch` 恢复同一队列，不重新创建运行。

当真实结果不符合需求，diagnose 区分产品缺陷、测试实现问题与未知结果。产品缺陷可提交 `disposition: business_defect`，本批完成且 conclusion 为 violated；定位器或 fixture 问题可提出候选修复后重跑，保留原判定条件。不能以反复修改期望获取通过。

knowledge 阶段记录确认过的事实和证据引用；推测单列为分析。`complete:true` 代表本批工作已处理完，是否通过看 conclusion 和实际执行证据。历史、过期、外来和手工结果均不能冒充本机当前版本验证。
