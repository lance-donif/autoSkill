# DSH 用例编辑与复杂定位验收

2026-11-13，新增功能在真实 DSH 内完成验收，发现的脏草稿路由缺陷已修复并定向复验。最终源码为 `b566f4eb8b9692ba6bb0be0ff445e26e8e4ae617`，属于 **v2.3.0 发布后的 main 新功能**，不代表已发布的 v2.3.0 离线包包含本次修改。实现提交：定位核心 `7536ca5`、录制 `f8a2c74`、脚本导出 `0caac9e`、后端编辑 `ee0c802`、表单抽屉 `b90f4d7`、独立集成测试 `b918d4b`、路由修复 `b566f4e`。

验收使用官方 DSH 0.2.2-rc.1，通过真实浏览器操作 DSH 中的 autoSkill iframe。保存、重读、下载与手动运行均使用实际页面入口，回放由真实内核和 Chrome 执行。本轮未发送新的模型推理；用例是明确标注来源的人工合成数据，不称为模型自行设计或录制。仅新增 `dsh-editor-acceptance` 项目，未操作旧泄密会话。

| 验收项 | 实际结果与证据 |
| --- | --- |
| API 完整表单 | 修改手写业务描述、步骤、headers、params、嵌套 JSON、布尔值和数组成员；完整定义逐字段对比通过，null、schema、抽取、依赖、来源和未知 extensions 保留。保存不启动运行，旧 Pass 变为当前待验证。手动 `run-ae5a6c311f5f` Pass。 |
| UI 动作与断言 | 保持两个动作的身份和顺序，点击改为双击；删除行内断言、新增结束断言。定位及操作改变未自动改写手写业务文案。保存后手动 `run-8edb7c5e69bb` Pass。 |
| XPath 与嵌套上下文 | 实际进入 shadow → 跨源 frame → shadow → frame → shadow；两处 frame 使用相对 XPath 与唯一元素锚点。前两个候选故意失配，末位 XPath 实际兜底 3 次。独立服务端观察到基线 1 次、双击 2 次叶层保存，其他同名表单零命中。 |
| 草稿与 409 | Escape、路由离开、真实宿主刷新取消均保留草稿。第二个真实 DSH 表单先保存，原表单提交得到 409，本地草稿保留且未覆盖远端；只有明确放弃后才重读新版本。 |
| closed Shadow DOM | `run-1524b1d12b6b` Fail，实际错误明确包含 closed。缺失访问能力没有被“元素隐藏”负断言误判为通过；这是预期反例。 |
| 新业务预期不冒绿 | 手工增加尚未映射的业务预期，保留原技术断言后运行 `run-060def1f8ed0`：执行 Pass，目标用例 business_status 为 Unverified；真实列表和详情显示“业务未验证”。 |
| Excel | 从真实 iframe 的“导出 Excel”入口下载 14 列工作簿，核对 D–G 手写正文；步骤与预期仅按 Skill 添加对应步骤序号。未将接口路径或定位器当成功能描述。 |
| 隐藏浏览器 | 保存偏好 headed=false，实际运行 headed=0；`run-1524b1d12b6b` 的 runner 31802 下观察到 Chrome 31816 携带 headless 参数，非验收外层浏览器。 |
| 窄屏与脚本异常 | 实际 iframe 宽 712/656 像素，根、抽屉与 footer 无横向溢出，保存/取消可见；最终流程 pageerror=0、非预期 console error=0。 |

首轮发现脏草稿阻止导航后 URL 已回到 cases，背景却渲染为执行记录。原始[失败测量](evidence/2026-11-13-dsh-case-editor/route-guard-reproduction.json)保留；`b566f4e` 在页面订阅执行前拦截导航。[真实 DSH 修复复验](evidence/2026-11-13-dsh-case-editor/route-guard-fixed.json)确认：继续编辑与关闭抽屉后 URL/H1 均为用例页；明确放弃导航草稿才进入执行记录。没有为此重跑已经完成的正向回放。

原有用例及报告的 **129 个文件逐字节哈希全部一致，缺失或变化 0**。这项基线不包含新合成项目，也不声称 SQLite 在新增运行后保持不变。合成数据中的两个不同功能复用了 ZLBC 前缀，导出因此保留 WQR 待确认编号；正文验证通过不等于该预置来源已完整。原始失败与业务 Unverified 均保留。弱网对照由独立任务完成，不计入此报告。

完整[证据清单与 SHA256](evidence/2026-11-13-dsh-case-editor/manifest.json)、[实际运行](evidence/2026-11-13-dsh-case-editor/actual-runs.json)、[完整合成定义](evidence/2026-11-13-dsh-case-editor/case-definitions.json)、[旧证据保护](evidence/2026-11-13-dsh-case-editor/old-evidence-preservation.json)及[Excel 读回](evidence/2026-11-13-dsh-case-editor/excel-readback.json)可独立核对。用户可读副本位于 `/workspace/shared/autoSkill-交付/DSH用例编辑验收/`。

代表截图：[API 表单](evidence/2026-11-13-dsh-case-editor/01-真实DSH接口用例表单.png)、[XPath 候选](evidence/2026-11-13-dsh-case-editor/02-真实DSH多层上下文与XPath编辑.png)、[409 草稿](evidence/2026-11-13-dsh-case-editor/03-真实DSH并发冲突保留本地草稿.png)、[业务未验证](evidence/2026-11-13-dsh-case-editor/06-真实DSH业务未验证状态.png)、[窄屏抽屉](evidence/2026-11-13-dsh-case-editor/07-真实DSH656像素iframe编辑抽屉.png)。409 图保留修复前的背景页面状态，修复结果以独立复验为准；04 为进入动画期间的原始截帧，06 为同数据的稳定画面，图片均未编辑或打码。
