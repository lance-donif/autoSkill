# 接口验证、回放和用例资产

下列命令沿用技能入口绑定的程序路径、`--runtime-context` 和本任务的 `--session`。

## 接口候选必须独立冷启动验证

接口录制只保存业务通信：HTTP 增删改查请求、WebSocket 握手与消息、SSE 事件流，以及实际捕获到的上传和下载接口。纯页面导航、脚本、样式、图片、字体、source map 和浏览器自动 CORS 预检不作为接口资产保存或生成用例；关联的认证及业务重定向链保留。判断以请求用途和响应内容为依据，不会只因业务接口地址以 `.json`、`.txt` 或 `.pdf` 结尾就丢弃；页面操作仍单独保存为页面用例。

`record stop` 将可编译的业务 HTTP 请求保存为接口候选：从状态码、字段和响应结构生成观测基线；这些观察不是独立业务预期，不能据此证明系统原本正确。令牌自动变成变量并依赖登录用例，前一个接口返回的 ID 自动传给后一个。`record stop` 输出里的 `steps` 包含「打开页面」这一步，`assertions` 是断言条数。`api_new` 是本次新候选数，`api_reused` 是引用已有案例的数量；两者都不能代替 `published_ids` 的入库事实。输出里的 `notes` 会指出断言较弱、需要宿主依据独立需求补齐的接口。输出里的 `api_traffic`：

- `business`：页面调用了业务接口，已保存对应候选，是否可复用须看独立验证和发布回执。
- `none`：本次没有生成普通 HTTP 接口用例。纯页面跳转不生成接口用例；也要检查 `api_uncompiled` 和会话捕获，不能据此断言没有 WebSocket 或上传等业务通信。页面能否打开由页面用例验证。

`conversation_captures.capture_scope` 说明筛选策略及各来源保留、排除的数量；`complete_network_log: false` 表示这是筛选后的业务捕获，不是浏览器全量网络日志。旧 HAR 缺少请求类型时，会保守保留无法确认是页面资源的请求。`api_uncompiled` 列出捕获但尚未生成普通 HTTP 回放用例的请求，例如 multipart 上传和 WebSocket；保留可获得的请求体、认证与消息，缺失的文件字节明确标记，不能宣称已能直接回放或导出为完整 Postman 用例。对话接口可按 `conversation build` 的显式字段映射生成多轮会话资产。

普通录制会把 UI 与 API 候选分别安排验证。API 使用冷的 HTTP 会话，不借用浏览器 Cookie 或已保存登录；认证、提取变量、创建/查询/修改/删除的数据依赖须通过接口依赖图自理。使用 `run <项目> --candidate <候选ID> --api-only --inputs <运行参数文件>` 可主动验证指定候选和不同输入；运行参数由你准备，`{{RAND}}` 每次生成，不能以旧响应中的固定资源 ID 冒充可复用数据链。

HAR 候选只有 observation 预期时，首次 API 回放只核对录制时的观测值。由你读取原需求、已交付用例表和接口资料，保留录制请求作为技术依据，使用已有 `workflow start` → `workflow next` / `workflow submit` 继续一份纯 API 验证：提交来源、需求、场景和 `kind: api` 的独立 oracle，再在 materialize 提交登录、造数、业务校验、清理的 API 候选、依赖提取与场景 bindings。沿用原设计任务的 `--session`，从 `plan show` 选中设计行，将该行的 **`execution_source` 原样复制到 `extensions.test_case_document.source`**，功能描述、前置、步骤和预期也取自同一行；不能只写 Excel 路径和行编号。按[执行文档的接口补全示例](workflow/execution.md)核对公开的绑定、执行及发布回执，再确认原 `task_id`、原 `delivery_excel` 的对应行已经回填；失败未入库也要保留在原表中。这些数据由你准备，不让用户填 JSON 或重新调用技能；原 candidate 包及失败报告不改写。只有需求或认证确实无法确定时说明具体缺口，不能仅改 observation 标签、照抄错误响应或省略数据依赖来声称已生成可复用接口。

接口只返回受理状态时，可由有来源的 API 后置用例检查真实业务结果；用 `extensions.postconditions` 显式关联，后置通过 `depends_on` 追溯原动作，单选动作也会实际执行后置。全部后置在同一运行通过后才形成动作的业务 Pass 与完整发布集合；后置失败不能用受理状态掩盖。字段与示例见[执行文档](workflow/execution.md#显式后置检查删除已受理不等于业务已完成)。

纯 API 工作流可冷验证；混合工作流里的 API 若没有冷会话回执，会保留未发布原因。由你在同一技能内续做对应纯 API 范围验证，不能声称混合工作流已自动完成所有接口的独立验证。也不能把只依赖浏览器状态才能运行的录制描述为独立可复用 API 流程。

边输入边搜索（每敲一个字发一次请求）只保留最后一次请求。接口回放是普通 HTTP 请求：有反爬网关的公网站点可能只认真实浏览器，这类站点以页面用例为准。

## 回放、导出、分享

```
autoskill run <项目>                          # 全部用例；前置用例（如登录）自动先执行
autoskill run <项目> --case TC-UI-002         # 指定用例，可重复 --case
autoskill run <项目> --engine selenium        # 页面用例换 Selenium 回放（默认 Playwright）
autoskill run <项目> --kind api               # 只回放接口用例
autoskill run <项目> --headless               # 本次显式隐藏窗口；省略时沿用已保存的回放窗口偏好
autoskill export postman <项目>               # Postman 集合 + 环境（不含凭据值）
autoskill export scripts <项目>               # 独立的 Playwright / Selenium 脚本
autoskill report <项目>                       # Excel 用例表 + 执行报告
autoskill pack                                # 项目与原始资产；不额外加入凭据库/登录态，资产内已有认证字段保留
autoskill pack --full --out <目录>            # 完整包：运行时、项目、明文凭据与已保存登录态
autoskill import <分享包>                     # 导入别人给的分享包
autoskill dashboard                           # 仪表盘（给人看；会一直运行，放后台启动或请用户运行）
```

目标步骤全部完成、已配置检查通过且执行记录完整时，用例结果为通过。未配置业务断言时，按步骤执行结果判定。目标开始前取消或受阻记为未执行，开始后中断、检查失败或执行证据缺失记为失败。

### 删除用例与执行记录

用户要求清理时，使用 `autoskill delete case <项目> <用例编号>...` 删除当前用例，或 `autoskill delete run <执行编号>...` 删除指定执行记录及其报告、截图和冻结输入。两者分开：删除用例保留历史运行，删除运行不删除用例、项目或登录数据。仪表盘列表和详情也有单条、批量删除按钮。

返回的 `deleted`、`pending`、`errors` 要分别报告；有部分错误时不要声称全部成功。活动运行会先取消再清理，`pending` 只表示已经请求，继续用同一删除命令查询/重试到完成，不创建新的运行。没有删除错误时退出码为 0，有删除错误为 2；不能单凭退出码把 pending 当已删除。不能通过删除失败记录来宣称测试已经修复或通过。

## 已有 Excel 与设计版本

用户提供已有用例时，先核对真实来源、步骤、预期和范围，再由你读取为本任务设计，不要求用户重新填表或再次调用技能。`plan write` 返回的 `delivery_excel` 才是正式交付文件；默认表在数据根的 `.local/case-plans/default/autoskill-cases`，命名会话各有独立目录，以返回路径为准。`AUTOSKILL_CASE_SKILL` 仍可指定兼容表目录。

保持同一任务设计完整。录制结束使用 `--plan` 绑定行；未入库的失败候选也通过冻结身份和实际运行回填，不用当前案例库是否有该案例推断是否执行。`plan mark <编号> 未执行 --note <实际原因>` 可记下无法继续的原因；手填“通过”不是执行证据。需求确实改变时保存新设计和新验证，原任务的初始 Excel、历史失败与报告继续保留。

项目知识先从 `context`、`knowledge list/read` 按需读取（业务规则、角色、测试数据、历史缺陷）；工作流中通过 knowledge 阶段提交关联来源与证据的事实，形成可追溯版本。旧项目 `knowledge/*.md` 继续作为兼容资料读取。只记录确认过的事实，不把密码写入说明文档。

录制、回放、登录和 DSH 对话任务沿用当前数据目录保存的「回放设置 → 显示浏览器窗口」。默认显示最大化窗口。Linux 显示窗口需要图形桌面，缺少桌面时报告启动受阻。用户本次明确要求显示或隐藏时使用 `--headed` 或 `--headless`，命令行优先于显式 `AUTOSKILL_HEADED=1/0`，后者优先于保存的偏好。定时任务使用其单独保存的窗口选择，旧任务没有单独选择时沿用回放偏好。
