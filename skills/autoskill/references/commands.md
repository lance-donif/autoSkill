# autoSkill 命令参考

所有命令输出一个 JSON 对象；`"error"` 表示没有成功，文字说明原因和下一步。任何失败（包括没有预料到的内部错误）都以这种形式输出，详细信息另写到标准错误。退出码：0 成功，1 有用例失败，2 阻塞、命令没有成功或用法错误，3 找不到随包运行时。

## 项目

| 命令 | 说明 |
| --- | --- |
| `project create --name 名称 --url 地址 [--category 分类] [--id 标识]` | 新建项目。地址用于确定起始页面；不限制后续访问站点；标识默认由名称或主机名生成 |
| `project list` | 全部项目及用例数、缺少的凭据 |
| `project show <项目>` | 单个项目的设置 |
| `project update <项目> [--name] [--category] [--timeout 秒] [--page-load 秒] [--insecure-tls on\|off]` | 修改设置 |
| `project update <项目> --audio-input <16-bit-PCM.wav\|off> [--assertion-timeout 秒] [--conversation-timeout 秒]` | WAV 复制到项目并作为测试麦克风输入；off 清除；长断言/流式对话可设置独立等待预算 |
| `plan write <项目> --file 用例.json` / `plan mark <编号> 通过\|失败\|阻塞\|未执行 [--note]` / `plan sync` | 用例表（Excel，位于同级的 `autoskill-cases` 用例 Skill）：生成、记录结果、把回放结果写回；文件夹或 Excel 可整体替换 |
| `plan show [--query 关键词] [--module 模块] [--status 结果] [--offset 0] [--limit 30]` | 默认只返回 30 条摘要和总数、下一页偏移；上限 100 条 |
| `plan show --case <编号> --details` | 选中后展开步骤/预期；不把几千条正文塞进上下文 |
| `context <项目> [--query 关键词] [--module 模块] [--offset 0] [--limit 30]` | 登录方式、有限的用例与知识片段、总数/分页、建议的下一步 |
| `cases <项目> [--query 关键词] [--module 模块] [--kind ui\|api] [--offset 0] [--limit 30]` | 分页查用例摘要（最多 100 条）；`--case <编号>` 展开单条完整定义 |

## 能力查询

`capabilities [命令或能力组] [--group 组名]`、`browser help [命令或能力组]`、`browser <命令> --help` 返回 JSON；不需要进行中的录制、后台服务、可写工作区或浏览器二进制。`api capabilities` 查询协议合同，也不执行请求。

例如 `capabilities --group network`、`capabilities dialog accept`、`browser help settings`。不带参数会列出完整组名和计数，覆盖 agent-browser 0.42 的导航、输入、快照、定位、等待、键鼠、标签页/框架/弹窗、文件、剪贴板、设置、cookies/storage、网络/HAR、flows、认证/插件、状态、流媒体/WebMCP、诊断/审计/React、MCP 与管理能力。

| 字段 | 含义 |
| --- | --- |
| `executable` | 有执行入口；仍需满足 `dependencies` 与 `prerequisites`。MCP stdio 描述符的 `executable=false` 表示只返回客户端配置 |
| `recordable` / `recording` | 可移植步骤 `portable`；明确阻塞的原生步骤 `native_only`；只看证据 `observation`；独立管理 `administration`；项目启动配置 `configuration`（自身不是业务步骤） |
| `replayable.playwright` / `.selenium` | 两种引擎分别声明的回放支持 |
| `exportable.playwright` / `.selenium` | 独立 Python 脚本的支持；不代表 Postman 支持页面动作 |
| `verified` | 查询不执行验证，固定为 false；实际测试证据应另行报告，不能把能力声明写成已通过 |

支持限于 `syntax` 和 `limitations` 的语法；不能从一个命令推断它的全部上游选项都可移植。

## 项目知识

| 命令 | 说明 |
| --- | --- |
| `knowledge list <项目> [关键词] [--offset 0] [--limit 20]` / `knowledge search <项目> <关键词> [--offset] [--limit]` | 返回文件名、标题、短片段和来源/证据引用；不返回所有正文，上限 100 条 |
| `knowledge show <项目> <文件名> [--offset 0] [--limit 4000]` | 按字符分页读选中的正文，单次上限 16000；只接受项目 knowledge 目录内的文件 |
| `knowledge add <项目> --title 标题 (--content 正文\|--file UTF8文件) [--name 新文件.md] [--source 来源]… [--case 编号]… [--run 运行编号]…` | 显式积累确认过的事实；至少有来源、用例或运行引用之一；同义入口 `knowledge record` |

新增知识写入可分享的 Markdown，保留来源、用例编号和当时运行状态；引用必须属于同一项目。已有 `.md/.txt/.json/.csv` 仍可检索。失败、页面内容与接口响应不会自动写成规则；不要保存凭据。来源可以是需求文档位置、网址或「用户确认」；无来源的推测不应积累。默认 `context` 只取最多 5 条、约 3000 字符预算的知识片段，先搜索再取正文。

单条新增事实正文最多 65536 字符。旧文档的索引读取同样有此预算，超出时标记 `truncated`；选中的旧文档仍可通过 `knowledge show` 按字符翻页读到末尾。长需求文档适合作为来源，确认后提取相应事实。

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

同一时间只能有一个录制。

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

下列文档语法记录为可移植步骤，两个引擎与独立脚本都有对应实现；查询能力表不会验证这些实现在当前机器/站点是否成功。

| 类别 | 语法 |
| --- | --- |
| 导航 | `open <地址或项目路径>`（别名 goto/navigate）、`reload`、`back`、`forward` |
| 元素 | `click <目标>`、`dblclick <目标>`、`hover <目标>`、`focus <目标>`、`fill <目标> <文字>`、`type <目标> <文字>`（不清空）、`select <目标> <单个value>`、`check <目标>`、`uncheck <目标>` |
| 滚动/拖动 | `scroll <方向> [像素] [--selector <目标>]`、`scrollintoview <目标>`（别名 scrollinto）、`drag <起点> <终点>` |
| 按键/键盘 | `press <按键>`（别名 key）、`keydown <键>`、`keyup <键>`、`keyboard <type\|inserttext\|press\|down\|up> <值>` |
| 鼠标 | `mouse move <x> <y>`、`mouse down [left\|middle\|right]`、`mouse up [按钮]`、`mouse wheel [dy] [dx]`；坐标/滚轮支持有限小数 |
| 框架 | `frame <CSS或@引用>` 选择当前框架里的子框架，`frame main` 重置；框架名字/URL 选择属于原生命令 |
| 弹窗 | 触发前 `dialog accept [文字]` 或 `dialog dismiss`，再执行触发动作；`dialog status` 仅查看；已打开弹窗的恢复会写 native_step |
| 等待 | `wait <毫秒>`、`wait <目标> [--state visible\|hidden\|attached\|detached] [--timeout 毫秒] [--stable-for 毫秒]`；其他 wait 模式为原生步骤 |
| 文件 | `upload <目标> <文件>…`、`download <目标> <相对输出名>`；上传自动存进项目，回放/导出按项目资源解析，下载不把机器路径写入用例 |
| 脚本 | `eval <表达式或函数>`、`eval -b <BASE64>`、`eval --script-file <文件>`；语句块请包装成函数；脚本以本机 ENV 值引用保存，分享时需补齐 |
| 剪贴板 | `clipboard write <文字>`；需要浏览器权限/上下文，不保证操作系统剪贴板等价 |
| 验证码 | `captcha <图片目标> <输入目标>`；本机模型只识别字母数字图，独立导出脚本需人工输入验证码 |
| 设置 | `set viewport <宽> <高> [缩放]`、`set offline on\|off`、`set headers <字符串键值JSON>`、`set geo <纬度> <经度>`、`set media dark\|light\|no-preference [reduced-motion\|no-preference]` |
| 浏览器数据 | `storage local\|session set <键> <值>`、`storage local\|session clear`、`cookies clear`、`cookies set <名> <值> [--http-only] [--secure] [--same-site Strict\|Lax\|None] [--domain 域或--url 地址] [--path 路径] [--expires 秒]` |

只查看、不生成业务步骤：`describe <目标>`（候选定位，含 testid）、`snapshot` / `snapshot --all`、`get text|html|value|url|title|count|box|styles|cdp-url`、`get attr <目标> <属性>`、`is visible|enabled|checked <目标>`、`clipboard read`。读取不是断言。

目标：`@e编号`（来自最近一次 snapshot）或 CSS 选择器。按键名如 `Enter` `Escape` `Tab` `Control+a`。

### 原生诊断、证据和非可移植操作

`browser` 自动接入 agent-browser 原生命令，不需要另加 native 前缀。没有录制时也能执行，且不生成业务用例；有录制时可移植语法走上面的合同，其他改变页面/状态的操作保存为明确不可回放的 `native_step`。`run`/`export scripts` 会阻塞并报告原因；不会略掉该步骤后算通过。多选 select、human 鼠标路径等额外选项也不能从基础命令的支持推断为可移植。

| 命令组 | 例子与边界 |
| --- | --- |
| 证据 | `screenshot [相对文件名] [--full]`、`pdf [文件名]`、`record start [文件.webm]` / `record stop`（视频需 PATH 中的 ffmpeg）、`trace start` / `trace stop [文件.json]`、`profiler start` / `profiler stop [文件.json]`；trace 与 profiler 不能同时持有 CDP Tracing |
| 诊断 | `console [--clear]`、`errors [--clear]`、`highlight <目标>`、`inspect`、`read [URL]`、`a11y [选项]`、`vitals [选项]`、`react tree\|inspect\|renders\|suspense`；React hook 需启动时启用，URL 形式的 a11y/vitals 导航是原生步骤 |
| 网络 | `network requests` / `network request <id>` 仅查看；`network route` / `unroute` 是非可移植修改；HAR start/stop 不能接管进行中的业务录制 |
| 状态/插件 | `set device` / `set credentials`、cookie --curl 导入、`state load`、`auth`、`plugin`、`flow`、`webmcp invoke`、`stream enable`/`disable` 等修改不是可移植业务步骤；list/show/status/result 等读取按能力表区分 |
| 管理 | `browser doctor --offline --quick`、`session list`、`profiles`、`skills`、原生 dashboard；有录制时 doctor/skills/dashboard 运行于独立管理会话。`autoskill dashboard` 是 autoSkill 仪表盘 |
| MCP | `browser mcp` 只返回 `executed=false` 的 stdio 客户端配置；`browser mcp call <工具> <JSON>` 是单次调用，进行中的录制与调用会话隔离 |

录制持有浏览器/会话配置、标签页/框架与 HAR。`close`/`connect`、upgrade、doctor --fix、启动/身份/config/proxy/engine 等变更先结束或取消录制；原生 batch 的内部命令也遵循这些前提。一般工作不要执行 install/upgrade 下载依赖；缺失时返回事实，不要安装。完整能力与前提用 `capabilities` 查，不要用旧的「一律不可用」列表推断。

截图/PDF/视频/trace/profiler/下载/状态等证据进入 `.local` 私有目录，返回相对路径与哈希；不把运行态/机器路径带进用例。state save 在本机以明文 JSON 保存并设置私有文件权限；兼容读取旧的 .enc，不把状态/cookie 值带进用例。证据不会自动变成断言或知识。

### 录下来的定位方式

每一步保存多个候选定位，回放时按顺序尝试：测试标识（`data-testid`）→ 标签文字 → 占位提示 → 角色+名称 → CSS。首选失效而备选命中时用例仍通过，结果里标注「定位已降级」，提示页面改了。

### check 类型

`visible` `hidden` `text` `contains` `value` `checked` `count` `min_count` `attribute`（需 `--attribute`）需要目标；`url` `url_contains` `title` 不需要。目标可写 `@e5`、`testid=…`、`text=…`、`label=…`、`placeholder=…`、`role=角色:名称` 或 CSS 选择器；`count`/`min_count` 的目标用 CSS 选择器。

`check ... --timeout <秒> --stable-for <秒>` 为长回答设置本条等待预算与连续稳定时间（单位为秒，和 browser wait 的毫秒不同）。稳定性只能证明所写断言持续成立；应断言业务的「完成」标志，避免把尚在流式更新的局部文字当最终答案。

## 直接接口与流式/语音对话

`api capabilities` 查看当前协议支持、依赖与规范位置。`api request <项目> [--method GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS] [--path /接口] [--transport http|sse|websocket] [--file 规范.json] [--save-case 标题]` 执行一次，带 save-case 才保存成项目用例。

常用请求选项：`--headers <JSON>`、`--params <JSON>`、`--json-body <JSON>`、`--form <JSON>`。断言选项：`--expect-status`、`--expect-json <JSON-Pointer映射>`、可重复的 `--expect-text` / `--expect-reasoning`、`--min-events` / `--min-messages` / `--min-audio-frames` / `--min-audio-bytes`、`--max-ms`、`--max-first-event-ms` / `--max-first-text-ms` / `--max-first-audio-ms`。等待预算：`--timeout-seconds`、`--idle-timeout-seconds`、`--first-event-timeout-seconds`。

SSE/双向 WebSocket 的具体会话顺序、结束事件、reasoning/text/audio 提取与音频输入输出写在 JSON 的 `transport`（保存后为 `extensions.transport`）；WebSocket 依赖随包 `websocket-client`。`--audio-input <文件>` 可重复添加发送音频步骤，文件会复制到项目。音频的字节/帧断言证明传输数据存在；识别内容、音质等业务要求应写可验证的文本/协议断言，不能从帧数推断。

SSE 默认等待 `data: [DONE]` 结束；服务使用其他结束标志时显式设置 `transport.until`（字符串或匹配字段的对象）。流提前断开、只收到部分答案、达到总/首事件/空闲预算都会失败。WebSocket 用有序 `steps` 指明 send/send_audio/receive/close；receive 可指定 until、min_messages 与 expect_json，未满足条件就断开不算完成。

浏览器语音输入用项目 `--audio-input` 设置测试 WAV（非空 16-bit PCM，单/双声道，8–192 kHz）；协议接口音频用 api request 的发送步骤。项目 `--assertion-timeout`/`--conversation-timeout` 与每条等待/断言预算允许有限正数，不为 AI 长响应强行套用普通短等待。

两类声音的本地合成数据可验证麦克风文件进入页面、PCM16 分块发送、音频回包/证据和对话结束条件；真实外部 ASR 识别质量、特定语音服务与 DSH 宿主的端到端结果必须另报实际验证，未执行不能写成通过。

麦克风采集与 WebRTC 对端通信应分开验证：宿主的 ICE/UDP/网络策略可能允许文件麦克风与 WebAudio 分析，但阻塞对端 RTP 传输。这种情况报告通信阻塞；不能把成功读取 WAV 算作 WebRTC 或 ASR 已通过。

## 回放

`run <项目> [--case 编号]… [--engine playwright|selenium] [--kind ui|api]`

- 浏览器默认显示最大化的窗口（没有窗口时页面为 1920×1080）；加 `--headless` 在后台运行（`record start`、`run`、`login`、`dashboard` 都支持，也可设置环境变量 `AUTOSKILL_HEADED=0`）。
- 前置用例自动先执行且每次运行只执行一次；前置没通过时，依赖它的用例记为阻塞。
- 登录用例：有保存的登录态且仍有效就直接复用，否则重新登录；回放中途发现登录过期会重新登录一次，只读用例自动重试，写操作不自动重发。
- 没有前置的接口用例不带任何登录信息发送（用来验证「未登录被拒绝」）。
- 接口用例之间的变量：`{{名称}}` 由前面用例从响应里提取。

失败归类：找不到元素、结果与预期不符、超时、缺少凭据、前置用例未通过、登录已过期、缺少前置数据、不可移植步骤或环境问题；不再用站点授权白名单限制请求。

## 导出与分享

| 命令 | 说明 |
| --- | --- |
| `export postman <项目> [--out 目录]` | HTTP 接口的 Postman Collection v2.1 + Environment。按依赖顺序排列，含断言和变量传递；凭据是空变量。含 SSE/WebSocket 时明确拒绝导出，不伪装成普通 HTTP 集合 |
| `export scripts <项目> [--out 目录]` | 每条页面用例各一份 Playwright 和 Selenium 独立 Python 脚本，包含纯页面前置链与项目资源，运行时不需要 autoSkill；凭据从同名环境变量读取。native_step 或接口前置链会明确阻止导出；文字验证码需人工输入 |
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
```

环境变量 `AUTOSKILL_WORKSPACE` 可以把工作区放到别处（仪表盘和命令行要用同一个值）。

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
