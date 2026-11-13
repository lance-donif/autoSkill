# 仪表盘单响应增量复核

确认未修缺陷：0。增量真实 TCP 检查 13/13；未重跑全量 26 项，未启动浏览器或原生组，TCP 服务已关闭，证据文件已恢复原字节。

代码范围：server/app.py:270 的 GET /api/dashboard；server/metrics.py:330 的 dashboard；ui/js/pages/dashboard.js 的 render；references/state-contract.md 的接口说明。

- dashboard 外层建立 Scope snapshot；summary、recent、failures 复用同一 SQL 连接和同请求证据投影。实际 14 次 scope 查询使用同一连接和快照。
- 前端只 await 一个 dashboard 响应，再一起绘制汇总、最近运行、失败列表；分项和证据核验函数未被绕过。
- 实际既有执行数据下 3 runs/3 results，bundle 与原 summary/runs 接口一致，失败范围 all=1/current=0，limit 和项目过滤保留总数。
- 纯 GET 不改变任何 SQLite 业务行、报告/执行证据字节，不生成报告或启动执行。
- 同一服务连续响应中破坏已通过尝试收据，passed 由 1 降 0；恢复原收据后重新核验恢复。请求级/全局缓存没有遮蔽失效。

机器证据：dashboard-bundle-result.json。候选哈希：final-source-approved.json；上一候选哈希保留在 final-source-before-dashboard-bundle.json。

待测：本增量未重新做新版前端一秒性能验收，不能将上一候选的浏览器时延当作新版实测；该项由根线程后续安静窗口独立验收。本复核未采用黑盒结果或预设结论。
