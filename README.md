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

## 本地使用

要求 Python 3.10+。在隔离环境安装依赖：

```bash
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# Windows PowerShell 改用 .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py' -v
```

初始化新项目（目标目录须不存在）：

```bash
python skills/autoskill/scripts/qa.py doctor
python skills/autoskill/scripts/qa.py init qa-workspace/demo
```

填入需求后使用 save-cases 保存完整用例目录（自动递增修订并生成项目UI/API用例Skill），再执行 validate、coverage、export、report；执行 API 前必须配置授权 origin 与执行授权。命令与数据契约见主 Skill 及其 references/。

重新执行示例的公开只读依赖接口：

```bash
python skills/selenium-api-cases/scripts/run_api.py qa-workspace/selenium-api
```

UI 用例需要宿主真实浏览器能力，按 UI Skill 的20条步骤执行；浏览器运行时需单独安装；仓库附带官方Agent Browser Skill（含许可证），npm可选依赖提供官方CLI。`skills/autoskill/skills/agent-browser/` 保留测试适配契约。

## 导入 Skill

每个顶层 `skills/<名称>/SKILL.md` 是独立入口，按使用平台的技能加载机制加载。autoSkill 会主动读取其内部子模块，不能依赖加载器自动发现嵌套目录。不保证所有客户端可直接用同一条安装命令。

## 已知限制

- 主引擎当前写入1.1.0，同时接受1.0.0旧定义；已支持源码/官方文档确认来源及目录附加断言。旧的执行结果缺少内容哈希时必须归档重跑，不能冒认通过。
- 已修复初测6项问题，并新增9项针对性回归；总计20项引擎测试通过。历史问题与修复说明见 reports/qa-master-test-report.md。
- 引擎测试通过不等于真实浏览器全流程通过；完整UI验收状态见执行报告。
- Selenium 真实浏览器本轮不可达，UI全部 Blocked；4条第三方依赖接口真实执行通过。具体证据与范围见 reports/selenium-execution-summary.md。
- 仅使用专用测试环境与合成数据；不要提交账号密码、Cookie、Token、原始 HAR 或其他秘密。

代码和报告保留当前状态，本次提交没有把已知缺陷标记为已修复。

## 双执行通道

共享runtime提供script/UI（Playwright）、script/API（HTTP）与ai（官方DSH SDK）通道。脚本模式不调用模型；自然语言用例需先形成extensions.automation动作和断言才可脚本执行。官方Agent Browser发现Skill已随仓库附带（Apache-2.0），CLI和浏览器运行时需单独安装。AI SDK需匹配用户DSH安装版本和模型配置。

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
