# Python 代码审查与验证

所有代码查看、查找、规则检查及验证调度均可从仓库中的 Python 入口复用。不需要当前机器的固定路径，不安装依赖，不读取默认用户数据目录，也不调用公网业务。Python 3.12 及已有检查依赖须事先可用；浏览器或 SDK 验收仍须显式选择对应测试和已有运行时。

## 查看与查找

```bash
python scripts/code_review.py --format text files --glob "plugin/*.mjs"
python scripts/code_review.py --format text view plugin/host-service.mjs --start 86 --end 118
python scripts/code_review.py --format text search "readRun" --glob "plugin/*.mjs"
python scripts/code_review.py check --rule repository
```

默认输出 JSON；`--format text` 只让文件、代码和搜索结果便于阅读。`--root <仓库目录>` 可检查另一份 checkout；不允许通过 `view` 的绝对路径、`..` 或符号链接越出所选目录。`files` 优先采用 Git 的已管理和未忽略文件清单；无 Git 时遍历目录，跳过运行时、依赖和本地数据目录。文件清单可包含报告名称，搜索默认排除 `reports`、`.env*` 及运行时/本地数据。仅按需要显式查看源码，勿用它读取真实认证文件或原始网络证据。

搜索由 Python 优先调用本机 `rg`；没有 `rg` 时采用 Python 正则。两种实现都按路径和行号输出，并保留行末空格；通用正则语法可跨机器复用，rg 特有语法在回退时会明确报错。`--glob` 可重复，`--limit` 只限制显示数量，`total` 保留匹配总行数。无匹配返回空列表和退出码 0；错误正则、文件或参数返回 JSON 错误和退出码 2。

## 规则接口

`scripts/review_rules/<名称>.py` 提供 `run(root: Path) -> list[dict]`，每个 finding 包含：

```json
{"rule":"repository","severity":"error","path":"package.json","line":1,"message":"发布版本不一致"}
```

`severity` 为 `error`、`warning` 或 `info`，`line` 为正整数。`check --rule <名称>` 可重复指定；省略时运行现有规则。检查使用结构、语义或隔离的本地契约，不以“源码包含某段字面量”替代行为验证。仓库规则检查本地 Python/ES 模块依赖、发布版本、包导出、CI 测试引用和依赖版本锁定。它不代替 Ruff、Biome、领域回归或真实设备验收。

没有 error finding 时退出 0；发现 error 时退出 1；规则不存在、返回值错误、导入或执行异常时退出 2，并在 `rule_errors` 记录原因。一个规则失败后仍收集其他规则结果。规则必须保持只读或使用私有临时数据；不要访问真实用户数据，不要修改业务预期、联网安装或把大套验收隐含在一次静态检查中。

## 显式验证范围

```bash
python scripts/validate_review.py --list
python scripts/validate_review.py --suite lint --suite conventions --suite review-tools --biome <已有biome可执行文件> --output-dir <新日志目录>
python scripts/validate_review.py --suite workflow --suite knowledge --suite host --output-dir <新日志目录>
python scripts/validate_review.py --python-test test_api_execution_inputs.py --node-test tests/host-backend.test.mjs --output-dir <新日志目录>
```

| 范围 | 实际检查 |
| --- | --- |
| `lint` | 现有 Ruff `check .` 与 Biome `lint`，可分别选择 `ruff` / `biome` |
| `conventions` | 项目约定测试 |
| `workflow` | 工作流、CLI 工作流、host context、原生证据契约 |
| `knowledge` | 失败上下文、知识条目、工作流、迁移、传输契约 |
| `host` | 五组 Node 插件/adapter/backend/commands/state summary 契约 |
| `review-tools` | 审查入口、规则及验证调度器自身回归 |
| `review-rules` | 六个审查域当前可用规则 |

`--python-test` 接受 `tests/` 下的文件名或文件名通配符，`--node-test` 接受仓库内现有文件路径；均可重复。缺失或空 Python 测试选择必须失败。默认不会执行整仓测试；变更稳定后选一次所需范围，再按失败或后续改动补测。`--dry-run` 只列出命令，`--fail-on-skip` 用于必须实际执行的验收，任何跳过都会令整体失败；摘要仍保留子进程原退出码和跳过数量。`--python` / `--ruff` / `--biome` / `--node` 可指定现有解释器或工具路径。

每次运行使用新的 `--output-dir`，其中保留各项 `.log`、工具版本输出和 `summary.json`，目录可在仓库外。已有非空输出目录不会覆盖。临时文件、autoSkill 工作区和用例表被定向到此目录；保留显式浏览器运行时、`AUTOSKILL_DSH_SDK_ROOT` 及 `AUTOSKILL_REVIEW_*` 验收开关，清除其他 autoSkill 会话/业务环境继承。Node 契约的 Python 子进程使用本次选择的解释器。

默认 stdout 仍只输出最终 JSON。显式添加 `--show-logs` 时，每项检查结束后将已保存的完整日志写到 stdout，包括成功、失败及版本探测提前失败的日志，最后再输出相同的 JSON 摘要；原始失败退出码和 `--fail-on-skip` 判定不变。需要程序读取结果时使用默认模式或 `summary.json`。

每个子检查通过列表参数和 `shell=False` 启动。执行结束后继续收集其余检查，逐项保留原始退出码；总退出码取首个失败，信号退出转换为 `128 + 信号号`。缺少工具为 127，超时为 124；`--timeout` 控制单项秒数。POSIX 超时会终止该项进程组，防止遗留测试服务。工具不格式化源码，也不放宽现有 lint 配置。

CI 使用相同入口，并预置 Ruff 0.17.4 / Biome 2.6.7。选定 Python 契约依赖取自项目现有版本锁定；CI 显式启用 `--show-logs`，完整日志与最终摘要保留在 Actions 步骤日志中，不依赖 artifact 存储额度。Linux x64 发布浏览器门禁仍使用刚构建的运行时，要求真实执行且不允许跳过；普通 host 契约不能代替真实 DSH SDK 或宿主文件卡验收。
