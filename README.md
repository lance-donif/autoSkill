# autoSkill

支持 **Skill** 与 **DeepSeek Harness 插件** 两种形式。插件版提供仪表盘、用例库、执行记录、定时任务和运行配置页；脚本模式无需AI，AI模式通过官方DSH SDK。完整安装说明见 [plugin/INSTALL.md](plugin/INSTALL.md)。

AI 自动化测试 Skill 原型：根据需求生成 UI/API 用例，统一存储并导出，结合真实浏览器和独立 HTTP 执行器保存测试结果。

## 当前内容

| 目录 | 内容 |
| --- | --- |
| `skills/autoskill/` | 主控、用例生成、Excel、知识库、浏览器适配、存储规范及 Python 引擎 |
| `skills/selenium-ui-cases/` | Selenium 官方测试页20条 UI 用例 Skill |
| `skills/selenium-api-cases/` | 索引依赖 GitHub Tree API 的4条接口用例 Skill 与执行器 |
| `examples/selenium/` | 测试需求、标准 JSON、真实接口结果与浏览器阻塞记录 |
| `tests/` | 11项基础回归及6项问题复现探针 |
| `reports/` | 已知问题、测试报告和真实执行摘要 |

## Windows x64 内网使用

使用依赖已内置的Windows完整包；解压后直接执行，不需要联网初始化：

```bat
skills\autoskill\run-windows.cmd verify
skills\autoskill\run-windows.cmd doctor --ui
skills\autoskill\run-windows.cmd dashboard
```

核心命令用 `run-windows.cmd qa <命令> <项目目录>`。完整包中有Python、Node、Python库、测试浏览器、Agent Browser和DSH SDK；GitHub源码ZIP不含这些二进制，不能代替完整包。安装与模型配置见 [plugin/INSTALL.md](plugin/INSTALL.md)。

先生成用例、save-cases保存、validate与coverage校验，再export/report；实际执行必须配置授权origin。脚本模式无需模型。AI只能使用显式配置的内网provider/model，缺配置不回退默认公网模型。

## 导入 Skill

每个顶层 `skills/<名称>/SKILL.md` 是独立入口，按使用平台的技能加载机制加载。autoSkill 会主动读取其内部子模块，不能依赖加载器自动发现嵌套目录。不保证所有客户端可直接用同一条安装命令。

## 已知限制

- 主引擎当前写入1.1.0，同时接受1.0.0旧定义；已支持源码/官方文档确认来源及目录附加断言。旧的执行结果缺少内容哈希时必须归档重跑，不能冒认通过。
- 已修复初测6项问题，并新增9项针对性回归；内网补充后累计49项Python与5项Node测试通过（含后续插件与调度测试）。历史问题与修复说明见 reports/qa-master-test-report.md。
- 引擎测试通过不等于真实浏览器全流程通过；完整UI验收状态见执行报告。
- Selenium 真实浏览器本轮不可达，UI全部 Blocked；4条第三方依赖接口真实执行通过。具体证据与范围见 reports/selenium-execution-summary.md。
- 仅使用专用测试环境与合成数据；不要提交账号密码、Cookie、Token、原始 HAR 或其他秘密。

Windows实机启动、断网抓包及DSH模型联调尚未完成，不将自动检查通过等同于这些验收通过。

## 双执行通道

共享runtime提供script/UI（Playwright）、script/API（HTTP）与ai（官方DSH SDK）通道。脚本模式不调用模型；自然语言用例需先形成extensions.automation动作和断言才可脚本执行。官方Agent Browser发现Skill已随仓库附带（Apache-2.0），Windows完整包的CLI和浏览器依赖已放在Skill内部。AI SDK需匹配用户DSH安装版本和模型配置。

## 插件开发与预览

```bash
npm run build
npm test
npm run doctor
npm run dashboard
```

DSH插件的宿主入口为plugin/index.mjs，客户端入口构建为lib/client.js。实际安装使用官方Plugins管理器的本地file: Bundle方式；请先阅读安装说明和兼容边界。插件面板当前限定同机访问回环后台，远程Web部署还需认证传输桥。

## 可脚本复跑的开源待办示例

`examples/todomvc/` 附带未经业务修改、逐文件核对Git blob的官方TodoMVC、20条UI动作/断言和4条本地控制台API用例，以及两个项目样例Skill。详情见 [示例说明](examples/todomvc/README.md)。测试结果诚实保留当前浏览器受阻状态，不把生成与格式校验当成真实页面通过。

本轮功能与验证边界详见 [最终验证报告](reports/final-validation.md)。当前已实现双形式与双执行通道，但真实DSH宿主/模型和浏览器端到端验收仍未完成，不能据自动测试通过宣称所有UI流程已通过。

## Windows x64 内网交付

内网用户使用依赖已内置的Windows完整包，不运行联网初始化命令。入口为 `skills/autoskill/run-windows.cmd`，支持本地校验、依赖检查、核心命令与仪表盘。完整包与源码ZIP不同；具体版本和未完成的Windows实机验收见 `reports/windows-bundle-components.json`，使用步骤见 `plugin/INSTALL.md`。运行入口不下载依赖或在线更新，AI无默认公网模型回退。
