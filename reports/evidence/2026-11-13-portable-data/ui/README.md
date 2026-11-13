# 数据管理页与偏好持久化验证

最终统一运行：6/6 真实 Chromium 浏览器用例通过，0 跳过。浏览器访问独立启动的真实 Python HTTP API，每个用例使用自己的临时数据目录。目标 Ruff、5 个 UI JavaScript 文件的 Biome、7 项 conventions 均通过。metadata.json 记录命令、退出码以及 5 个 UI 文件和 4 个测试文件运行前后 SHA-256；这些拥有文件在最终运行期间未变化。

覆盖：
- theme/engine/headed 写入当前数据目录 `.local/settings.json.dashboard`；另一浏览器禁用 localStorage 后仍读取这些服务端值。
- 旧浏览器偏好仅在服务端允许时填补缺失字段；已保存值优先；同一浏览器切换另一数据根不再继承旧值。全新数据根不接纳未领取的旧值。
- 旧页面连接到另一数据根时，theme 和本机 Chrome 开关保存收到真实 409；界面原值不变、新数据根 settings 零写入。
- 保存失败可见且不伪造已生效；导入失败保留可见错误。
- 通过真实浏览器下载完整 ZIP，再导入另一空目录；逐项保留凭据、证据文件、服务端偏好和 SQLite 中原有 Fail 历史。此历史行是明确的合成迁移样本，不是这项测试新执行的业务用例。
- 导入成功只显示“已导入，尚未启用”，原服务仍使用原目录；重复导入非空目标失败，不覆盖已有文件。

先前独立执行的既有 UI 回归：dashboard_review_fixes 5/5，dashboard_deletions 9/9，test_html_report 3/3。它们只增加新 GET settings 的 fixture 合同，没有更改业务断言。

开发联测中修正过两个测试刺激问题：对同 origin 相同 hash URL 的 page.goto 不会重新加载文档，现明确 reload；真实创建项目接口需要 `url` 而不是 fixture 最初误用的 `base_url`。最终结果不是首次尝试稳定率的度量。

截图来自实际页面，包括正常桌面、成功导入仍未启用、错误可见及 390px 窄屏。page_errors 为空，窄屏 document width 与 viewport 均为 390。运行结束后所有临时服务和数据均由 fixture 清理。

本证据只证明这批 dashboard UI/数据接口集成，不等于 DSH Web、宿主真实模型或生产设备验收。原真实 DSH SDK 9 项证据保持独立，未把这批浏览器结果扩大到模型验收。
