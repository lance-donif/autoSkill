---
name: selenium-ui-cases
description: 对 Selenium 官方测试站执行恰好20条 UI 用例，覆盖索引、表单控件与提交导航；保留用例来源、版本、逐条断言与证据。用户要求 Selenium UI 用例 Skill 或真实浏览器执行这20条用例时使用。
---
# Selenium UI 用例

先读取 references/requirements.md、references/cases.json、references/case.schema.json。用例 JSON 为权威源，固定20个稳定 ID，不把每一条用例各自安装为一个技能。

使用当前宿主提供的真实浏览器工具并遵守其规则；不要用 HTTP 请求或源码检查代替 UI 执行。先尝试用户指定的 https://www.selenium.dev/selenium/web/index.html；官方表单页为同目录 web-form.html，目标为 submitted-form.html。页面可见或源码位置发生变化时先核对，不直接按旧定位器点错元素。

逐条执行：从干净表单状态开始，按 steps 进行真实输入/点击；读取控件值、禁用/只读/选中状态、当前 URL 或可见反馈作为断言。用宿主支持的属性读取方法取证，不通过脚本修改 DOM 伪造状态。文件仅使用合成 qa-fixture.txt；密码仅为 QA-DUMMY-ONLY，GET 表单不可放真实秘密。不要扩大到任意页面或执行其他写操作。

结果与定义分离，保存 run_id、case_id、case_revision、case_content_sha256、started_at、status、actual、reason、assertions 与 evidence。Pass 必须逐项断言成功并有可定位证据；Fail 要有实际失败断言；环境不可达记 Blocked；未尝试记 Not Run。用例修改递增 revision，结果内容哈希必须对应定义。DOM 状态与截图尽量按用例保存，所有证据用项目相对路径；截图不得含秘密。

来源为官方仓库源码，不能假设与线上部署完全一致；本轮初始浏览器记录为 Site Unavailable，不沿用任何通过结果。若再次不可达，停止同站重试，完成阻塞报告。可用网络捕获只用于补充，不等于接口测试完成。

向用户交付统一 JSON、20条用例清单与执行报告；可按需求导出 Excel，导出器必须支持 schema_version=1.1.0，不能直接喂给只接受1.0.0的旧引擎。新增源码确认来源枚举的兼容说明见需求文档。UI 用例不覆盖全站或完整兼容性矩阵。
