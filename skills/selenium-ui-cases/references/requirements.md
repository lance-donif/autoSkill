# Selenium 测试需求与验收说明

## 范围与来源
用户指定入口 https://www.selenium.dev/selenium/web/index.html。覆盖该入口与官方 web-form.html / submitted-form.html；预期基于官方仓库源码，当前无法核对线上部署版本。本文为测试需求重建，不是 Selenium 原作者的产品 PRD。

- 表单源码：https://github.com/SeleniumHQ/selenium/blob/trunk/common/src/web/web-form.html（blob SHA 0b64a6bbed287a5114b1955674ffdb2bc6a35ac2）
- 索引源码：https://github.com/SeleniumHQ/selenium/blob/trunk/common/src/web/index.html（blob SHA 4c2ae61e37a620b4d079e9200191d62bba86c2f2）
- 提交目标源码：common/src/web/submitted-form.html（blob SHA 09eb70d4e954d14282bbf0e1642e08ce9b2de6e1）
- 索引实际依赖接口：https://api.github.com/repos/SeleniumHQ/selenium/git/trees/gh-pages?recursive=true
- GET Tree 契约：https://docs.github.com/en/rest/git/trees?apiVersion=2022-11-28

## 使用约束
只操作公开测试页、合成数据和只读依赖 API。表单使用 GET，密码字段内容可能进入 URL；仅使用 QA-DUMMY-ONLY，不使用真实密码。文件场景仅选合成文件。每条 UI 用例重新初始化页面；不用脚本直接修改 DOM 来替代真实交互。

## 需求与 UI 验收（恰好20条）

| ID | 功能 | 验收标准 |
| --- | --- | --- |
| REQ-UI-001 | 索引生成表单入口 | 列表含 web-form.html，点击后进入表单页 |
| REQ-UI-002 | 表单页面基础呈现 | title 与主标题为 Web form；Submit 可见可用 |
| REQ-UI-003 | 普通文本录入 | 输入值精确等于 Selenium QA |
| REQ-UI-004 | Unicode 与特殊字符保留 | 完整保留 Unicode、空格和特殊字符，不自动丢弃或替换 |
| REQ-UI-005 | 合成密码遮蔽 | type=password，视觉遮蔽，值为合成字符串；不截取明文秘密 |
| REQ-UI-006 | 多行文本保留 | 保留两行及换行符 |
| REQ-UI-007 | 禁用输入不可编辑 | 控件 disabled=true，不可编辑，值不变；无法聚焦是预期行为 |
| REQ-UI-008 | 只读输入值不可更改 | readonly=true，最终值仍为 Readonly input |
| REQ-UI-009 | 下拉默认值与切换 | 初始为提示选项；切换后显示 Two，value=2 |
| REQ-UI-010 | 建议值选择 | 候选源含 New York；选择后 value=New York；原生候选行为记录浏览器差异 |
| REQ-UI-011 | 选择合成本地文件 | files.length=1 且文件名 qa-fixture.txt；不要求真实目录可见 |
| REQ-UI-012 | 默认选中的复选框可取消 | 初始 checked=true；点击后 false |
| REQ-UI-013 | 默认未选中的复选框可勾选 | 初始 checked=false；点击后 true |
| REQ-UI-014 | 同组单选互斥 | my-radio-2=true 且 my-radio-1=false；不会同时选中 |
| REQ-UI-015 | 颜色默认值与更新 | 初始 value=#563d7c；选择后归一化 value=#ff0000 |
| REQ-UI-016 | 日期文本输入 | 值保留为 11/11/2026；若依赖加载失败影响交互，记录环境依赖，勿伪判通过 |
| REQ-UI-017 | 滑块下边界 | min=0、max=10、step=1；Home 后 value=0 |
| REQ-UI-018 | 滑块上边界 | End 后 value=10；再按右箭头仍为 10，不越界 |
| REQ-UI-019 | GET 提交及成功反馈 | 导航到 submitted-form.html；#message=Received!；my-text 解码后完整一致；my-select=2；my-disabled 不出现在查询中 |
| REQ-UI-020 | 返回索引导航 | 目标为 /selenium/web/index.html；索引加载失败需另记依赖原因，不把跳转成功等同列表加载成功 |

## 接口验收
仅验证索引依赖的公共 GitHub Tree API；不虚构登录、用户管理或支付 API。4 条接口用例分别覆盖递归清单、非递归清单、字符串 false 的递归语义与无效引用。来源为官方源码及文档，未从本次浏览器 Network 捕获。

## 输出与统一存储
UI/API 定义与执行结果分离，ID 不随排序变化；每次修改 revision 递增，结果必须关联 case_revision 和 run_id。schema_version=1.1.0。具体用例含前置、步骤、数据、预期、风险、来源。接口来源新增 source-and-official-docs 枚举，不冒充 OpenAPI 或浏览器抓包；这是相对 QA Master 原始来源枚举的扩展，旧校验器需显式升级后再导入。

## 未覆盖与依赖
未包含所有 Selenium 测试页、跨浏览器矩阵、全部键盘无障碍、负载与安全测试。日期/样式依赖 CDN；GitHub 目录可能被限流。目录数据被截断时不得宣称索引完整。范围滑块边界采用 HTML 原生约束。

## 当前实测环境
真实浏览器访问 index.html，刷新一次后仍显示 Site Unavailable；后续访问官方 web-form.html 同样不可达。原因尚不能归类为机器人拦截。UI 无法执行：入口用例 Blocked，依赖表单的用例也按环境不可达记录 Blocked，不计为通过。
