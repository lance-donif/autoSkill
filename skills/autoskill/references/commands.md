# autoSkill 命令参考

所有命令输出一个 JSON 对象；命令错误用 `"error"` 说明原因，内部错误的详细信息另写到标准错误。业务失败保留正常结果结构：例如 check 返回 `passed:false`、expected/actual，回放返回 Fail/Blocked；不能只检查 error。退出码：0 命令成功（入队或查询不代表业务通过），1 同步用例或 check 失败，2 阻塞、命令错误或用法错误，3 找不到随包运行时。

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
| `plan write <项目> --file 用例.json` / `plan show` / `plan mark <编号> 通过\|失败\|阻塞\|未执行 [--note]` / `plan sync` | 用例表：生成、读取、记录结果、按设计版本、候选和指定运行回填完整表，保留未执行及失败；`initial_excel` 是最初副本。用户交付读取 `delivery_excel` 正式14列文件，`excel` 是兼容的内部11列表。默认表位于数据根的 `.local/case-plans/default/autoskill-cases`，命名会话拥有独立目录，以命令返回的路径为准 |
| `plan link <设计行编号> <可复用用例编号>` | 将当前会话设计行关联到同项目已有用例，不执行、不复用旧通过。先确认操作与独立断言符合本行预期，再保持相同 `--session` 执行 `run <项目> --case <可复用用例编号>`；API 流程可带 `--api-only --inputs <参数文件>`。本次真实结果回填原表，自动前置只回填表内已有明确关联的行，不增加运行报告的显式目标数 |
| `context <项目> [--limit 50] [--cursor 游标] [--knowledge-limit 5] [--knowledge-cursor 游标] [--all]` | 登录、缺少的凭据、用例与知识预览；`page.cases`、`page.knowledge` 分别标明总数、剩余页及内容版本 |
| `cases <项目> [--limit 数量] [--cursor 游标] [--all] [--kind] [--module] [--requirement] [--query]` | 分页用例摘要；`page` 返回 `total/returned/has_more/next_cursor/content_revision`，内容或查询改变会报 `cursor_expired` |
| `cases <项目> --case <编号>` | 完整用例定义及内容版本，包含断言和需求关联；不是仪表盘展示投影 |
| `knowledge list <项目> [--limit 数量] [--cursor 游标] [--query] [--all]` | 知识文件索引、字节数、内容版本；不默认读取每份正文 |
| `knowledge read <项目> <knowledge/文件> [--offset 字符偏移] [--length 12000] [--revision 内容版本] [--all]` | 按 Unicode 字符读取正文，返回 `next_offset/total_length/has_more`；传 revision 可防止混读新旧版本 |
| `knowledge search <项目> --query <文字> [--team] [--limit 50] [--cursor 游标]` | 按标题/正文检索知识条目及旧文件；返回 `entries/page/pagination/scope_note`，四类状态分别判断，正文按需读取 |
| `knowledge get <项目> <条目ID> --revision <版本>` | 读取指定不可变版本和当前证据、适用性投影；跨项目命中使用结果中的 `owner_project_id` |
| `knowledge settings <项目> [--from-file <JSON>]` | 读取或条件更新团队/业务范围；写入 `{expected_hash,team_id,business_scope}`，二者同时非空或同时为 null |
| `knowledge export <项目> --entry <ID>@<版本> [--entry <ID>@<版本> ...] --output <新JSON文件>` | 导出当前项目选定条目的完整版本链；不覆盖已有输出文件 |
| `knowledge export <项目> --all --output <新JSON文件>` | 导出当前项目所有有效 canonical 条目的当前版本；`--all` 与 `--entry` 互斥，不包含旧文件或团队项目 |
| `knowledge import <项目> --file <知识包JSON> --idempotency-key <标识> [--as-new]` | 原子校验并导入；同一请求重试沿用标识，`--as-new` 创建独立谱系，导入后仍需本地验证 |

分页没有项目或用例总数量上限。`--all` 读取全部条目，不能与 cursor 同用；知识正文的 `--all` 从指定 offset 读到末尾。原 Python `cli.context(project)` 仍返回全量，`cli.knowledge(project, limit=2400)` 仍表示每个文件的字符数。

`context` 保留旧 `knowledge`，另提供 `knowledge_entries`。搜索页绑定参与项目的版本、知识设置与旧文件内容；变化后 `cursor_expired`，从首页重读。`--team` 只查询配置了完全相同非空 `team_id/business_scope` 的项目；未配置时返回当前项目及 `scope_note: project_only_unconfigured`，条目标签不能代替项目设置。搜索命中不表示适用或事实已证实。新知识接口错误返回 `code/error/details`，退出 2；完整提交、状态和导入语义见 [诊断与知识闭环](diagnosis-knowledge.md)。

功能设计字段为 `description/preconditions/steps/expected/source`，以及已确认的 `business_path/prefix/author/remarks`。`plan write` 保留这些来源字段供 `plan show` 和同一会话的 `record stop --plan <编号>` 使用；可执行用例将其保存在 `extensions.test_case_document`。主表使用真实业务操作及对应预期，HTTP 请求、选择器和浏览器 IR 留在技术附表。只改功能文字或重新关联旧检查不能证明新预期已验证；需要按来源录制独立检查，或通过工作流提交明确对应新预期的候选断言并真实回归。

## 输入文件、运行绑定与后台转交

JSON 输入均支持 `--file 文件.json`、`--file=文件.json`、`--file -`；最后一种从标准输入读取完整 JSON（保留中文及换行），不是只读一行。文件路径相对发起命令的目录；JSON 内 `sources[*].file`、`artifacts[*].file`、资产定义的 `source` 相对该 JSON 文件目录，stdin 中的路径相对调用目录。`argv`、业务参数里的同名 `file` 字段不会被随意改写。

受限调用的 JSON、显式来源文件/目录经 bridge 转交，并校验附件内容 SHA-256；后台临时输入在命令结束后清理，来源由领域/资产服务快照保存。`import <分享包>` 的输入同样按调用目录处理。直接 HTTP 文件输入可显式带 `base_dir`，省略则以该服务 workspace 为基准；跨命名空间输入使用 CLI bridge 附件转交，或直接提交来源 `text`。

DSH 注册命令中的 `--runtime-context <绑定值>` 同时固定 workspace 和后台端口，不含模型密钥。保留此全局前缀，也可通过 `AUTOSKILL_RUNTIME_CONTEXT` 传入；`--session` 仍独立选择任务。错误保留 `error` 文字，并可带 `code/details`；幂等冲突和过期版本需要读取现状后重新计划，不能盲目重复业务动作。

`workflow next` 返回 `binding_token`；录制、保存和执行该工作项时，继续传全局 `--workflow-binding <token>`（或 `AUTOSKILL_WORKFLOW_BINDING`）。它关联具体工作项，与 `--session` 的浏览器隔离不同。同一任务会话有多个已领取工作项时必须明确绑定，不能猜选最新一项。

通用 CLI 的宿主默认 `generic`；DSH 注册的运行绑定显式选择 `dsh`。宿主投递提供的全局 `--host-session <宿主会话>`（或 `AUTOSKILL_HOST_SESSION_ID`）让后续判定回到该宿主会话，不改变浏览器 `--session`。宿主的模型配置继续由宿主管理，autoSkill 不另建模型配置。

## 工作流

| 命令 | 说明 |
| --- | --- |
| `workflow start <项目> --file <定义.json> --idempotency-key <标识>` | 创建持久流程，定义包含 `title/goal/sources/artifacts/validation` |
| `workflow revise <工作流> --file <定义.json> --idempotency-key <标识> [--expected-hash]` | 提交非空新 sources，开启新维护周期；可更新 title/goal/validation，保留旧版本与执行结果 |
| `workflow status <工作流> [--limit] [--cursor] [--all]` | 纯读取状态及工作项；完成与业务通过是不同字段 |
| `workflow read <工作流> [--kind definition] [--offset] [--limit] [--head] [--text-offset] [--text-length]` | 按需读取 sources/requirements/scenarios/oracles/materialization/knowledge；后续页保留 head，大文档按字符续读 |
| `workflow next <工作流> --idempotency-key <标识> [--owner] [--lease-seconds 300]` | 领取一项任务，返回 item、attempt、lease、输入引用和提交字段 |
| `workflow submit <工作流> <item> --lease-token <token> --attempt-id <attempt> --idempotency-key <标识> --file <结果.json>` | 提交候选定义或诊断；实际执行结论由内核读取证据 |
| `workflow heartbeat <工作流> <item> --lease-token <token> --attempt-id <attempt> --idempotency-key <标识> [--lease-seconds] [--file <检查点.json>]` | 续租并持久保存检查点 |
| `workflow release <工作流> <item> --lease-token <token> --attempt-id <attempt> --idempotency-key <标识> [--file <检查点.json>]` | 释放工作；未知副作用进入恢复，不自动重播 |
| `workflow recover <工作流> --idempotency-key <标识>` | 调和已提交定义、实际执行与恢复日志 |
| `workflow cancel <工作流> --idempotency-key <标识>` / `workflow resume <工作流> --idempotency-key <标识>` | 持久暂停/恢复流程；不会改写已有业务失败 |
| `failure-context <项目> --run <运行ID> --execution-attempt <执行回执ID> --case <用例ID>` | 只读失败上下文与已核验的证据引用，不执行、不领取、不导出、不提交诊断；显式身份不能用最新结果代替 |

同一逻辑请求重试使用同一幂等标识、相同输入；新的意图才生成新标识。已成功但响应丢失的提交返回原收据。`next` 是领取，会改变状态，不能当只读轮询。

诊断提交使用 `{manifest_hash,diagnoses:[{target_id,disposition,reason,evidence_refs}]}`，逐项覆盖当前 `diagnostic_targets`；`disposition` 为 `business_defect/repair/retry/incomplete`。证据缺失、损坏或外部操作结果未知只能 `incomplete`；任一目标 incomplete 就停止该周期自动维护/重试，既有 Fail 保留。工作流租约 `attempt_id` 与真实执行 `execution_attempt_id` 分开。旧版无清单诊断仅保留 `incomplete` 兼容，其他结论需升级到逐目标提交。

知识阶段使用 `{schema_version:2,mutations:[...]}`；支持带 `client_key` 的 create、带 `expected_revision/expected_content_hash` 的 revise/withdraw，完整合同见 [诊断与知识闭环](diagnosis-knowledge.md)。所有阶段都可在提交信封中用 `knowledge_used:[{owner_project_id,id,revision,content_hash,purpose}]` 声明实际引用；它不嵌入阶段 payload，也不证明知识带来提升。

## 原生资产、会话捕获与宿主判定

运行、结果与宿主上下文的统一 `state`、当前/历史周期、1 秒内联动和报告手动重试，见 [状态分类与联动](state-contract.md)。`workflow status <工作流> --cycle current|all` 默认 current；状态读取不触发执行或报告生成。

| 命令 | 说明 |
| --- | --- |
| `engine list` / `engine probe [引擎]` | 已注册适配器与实际安装/运行诊断 |
| `asset import <项目> [--source <目录或文件>] [--engine <引擎>] [--name] [--file <定义.json>]` | 保存完整原生资产；JSON 可提供 source/engine/name、完整 argv 数组、相对资产根的 cwd、source_info、asset_id、expected_head |
| `asset list <项目> [--limit] [--cursor] [--all]` / `asset show <项目> <资产> [--head]` | 分页资产摘要及固定版本详情 |
| `native run <项目> <资产> [--head] [--file <参数.json>] [--host] [--host-session-id] [--wait]` | 默认异步入队并启动本机驱动，返回真实 run_id；宿主路由与目标业务参数分开，有宿主 judge 的运行不要加 --wait |
| `native status <运行> [--limit 50] [--cursor] [--all]` | 默认运行与业务结论摘要、分页用例引用及证据文件路径/hash；不会内联全部 outcome.cases、对话正文与事件 |
| `native status <运行> --case <用例编号>` | 读取该例完整轮次、断言和判定；重复用例编号可用 `result:<结果 id>`；`--all` 显式读取旧全量结果 |
| `native cancel <运行>` / `native dispatch` | 取消运行、恢复已有队列驱动；dispatch 不重建运行 |
| `conversation import <项目> --source <curl或HAR文件> [--format curl\|har] [--name] [--expected-head]` | 保存原始捕获与候选会话请求；不会执行 cURL shell |
| `conversation show <项目> <捕获> [--head]` / `conversation build <项目> <捕获> [--file <映射.json>] [--asset-id] [--name] [--expected-head]` | 查看原请求；按显式 mapping 生成多轮资产，未知字段需补映射 |
| `judge next --idempotency-key <标识> [--owner] [--lease-seconds 300] [--run <运行>]` | 宿主领取待判定对话，不调用另一套模型 SDK |
| `judge status <判定>` | 查看持久判定状态及已有收据 |
| `judge read <判定> [--offset 0] [--length 12000]` | 分页读取冻结的判定输入；继续读取直到完整，不把预览当全部事实 |
| `judge submit <判定> --attempt-id <attempt> --lease-token <token> --idempotency-key <标识> --file <判定.json>` | 提交 verdict、action、reason、clarification、reply、facts_used/evidence_refs |
| `judge heartbeat <判定> --attempt-id <attempt> --lease-token <token> --idempotency-key <标识> [--lease-seconds]` / `judge release <判定> --attempt-id <attempt> --lease-token <token> --idempotency-key <标识>` | 续租或释放判定任务 |

一条多轮样本是一个 case：`samples:[{id,title,turns:[{question,expected/reference,...}]}]`。轮次共享该样本独立会话，任一轮失败保留整例失败。先页面体验、捕获接口、验证多轮与独立预期，再设置 `load` 做并发；接口 200 不等于答案正确。宿主 judge 返回 `pass/fail/uncertain/needs_clarification`，动作 `finish/continue/reply`，不能编造资料替用户回答反问。

原生结果的 `page.total` 是已有结果数，运行的 `total`/`outcome.metrics.planned_cases` 表示计划分母；尚未发现的数量用 `planned_unknown` 明示。`page.content_revision` 绑定整次读取，运行继续产生结果后旧 cursor 会报 `cursor_expired`。完整报告在 `report.path`，并附 SHA-256；命令和 HTTP 默认使用同一分页投影，旧 Python `native_run.get(run_id)` 保持全量兼容。

## 定时任务

| 命令 | 说明 |
| --- | --- |
| `schedule list [--project <项目>] [--offset] [--limit] [--all]` / `schedule show <任务>` | 分页查看持久调度 |
| `schedule create --file <定义.json>` / `schedule update <任务> --file <定义.json>` | 共用 HTTP 调度服务；创建提供 idempotency_key，更新提供 expected_revision |
| `schedule run <任务> [--idempotency-key]` | 创建一次即时 occurrence；维护任务返回 awaiting_host，不代表 AI 已执行 |
| `schedule pause <任务> [--expected-revision]` / `schedule resume <任务> [--expected-revision]` / `schedule delete <任务>` | 修改调度生命周期 |

kind 为 replay/native/maintenance；replay 选项目及 case_ids，native 选 asset_id/parameters，maintenance 选 workflow_id/host/parameters。规则仍由 autoSkill 的 interval/at/daily/weekly 时钟负责，DSH 只接收已到期的单次投递，不再建立第二个重复时钟。投递回执只证明指令已进入宿主会话，不证明流程完成；宿主退出期间保留 outbox。

回放任务定义可提供 `headed: false` 隐藏浏览器窗口，`true` 显示；这个选择保存在任务中，编辑、立即运行、到期执行和重启后继续使用。旧任务或省略该字段的 CLI 任务沿用当前数据目录保存的回放窗口偏好；没有偏好时使用运行环境默认。仪表盘新建任务会将当前窗口偏好作为该任务的初始值，并允许在编辑器中修改。原生资产自己的浏览器启动方式由其原脚本控制。

新任务的 host 默认跟随创建命令的宿主绑定，显式 host 优先；`host_session_id` 是独立字段。更新省略宿主字段会保留原值。创建、更新、恢复任务会启动本机队列驱动，命令退出后未来计划仍可触发；机器重启或驱动被关闭后，用任一 `schedule update`、`schedule resume` 或 `native dispatch` 重新启动。当前没有安装系统开机服务。DSH 插件只能激活运行中宿主的会话，不能启动已退出的 DSH；重开插件后会继续读取持久投递。

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
| `check <类型> [目标] [期望] [--attribute 属性] [--timeout 秒] [--stable-for 秒] [--mode eventually\|always] [--from-json <来源断言JSON>]` | 保存期望并实际检查；失败也保留 actual，passed:false 时退出 1。转换断言可先验证 from-json 再验证目标 |
| `record status` | 已记录的步骤 |
| `record undo` | 撤销最后一步的记录 |
| `record stop --title 标题 [--module 模块] [--category 分类]` | 结束并保存候选及录制观察；普通任务自动安排UI/API分别验证，只有实际通过后才发布；`--plan` 绑定设计行 |
| `record cancel` | 放弃，不生成用例 |

每个会话维护自己的录制；不同会话可以同时录制。`record start`、`browser snapshot`、`browser wait`、`record status` 返回的 `session_id`、`browser_session`、`headed`、`tab` 用于核对当前录制的任务、内部浏览器、启动时窗口选择与标签页；继续命令始终用原任务 `--session`，不能把内部 `browser_session` 当任务标识。

遇到 `needs_login` 或用户选择人工认证时保持原录制和窗口，只让用户在该窗口完成登录后回复继续。恢复时沿用相同 `--runtime-context` / `--host-session` / `--session`，执行 `browser snapshot`，必要时 `browser tab list` 确认登录产生的标签页，再根据新快照继续业务。等待用户不会关闭录制；不调用 `record cancel`、不重复 `record start`，不要求用户填写端口或 profile。事先知道需要手工操作时 `record start <项目> --headed`。手工认证不自动成为可回放登录用例，候选仍按实际认证依赖验证。

目标支持 CSS、`@e编号`、`testid=`、`label=`、`placeholder=`、`text=`、`role=button:名称` 与 `xpath=//button[@id='save']`。显式 XPath 是主定位；录制时自动生成的 XPath 经唯一性验证后追加为末位备选。`browser describe <目标>` 返回可用写法、完整 `locator_definitions` 和 `context`，不会从离线 CSS 猜 XPath。

| 命令 | 说明 |
| --- | --- |
| `browser frame <目标>` | 从当前区域进入 iframe，支持同域、跨域及嵌套 |
| `browser shadow <宿主>` | 进入开放的 Shadow DOM；可与 iframe 交替嵌套 |
| `browser context show` | 查看当前有序上下文路径与该区域元素清单 |
| `browser context parent` | 返回一层；旧元素编号失效 |
| `browser context top` | 返回当前标签页的主文档 |

上下文选择不新增业务操作步骤，后续操作和断言保存其路径。打开地址或切换标签页回到主文档；刷新保留路径，读取和操作都重新解析，不能沿用已失效的 frame 或元素句柄。`snapshot`、`describe`、元素 `get/is/wait` 和验证码读取跟随当前区域；`get url/title` 及地址断言仍指顶层标签页。XPath 不能穿透边界；Shadow 根内自动 XPath 带唯一 CSS `xpath_anchor` 和相对表达式，必须完整保留。没有开放根、区域丢失或定位表达式无效时明确失败，不能当作 `hidden` 或 `count=0` 通过。封闭 Shadow DOM 不支持。

自动编译的业务候选范围是 HTTP、WebSocket、SSE；页面文档、JS、CSS、图片、字体、source map、自动预检，以及明确的 Cloudflare RUM / Google Analytics 等遥测端点不生成业务接口用例。用途不明的浏览器 Ping/beacon 标为未分类观察，不因名称包含 track 等词就排除真实业务请求。`record stop` 以 `business-traffic-v2` 返回业务投影的 HAR/CDP 保留、排除计数和 `api_compile_scope` 编译数量；自动生成的响应规则仍是未验证观察，业务目标与预期需来自独立需求。录制器实际提供的原 HAR 字节另存为 `source.original.har`，未筛选的 CDP 条目及事件另存为 `source.original.cdp.json/jsonl`，保留其中遥测与页面资源，不把业务投影冒充完整原始流量；均不承诺覆盖浏览器全部网络。认证、参数、请求体、响应及流式消息按实际可获取内容保留，不能获取的上传文件字节不会伪造。普通 HTTP 编译器不能完整表示的 multipart、WebSocket 等在 `api_uncompiled` 说明；捕获成功不等于已生成可回放用例。需要显式验证特殊接口时，可从原始来源提取请求并使用 `conversation import` 的 cURL 输入与独立映射，不改变浏览器网络访问。

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

`browser` 子命令不提供浏览器安装、升级、连接外部浏览器、代理或登录态管理；这些由 autoSkill 统一管理。更新 autoSkill 只替换程序文件，并继续使用同一个外置数据目录。

### 录下来的定位方式

每一步保存多个候选定位，回放时按顺序尝试：测试标识（`data-testid`）→ 标签文字 → 占位提示 → 角色+名称 → CSS。首选失效而备选命中时用例仍通过，结果里标注「定位已降级」，提示页面改了。

### check 类型

`visible` `hidden` `text` `contains` `value` `checked` `count` `min_count` `attribute`（需 `--attribute`）需要目标；`url` `url_contains` `title` 不需要。目标可写 `@e5`、`testid=…`、`text=…`、`label=…`、`placeholder=…`、`role=角色:名称` 或 CSS 选择器；`count/min_count` 的目标用 CSS 选择器。

时间断言为采样观察：默认 eventually 在 timeout 内等待目标成立，`--stable-for 2` 要求最终连续成立 2 秒；`--mode always --stable-for 2` 要求整个观察窗口持续成立，任一次观察不符即失败。`--from-json '{"assertion":"text","expected":"处理中"}'` 先在本次检查窗口观察来源，再观察目标，可沿用目标 locator；不表示系统已经在业务 action 之前检查过该状态，也不保证捕捉采样间的瞬时变化。

## 回放

`run <项目> [--case 编号]… [--engine playwright|selenium] [--kind ui|api] [--candidate 候选ID] [--api-only] [--inputs 参数.json]`

`run-status <运行ID>` 只读普通/原生运行的状态、结果和已生成报告、候选发布回执；不认领、不运行、不生成报告。`record stop` 已入队后，用它等待每个返回的 `run_ids`，不能再发一次 `run` 来代替查询。

`--candidate` 验证同一候选任务并报告 `published_ids`、`unpublished`，只发布实际通过且依赖满足的子集。`--api-only` 使用独立冷HTTP会话，不借用浏览器或已保存登录；`--inputs` 从JSON对象加载本次运行参数（`-`为标准输入），`RAND`由每次运行产生，不能覆盖。输入/认证/依赖改变不借用此前通过结果。

- 录制、回放、登录和 DSH 对话任务沿用当前数据目录保存的回放窗口偏好；省略参数不会覆盖用户已经选择的隐藏。没有偏好时默认显示最大化窗口，无桌面的 Linux 默认隐藏。用户最新明确要求显示/隐藏时分别传 `--headed` / `--headless`，命令行优先于旧的 `AUTOSKILL_HEADED=1/0`，再优先于保存偏好；临时覆盖不改写保存偏好；没有窗口时页面为 1920×1080。这些命令行选项适用于 `record start`、`run`、`login`、`dashboard`。
- 全局默认同时执行 2 个任务，`AUTOSKILL_MAX_PARALLEL` 可调大；同一项目的浏览器/HTTP replay 共享登录态，会自动排队，native 原生任务使用独立输入与证据目录、同项目可并行。不同会话录制仍可并行，保存到同一项目时只对写入阶段排队。
- 前置用例自动先执行且每次运行只执行一次；前置没通过时，依赖它的用例记为阻塞。
- API 动作声明 `extensions.postconditions` 时，单选该动作也会执行其后置及依赖；必须在同一运行完成全部独立业务检查，才可形成动作业务 Pass 与完整发布集合。仅 HTTP 201 等受理状态不证明业务完成；后置失败、阻塞或未验证时保留原结果，见[执行契约](workflow/execution.md#显式后置检查删除已受理不等于业务已完成)。
- 登录用例：有保存的登录态且仍有效就直接复用，否则重新登录；回放中途发现登录过期会重新登录一次，只读用例自动重试，写操作不自动重发。
- 没有前置的接口用例不带任何登录信息发送（用来验证「未登录被拒绝」）。
- 接口用例之间的变量：`{{名称}}` 由前面用例从响应里提取。

失败归类：找不到元素、结果与预期不符、超时、地址未授权、缺少凭据、前置用例未通过、登录已过期、缺少前置数据、操作未被允许、环境问题。

## 删除用例与执行记录

| 命令 | 说明 |
| --- | --- |
| `delete case <项目> <用例编号>...` | 删除指定当前用例，保留历史运行和其他用例；不悄悄移除其他用例的前置依赖 |
| `delete run <执行编号>...` | 删除指定运行的结果、报告、截图及不再被其他执行引用的冻结输入；不删除项目、用例、凭据或保存的登录态 |

单条与批量均返回 `deleted/pending/errors` 数组。退出码 0 表示本次请求没有删除错误，2 表示至少一项错误；活动运行先取消，pending 不等于删除成功，可以用相同命令查询/重试直到 pending 清空。重复删除已删除项按已经完成返回，不会重新执行测试。

HTTP 同样调用公共删除服务：`DELETE /api/projects/{project_id}/cases/{case_id}`、`POST /api/cases/delete`（`items: [{project_id, case_id}]`），以及 `DELETE /api/runs/{run_id}`、`POST /api/runs/delete`（`run_ids: [...]`）。仪表盘提供列表、详情及多选删除。删除执行证据不会使缺失证据的工作流继续声称通过；删除前置用例后，仍引用它的运行计划会明确报告缺失。

## 导出与分享

| 命令 | 说明 |
| --- | --- |
| `export postman <项目> [--out 目录]` | Postman Collection v2.1 + Environment。按依赖顺序排列，含断言脚本和变量传递；凭据是环境里的空变量 |
| `export scripts <项目> [--out 目录]` | 每条页面用例各一份 Playwright 和 Selenium 的独立 Python 脚本（含前置步骤），运行时不需要 autoSkill；凭据从同名环境变量读取 |
| `report <项目> [--out 目录]` | Excel 以14列功能测试用例为主表，保留技术定义、执行明细与最近结果附表，并生成 Markdown 摘要 |
| `task-report <报告标识> [--retry]` | 读取本次任务的 Excel、简报和 Bug 清单交付回执；`--retry` 按同一任务快照重新导出，不重新执行测试，也不借用其他任务的结果 |
| `pack [项目]… [--out 路径] [--include-secrets --passphrase 口令]` | 分享包保留用例、知识、项目报告附件、原生资产及原始捕获，不额外加入本机登录态、`.local/` 运行数据库、截图和凭据库；资产或捕获内已有的认证字段保留。`--include-secrets` 额外加密携带凭据库 |
| `pack --full [--out 路径]` | 完整包：运行时、全部项目与知识库及项目报告附件、明文凭据和已保存登录态，统一放到解压后的标准工作区。不含 `.local/` 运行历史数据库、截图和临时进程文件；返回包含的凭据名、登录项目及仍缺少的凭据名 |
| `import <分享包> [--passphrase 口令] [--overwrite]` | 导入轻量分享包。已有同名项目默认跳过 |

完整录制、运行及工作流周期完成后返回 `test_report`，保留原生执行原有 `report` 的含义。只有 `status: ready` 且三个文件检查成功才给出 `excel`、`markdown`、`bugs`、`download_url`、`markdown_download_url`、`bugs_download_url` 及各自 SHA-256、大小；`pending`、`failed`、`skipped` 不能算交付完成。Markdown 简报仅包含任务基本字段、结果汇总、主要问题和 Excel 链接。Bug 清单区分业务断言失败、执行问题和证据待核验，保留此前失败及复测；零问题也不表示未执行的需求已通过。导出失败可按 `report_id` 重试；`task-report` 返回 failed 时退出 2，查询或请求成功也不意味着业务用例通过。HTTP 提供 `GET /api/task-reports/{id}`、`GET /api/task-reports/{id}/excel`、`GET /api/task-reports/{id}/markdown`、`GET /api/task-reports/{id}/bugs`、`POST /api/task-reports/{id}/retry`。

任务级报告与 `report <项目>` 的项目最近结果概览不同，按各自任务、工作流周期、录制身份分别保存。随包 [test-cases Skill](test-cases/SKILL.md) 定义 14 列和业务分层；FT 展示编号不替换内部用例标识，映射和每次尝试均保留。缺少业务事实时明确标记待确认，格式通过不等于业务通过。执行报告页的「下载 HTML 报告」沿用当前页面的报告组件，包含全部用例明细及可取得的截图，下载后无需运行服务；导出时缺失的截图明确说明。

已确认的业务层级、缩写和编制人可写入用例的 `extensions.test_case_document`，例如 `{"business_path":["业务系统原名","模块原名","功能项原名"],"prefix":"GNXYM","author":"实际编制人"}`；示例值不是默认事实。AI 对话样本使用同名 `test_case_document`。这些字段与用例一起冻结，修改当前定义不会回写过去报告。没有来源支持的字段保持待确认，不根据技术引擎或接口路径补造业务层级。

## 其他

| 命令 | 说明 |
| --- | --- |
| `dashboard [--port 8787]` | 启动仪表盘（只监听本机）；已启用的后台队列驱动也可独立处理定时任务 |
| `demo` | 安装示例项目「示例工单系统」；启动仪表盘时会同时启动示例站点 |
| `doctor [--launch]` | 检查运行时；`--launch` 真实启动一次浏览器 |
| `verify` | 按清单校验随包运行时文件（完整离线包） |

## 数据目录与程序更新

| 命令 | 说明 |
| --- | --- |
| `data status` | 只读显示当前数据根、数据身份和已知外部来源，不初始化空目录、不生成报告或启动任务 |
| `data import <旧数据目录或ZIP> [--target <空数据目录>]` | 无损复制整份数据到空目标，原目录保留；不覆盖或合并已有数据；只迁入数据，不替换程序 |
| `data export [--out <ZIP路径>]` | 导出全部数据，包含用例、知识、用例表、执行记录、证据、报告、凭据、登录态、设置及调度状态；默认 ZIP 放在数据根旁 |

默认数据目录是 Windows `%USERPROFILE%\autoSkill-data`、Linux/UOS `$HOME/autoSkill-data`；`AUTOSKILL_WORKSPACE` 或 DSH 插件 `workspace` 可显式选择其他目录。程序更新只替换程序目录并重新加载插件，继续使用同一份数据，不再提供 `upgrade` 命令或 `autoskill-upgrade.py`。

首次从旧版迁入使用数据管理界面或 `data import`，不会自动启用另一个目标目录；若目标不是当前目录，需要修改宿主的 workspace 配置并重新启动。复制/导出需要一致的文件与数据库；活动任务结束后再操作，不能把复制期间变化的文件报成完整备份。旧证据字节与哈希保留，旧绝对路径通过数据根清单解析到新位置，不改写成另一份执行事实。操作说明见 [数据保留流程](workflow/data.md)。

## 文件放在哪

```
<autoSkill-data 数据目录>/
  data-root.json          数据身份、历史位置与路径映射
  projects/<项目>/        可以分享：project.json、qa-config.json、auth.json、testcases/、knowledge/
  .local/                 完整用户数据：凭据、登录态、运行记录、截图、报告、设置与调度
    recording.json        默认会话的录制状态（兼容旧版本）
    recordings/<哈希>/    命名会话的录制状态和临时文件
    case-plans/default/autoskill-cases/ 默认会话的 Excel 用例 Skill
    case-plans/<哈希>/autoskill-cases/  命名会话的 Excel 用例 Skill
```

环境变量 `AUTOSKILL_WORKSPACE` 可以把工作区放到别处（仪表盘和命令行要用同一个值）。

`AUTOSKILL_CASE_SKILL` 可以显式指定用例 Skill 目录，优先于默认和命名会话的目录选择；需要不同任务各自使用自己的表时，使用不同目录或保留各会话的默认目录。首次迁入的已知外置表收纳到数据目录，并按明确映射选择该副本。通过后台服务执行时，调用方显式设置的值用于选择目录（相对路径按调用方位置解析）；空值恢复该会话的默认目录，未设置则沿用服务配置。

## 等待时间

| 设置 | 默认 | 作用 |
| --- | --- | --- |
| `project update <项目> --page-load 秒` | 90（10–600） | 打开或刷新页面最多等多久，录制和回放都用；「页面与资源可访问」类接口用例也用它 |
| `project update <项目> --timeout 秒` | 20（3–120） | 单步等待元素出现、接口请求的超时 |

录制时页面在等待时间内没有完全加载，会带着提示继续，不算失败；回放时超过则记为「超时」。

## 在受限沙箱里

命令所在的进程写不了数据目录、或写不了数据目录下的 `.local/browser-control`（浏览器控制目录）时，autoskill 判定自己在受限沙箱里，把命令转交给本机正在运行的 autoSkill 后台服务（`dashboard` 进程）执行，原样返回它的输出和退出码。

- 后台服务由 DeepSeek Harness 插件启动，或由用户运行 `autoskill dashboard` 启动；它的端口记在 `workspace/.local/service.json`。
- 只转交 autoskill 自己的命令；`dashboard`、`pack --full`、`pack --include-secrets`、以及 `--out` 指向数据目录以外的导出不转交。
- 环境变量 `AUTOSKILL_BRIDGE`：`on` 强制转交（沙箱只拦浏览器、不拦文件时用），`off` 禁止转交；默认自动判断。启动后台服务时设 `AUTOSKILL_BRIDGE_SERVER=off` 可以关闭代为执行。
- 这个接口只监听本机，只接受命令行发来的请求，网页无法调用。
