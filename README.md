# autoSkill

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

UI 用例需要宿主真实浏览器能力，按 UI Skill 的20条步骤执行；仓库没有内置浏览器运行时，也没有安装 Vercel 官方 Agent Browser。`qa-master/skills/agent-browser/` 是适配说明，不是官方工具本体。

## 导入 Skill

每个顶层 `skills/<名称>/SKILL.md` 是独立入口，按使用平台的技能加载机制加载。autoSkill 会主动读取其内部子模块，不能依赖加载器自动发现嵌套目录。不保证所有客户端可直接用同一条安装命令。

## 已知限制

- 原 autoSkill 数据格式为1.0.0；Selenium 示例为1.1.0，新增准确的源码/官方文档来源枚举。不得直接把示例接口 JSON 输入只接受1.0.0的旧引擎；使用示例专用执行器，或先显式升级转换器。
- 已修复初测6项问题，并新增9项针对性回归；总计20项引擎测试通过。历史问题与修复说明见 reports/qa-master-test-report.md。
- 引擎测试通过不等于真实浏览器全流程通过；完整UI验收状态见执行报告。
- Selenium 真实浏览器本轮不可达，UI全部 Blocked；4条第三方依赖接口真实执行通过。具体证据与范围见 reports/selenium-execution-summary.md。
- 仅使用专用测试环境与合成数据；不要提交账号密码、Cookie、Token、原始 HAR 或其他秘密。

代码和报告保留当前状态，本次提交没有把已知缺陷标记为已修复。
