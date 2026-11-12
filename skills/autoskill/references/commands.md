# autoSkill 命令参考

所有命令输出一个 JSON 对象；`"error"` 表示没有成功，文字说明原因和下一步。任何失败（包括没有预料到的内部错误）都以这种形式输出，详细信息另写到标准错误。退出码：0 成功，1 有用例失败，2 阻塞、命令没有成功或用法错误，3 找不到随包运行时。

## 会话

所有命令都支持全局前缀：`autoskill --session "会话标识" <命令> ...`，也可设置任务进程的 `AUTOSKILL_SESSION`。命令行参数优先；省略两者时使用原来的默认会话。标识允许任意文字，文件和浏览器名称使用其哈希，不把原文当路径。

多个 AI 任务各自使用不同标识，从 `record start`、`browser`、`check` 到 `record stop`、`plan`、`run` 保持一致。各会话拥有独立的录制、浏览器和默认 Excel 用例表；取消一个会话不影响其他会话。同一会话的录制命令顺序执行，不同会话可以并行；同项目的用例保存自动串行，项目和保存后的用例仍然共享。

直接从 Python 调用时，可用 `from autoskill.task_context import use_session` 和 `with use_session("任务甲"):` 选择上下文；`use_session(None)` 明确选择默认会话并屏蔽当前环境变量。后台转交会明确传递调用方身份，未指定身份的旧调用仍使用默认会话。

## 项目

| 命令 | 说明 |
| --- | --- |
| `project create --name 名称 --url 地址 [--category 分类] [--id 标识]` | 新建项目。地址用于确定起始页面；不限制后续访问站点；标识默认由名称或主机名生成 |
| `project list` | 全部项目及用例数、缺少的凭据 |
| `project show <项目>` | 单个项目的设置 |
| `project update <项目> [--name] [--category] [--timeout 秒] [--page-load 秒] [--insecure-tls on\|off]` | 修改设置 |
| `settings [--browser system\|bundled]` | 本机设置：`system` 优先用本机 Chrome、随包浏览器备用（默认），`bundled` 只用随包浏览器；不带参数时显示当前使用的浏览器 |
| `plan write <项目> --file 用例.json` / `plan show` / `plan mark <编号> 通过\|失败\|阻塞\|未执行 [--note]` / `plan sync` | 用例表：生成、读取、记录结果、把回放结果写回；默认会话位于同级 `autoskill-cases`，命名会话拥有独立默认目录，以命令返回的路径为准 |
| `context <项目>` | 登录方式、已保存的登录态、缺少的凭据、用例清单、知识库、建议的下一步 |
| `cases <项目>` | 用例清单 |

## 凭据与登录

| 命令 | 说明 |
| --- | --- |
| `secret set 名称 [值]` | 保存凭据到本机（明文，`workspace/.local/credentials.json`，项目设置里可直接查看和修改）。省略值时从标准输入读一行 |
| `secret list` / `secret delete 名称` | 只列名称，不显示值 |
| `login <项目> [--force]` | 执行登录用例并保存登录态；`--force` 忽略已保存的登录态 |

用例里的凭据写作 `${ENV:名称}`；录制时在命令行写 `secret:名称`。没有保存时回退到同名环境变量。

## 录制

| 命令 | 说明 |
| --- | --- |
| `record start <项目> [--as-login] [--no-login] [--url 地址]` | 打开浏览器并开始录制。有登录用例时先自动登录；`--as-login` 表示录的就是登录；`--no-login` 以未登录状态开始 |
| `browser <命令> …` | 操作页面并记录，见下表 |
| `check <类型> [目标] [期望] [--attribute 属性]` | 当场验证并记录断言 |
| `record status` | 已记录的步骤 |
| `record undo` | 撤销最后一步的记录 |
| `record stop --title 标题 [--module 模块] [--category 分类]` | 结束并生成用例 |
| `record cancel` | 放弃，不生成用例 |

每个会话维护自己的录制；不同会话可以同时录制。

### 标签页与新窗口

| 命令 | 说明 |
| --- | --- |
| `browser tab list`（或 `browser tab`） | 查看标签页的 `id`、`active`、`url`、`title`；列表本身不生成操作步骤 |
| `browser tab t1` | 切到指定标签页并录入步骤，返回该页 snapshot |
| `browser tab new [地址]` | 新建并切换到标签页；默认空白页，支持项目相对地址 |
| `browser tab close [t2]` | 关闭指定页或当前页，并记录关闭后的活动页面；不能关闭最后一页 |

编号是 `t1`、`t2` 这样的录制内稳定标识，关闭后不会复用。链接的 `target="_blank"`、`window.open()`、延迟弹窗和页面自身 `window.close()` 的变化都会记录。读取或断言前发现延迟的新页，也会补记变化；可先 `browser wait 1000` 再 `browser tab list` 确认。

两种回放引擎和独立导出脚本都支持这些步骤。新页丢失或关闭未完成时报错；同时出现多个新页时通过地址匹配，不会猜测无法区分的同地址窗口。新页地址匹配提示不保存内嵌密码、令牌、一次性登录参数或 fragment。更新前录下的跨窗口流程缺少标签页变化，需要重新录制。

### browser 子命令

记录为步骤：`open <地址或路径>` `reload` `click` `dblclick` `hover` `fill <目标> <文字>` `type <目标> <文字>`（逐字输入，不清空）`select <目标> <value>` `check` `uncheck` `press <按键>`。

只查看、不记录：`describe <目标>`（列出该元素可用的定位写法，含 testid）`snapshot`（只列可交互元素）`snapshot --all`（含标题、表格等全部内容） `get text|html|value|url|title|count <目标>` `get attr <目标> <属性>` `is visible|enabled|checked <目标>` `wait <选择器或毫秒>` `screenshot [路径]` `scroll <方向> [像素]`。

目标：`@e编号`（来自最近一次 snapshot）或 CSS 选择器。按键名如 `Enter` `Escape` `Tab` `Control+a`。

不可用：安装、升级、连接外部浏览器、代理、网络与登录态相关命令——这些由 autoskill 统一管理。

### 录下来的定位方式

每一步保存多个候选定位，回放时按顺序尝试：测试标识（`data-testid`）→ 标签文字 → 占位提示 → 角色+名称 → CSS。首选失效而备选命中时用例仍通过，结果里标注「定位已降级」，提示页面改了。

### check 类型

`visible` `hidden` `text` `contains` `value` `checked` `count` `attribute`（需 `--attribute`）需要目标；`url` `url_contains` `title` 不需要。目标可写 `@e5`、`testid=…`、`text=…`、`label=…`、`placeholder=…`、`role=角色:名称` 或 CSS 选择器；`count` 的目标用 CSS 选择器。

## 回放

`run <项目> [--case 编号]… [--engine playwright|selenium] [--kind ui|api]`

- 浏览器默认显示最大化的窗口（没有窗口时页面为 1920×1080）；加 `--headless` 在后台运行（`record start`、`run`、`login`、`dashboard` 都支持，也可设置环境变量 `AUTOSKILL_HEADED=0`）。
- 全局默认同时回放 2 个任务，`AUTOSKILL_MAX_PARALLEL` 可调大；同一项目的回放共享登录态，会自动排队。不同会话录制仍可并行，保存到同一项目时只对写入阶段排队。
- 前置用例自动先执行且每次运行只执行一次；前置没通过时，依赖它的用例记为阻塞。
- 登录用例：有保存的登录态且仍有效就直接复用，否则重新登录；回放中途发现登录过期会重新登录一次，只读用例自动重试，写操作不自动重发。
- 没有前置的接口用例不带任何登录信息发送（用来验证「未登录被拒绝」）。
- 接口用例之间的变量：`{{名称}}` 由前面用例从响应里提取。

失败归类：找不到元素、结果与预期不符、超时、地址未授权、缺少凭据、前置用例未通过、登录已过期、缺少前置数据、操作未被允许、环境问题。

## 导出与分享

| 命令 | 说明 |
| --- | --- |
| `export postman <项目> [--out 目录]` | Postman Collection v2.1 + Environment。按依赖顺序排列，含断言脚本和变量传递；凭据是环境里的空变量 |
| `export scripts <项目> [--out 目录]` | 每条页面用例各一份 Playwright 和 Selenium 的独立 Python 脚本（含前置步骤），运行时不需要 autoSkill；凭据从同名环境变量读取 |
| `report <项目> [--out 目录]` | Excel（用例、步骤、断言、最近结果）和 Markdown 摘要 |
| `pack [项目]… [--out 路径] [--include-secrets --passphrase 口令]` | 分享包：用例、知识库、登录方式定义。不含登录态、运行记录、截图；凭据仅在 `--include-secrets` 时用口令加密带上 |
| `pack --full [--out 路径]` | 完整包：整个 Skill（运行时 + 全部项目与知识库），对方解压即用。不含 `workspace/.local`（凭据、登录态、运行记录、截图） |
| `import <分享包> [--passphrase 口令] [--overwrite]` | 导入轻量分享包。已有同名项目默认跳过 |

## 其他

| 命令 | 说明 |
| --- | --- |
| `dashboard [--port 8787]` | 启动仪表盘（只监听本机）。定时任务在仪表盘运行期间才会触发 |
| `demo` | 安装示例项目「示例工单系统」；启动仪表盘时会同时启动示例站点 |
| `doctor [--launch]` | 检查运行时；`--launch` 真实启动一次浏览器 |
| `verify` | 按清单校验随包运行时文件（完整离线包） |

## 文件放在哪

```
<Skill 目录>/workspace/
  projects/<项目>/        可以分享：project.json、qa-config.json、auth.json、testcases/、knowledge/
  .local/                 只在本机：凭据、登录态、运行记录、截图、报告
    recording.json        默认会话的录制状态（兼容旧版本）
    recordings/<哈希>/    命名会话的录制状态和临时文件
    case-plans/<哈希>/autoskill-cases/  命名会话的默认 Excel 用例 Skill
```

环境变量 `AUTOSKILL_WORKSPACE` 可以把工作区放到别处（仪表盘和命令行要用同一个值）。

`AUTOSKILL_CASE_SKILL` 可以显式指定用例 Skill 目录，优先于默认和命名会话的目录选择；需要不同任务各自使用自己的表时，使用不同目录或保留各会话的默认目录。通过后台服务执行时，调用方显式设置的值会原样选择目录（相对路径按调用方位置解析）；空值恢复该会话的默认目录，未设置则沿用服务配置。

## 等待时间

| 设置 | 默认 | 作用 |
| --- | --- | --- |
| `project update <项目> --page-load 秒` | 90（10–600） | 打开或刷新页面最多等多久，录制和回放都用；「页面与资源可访问」类接口用例也用它 |
| `project update <项目> --timeout 秒` | 20（3–120） | 单步等待元素出现、接口请求的超时 |

录制时页面在等待时间内没有完全加载，会带着提示继续，不算失败；回放时超过则记为「超时」。

## 在受限沙箱里

命令所在的进程写不了数据目录、或写不了用户目录下的 `.agent-browser`（浏览器控制目录）时，autoskill 判定自己在受限沙箱里，把命令转交给本机正在运行的 autoSkill 后台服务（`dashboard` 进程）执行，原样返回它的输出和退出码。

- 后台服务由 DeepSeek Harness 插件启动，或由用户运行 `autoskill dashboard` 启动；它的端口记在 `workspace/.local/service.json`。
- 只转交 autoskill 自己的命令；`dashboard`、`pack --full`、`pack --include-secrets`、以及 `--out` 指向数据目录以外的导出不转交。
- 环境变量 `AUTOSKILL_BRIDGE`：`on` 强制转交（沙箱只拦浏览器、不拦文件时用），`off` 禁止转交；默认自动判断。启动后台服务时设 `AUTOSKILL_BRIDGE_SERVER=off` 可以关闭代为执行。
- 这个接口只监听本机，只接受命令行发来的请求，网页无法调用。
