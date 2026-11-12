> 独立测试模型：gpt-6-luna；初始会话不继承主代理上下文。测试代码提交：b85f4a2bddc8c8a38498ca493991cbd0db9ead57；另检查实际Windows离线包。以下保留独立测试结论，仅校正日期与仓库链接。

# autoSkill 完成交付独立验收报告

日期：2026-11-12（北京时间）  
评审材料：指定源码副本 `/workspace/scratch/8aaa49456f82/luna-review-b85f4a2`、Windows x64 离线包 `/workspace/scratch/8aaa49456f82/autoSkill-Windows-x64-Offline.zip` 与其解压目录 `/workspace/scratch/8aaa49456f82/autoSkill-windows-x64-release`。未读取其他项目副本或既有评审报告来形成结论。

## 结论

**部分验收通过，不能作为 Windows/DSH 最终验收通过。** 交付文件完整性、随包依赖清单、脚本项目初始化/校验/覆盖/导出路径有本地可复现证据；所提供 UI 样例在当前环境实际尝试后为 Blocked。Windows 启动器、Windows 原生依赖、真实 DSH 插件宿主、AI 模型调用和定时任务调度没有在目标 Windows x64 内网环境验证。当前未发现 ZIP 与解压目录或共同源码文件之间的内容差异。

## 已完成检查及证据

1. **ZIP 与解压目录完全一致。** 对 ZIP 全部 3603 个文件逐个计算 SHA-256 并与解压目录比较：3603 个路径相同、0 个 ZIP-only/目录-only 路径、0 个哈希差异。源码副本 115 个文件中，112 个与交付物存在同路径文件且内容哈希一致；共同文件 0 个差异。其余三个源码副本文件（`.gitignore`、`.npmignore`、`reports/windows-offline-package.json`）没有打包，这是打包内容差异，不是共同源文件内容漂移。
2. **离线依赖清单校验。** 按交付物 `skills/autoskill/vendor/windows-x64/runtime-manifest.json` 的 3474 条记录逐项检查文件存在性、路径不越界和 SHA-256：3474 项全匹配。`vendor/windows-x64/app` 中 126 个嵌入运行文件与交付根目录对应文件全部一致。交付报告记录的 ZIP SHA-256 为 `a604478fe20e186e49bd0bd745a8d5ae640ec285b7f714cfdb2c20226b5e5197`；此处另做了逐项校验，没有在 Windows 上运行 `verify` 命令本身。
3. **脚本项目生命周期。** 使用交付包中的 `skills/autoskill/scripts/qa.py`（当前 Linux Python 3.12.14，仅用于检查随包 Python 逻辑）初始化空项目，`validate` 通过；`coverage` 对空项目正确返回 2（无需求可覆盖）。用包内 `scripts/seed-demo.py --example todomvc` 导入随包样例，得到 24 条用例；随后的 `validate` 通过，`coverage` 报 24/24 需求关联（100%）。这里验证的是结构与关联，不代表用例生成质量或测试通过。
4. **导出与报告。** 对已导入的 TodoMVC 项目执行 `export`、`report` 均返回 0，并实际生成 `功能测试用例.xlsx`、`接口测试用例.xlsx`、`traceability-matrix.xlsx`、`测试执行报告.md`。
5. **授权门禁。** 对默认 `execution_authorized=false` 的样例执行 `qa.py run-api`，结果为 `{"Blocked": 4}`、退出码 2。此项证明默认未授权时阻止执行；没有向任何 API 发送请求，因此 API 网络执行能力仍未验收。
6. **脚本 UI 实际尝试。** 对随包 TodoMVC 样例 `TC-TODO-UI-001`，在隔离测试项目中仅配置授权的 localhost origin 和合成样例风险范围后调用真实 `runtime.engines.execute(..., 'ui', 'script', ...)`。结果为 `Blocked`，原因 `浏览器启动失败: Error`，没有 UI 断言或页面证据。当前系统 Playwright 对应的 Chromium/headless-shell 可执行文件不存在；没有下载或安装浏览器。此结果是平台依赖受阻，不用模拟页面代替验收。
7. **AI worker 配置门禁。** 在 `AUTOSKILL_DSH_COMMAND`、`AUTOSKILL_AI_PROVIDER`、`AUTOSKILL_AI_MODEL` 未设置时实际调用随包 `plugin/ai-worker.mjs`，退出码 2、输出 `AI execution blocked: Error`。DSH SDK 包本身存在于交付物，但当前环境没有 DSH JSON-RPC 宿主、内网 provider/model 或 DSH SDK 连接配置，因此没有执行 AI 生成、AI 回放或 AI 解释。worker 的显式配置要求见源码 [ai-worker.mjs](../../plugin/ai-worker.mjs#L14)；错误输出只暴露错误类型，用户无法从这条日志确认是哪项配置缺失。

## 未验证项与限制

- 本环境为 Linux x86_64，不是用户目标 Windows。`run-windows.cmd` 第 8 行启动内置 Windows Python，后续由 `scripts/windows_entry.py` 选择 Node、doctor/dashboard 或 QA 入口；这些 `.cmd`/`.exe` 启动步骤未能实机运行。当前没有 Wine，也没有 Windows 执行器。路径静态检查显示 `run-windows.cmd` 指向的 `skills/autoskill/scripts/windows_entry.py` 和入口依赖在交付目录中存在，尚不能据此声称 Windows 启动成功。
- `doctor` 在当前宿主报告系统 Python `3.12.14`、`httpx/jsonschema/openpyxl` 可用、`agent_browser_cli=false`；它不是 Windows 随包 Python/Node/浏览器的运行结果。内置浏览器即使文件校验通过，也未能证明启动。
- 没有对真实业务内网 origin 执行请求；API 样例中有 DSH 本地控制台 API，也没有对它们发请求。当前环境对回环绑定仅开放特定测试端口，尝试启动本地后台未形成可连通 listener，故未以伪造响应或 Mock 代替。
- DSH 插件 `plugin/index.mjs` 包含本地后端拉起、Skill 注册、回环后台健康探测；`runtime/service.py` 定义了项目生成、运行、重放、任务 API 和调度器。上述是代码证据，真实宿主加载、dashboard 页面、定时任务触发与 AI 回放均未在 DSH 中验收。
- 不能据静态配置得出整个宿主 DSH 零外联。`plugin/INSTALL.md` 已要求管理员单独禁用宿主更新和遥测，并在 Windows 做网络流量验收；本轮未做断网、抓包或进程外联观测。

## 可复现问题/状态

| 状态 | 位置 | 预期 | 实际与证据 |
| --- | --- | --- | --- |
| Windows 平台阻塞，未判为源码缺陷 | `skills/autoskill/run-windows.cmd:3-9`；`skills/autoskill/scripts/windows_entry.py:41-65` | 在 Windows x64 上 `verify`、`doctor`、`qa`、`dashboard` 均从包内依赖启动 | 当前 Linux 没有 Windows 解释器，无法实际启动。依赖路径与入口文件存在，不能替代实机结果。 |
| 脚本 UI 阻塞 | `runtime/engines.py:53-66` | 本地 Chromium 可启动后执行真实动作、断言并留证 | `TC-TODO-UI-001` 实际尝试返回 Blocked；系统 Chromium executable 不存在，无网络安装。具体结果见测试步骤 6。 |
| API 实际执行未验收 | `skills/autoskill/scripts/qa.py` 中 `run-api`；样例 `examples/todomvc/api-cases.json` | 获授权后对本地或内网 origin 请求并核对断言 | 未授权默认安全门禁返回四条 Blocked；未发送请求。不能将此计为 API Pass。 |
| AI/DSH 实际联调未验收 | `plugin/ai-worker.mjs:14-19`；`runtime/engines.py:126-145` | 显式配置的本地 DSH runtime 和内网模型完成生成/回放并保存完整结果 | 当前缺少宿主/模型配置；worker 以退出码 2 阻断。无真实模型调用。 |

没有将测试限制升级成虚构的产品缺陷，也没有用 mock、安装依赖或公网访问补齐证据。下一步验收需在目标 Windows x64 内网机器执行安装说明中的 `verify`、`doctor --ui`、脚本 API/UI样例与 DSH 插件/模型回放，并保留实际结果与网络观测。
