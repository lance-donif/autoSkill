# 可执行化、验证与恢复

materialize/maintain 阶段产出候选用例或已有资产绑定，提交给工作流服务。不要先编辑当前用例正文再尝试通过 submit 补一个校验；候选提交和发布需要内容版本比较。真实执行使用固定输入及依赖快照。录制返回的 `candidates` 作为提交的 `cases`，同时提交 `candidate_auth` 以冻结尚未发布的新登录依赖；只有通过的依赖闭合子集可以发布。

录制时先确认业务预期，再操作页面。`check` 会保存期望与实际，失败也形成证据；带缺陷的候选保留在完整Excel、候选快照和报告中，不发布到可复用案例库。每个任务使用独立 session，保持 workflow/item/attempt 的关联。长任务通过 heartbeat 保存检查点；记录的动作存在未知结果时，先读取现场和操作日志，不能直接重播提交/删除等写操作。

## UI 预期与录制候选的对应

design/completion 中 `kind: ui` 的 oracle 使用有序 `checks` 列表，例如：

```json
{"id":"oracle-ui-1","kind":"ui","requirement_ids":["req-1"],"checks":[{"assertion":"text","expected":"已预约"},{"assertion":"text","expected":"合计：160 元"}]}
```

列表顺序对应候选 `extensions.automation.actions` 中的检查项，再接 `extensions.automation.assertions` 中的检查项；每项只保留已经存在的 `assertion`、`expected`、`attribute`。保留值的 JSON 类型，不把缺失字段补成 null。`locators`、检查的 `oracle` 来源、`recording_result` 和等待参数保留在完整候选中，不复制进设计 oracle 的 `checks`。API oracle 则原样对应候选的 `assertions` 对象。

若已持有公开 `record stop` 回执中的 UI `candidate`，宿主可用下面的投影核对已声明的设计 oracle。预期先来自需求或原 Excel；这段投影不能把现场实际值改写成预期，也不能用来覆盖先前设计以消除失败。

```python
from copy import deepcopy

automation = candidate["extensions"]["automation"]
checks = [
    {key: deepcopy(step[key]) for key in ("assertion", "expected", "attribute") if key in step}
    for step in [*automation.get("actions", []), *automation.get("assertions", [])]
    if "assertion" in step
]
```

场景通过 `oracle_ids` 关联该 oracle，materialize 的 `bindings` 关联同一场景与完整候选。`workflow next` 的 `payload_schema.ui_example` 提供完整 UI 设计示例；阶段领取后仍使用该次 item、attempt 和 lease 提交。检查列表不一致时保留 `oracle_change`，核对格式和原预期，不改变业务判定。

## 执行与恢复

复用 native 资产先 `engine probe`，保留入口 argv、fixture、数据文件及原断言。`native run` 默认异步并返回 run_id。轮询 native status、处理 judge next、继续其他可独立工作；队列驱动启动失败则 `native dispatch` 恢复同一队列，不重新创建运行。

当真实结果不符合需求，先读取 `next/status` 的冻结 `diagnostic_targets` 和 `manifest_hash`，再按[诊断与知识闭环](../diagnosis-knowledge.md)逐项读取证据，提交 `{manifest_hash, diagnoses:[...]}`。每个目标都要有唯一的诊断、理由和属于该目标的证据引用；产品缺陷在对应项使用 `disposition: business_defect`，本批完成且 conclusion 为 violated。定位器或 fixture 问题可提出候选修复后重跑，保留原判定条件。证据缺失或副作用未知时保留 `incomplete`；不能以反复修改期望获取通过。

knowledge 阶段记录确认过的事实和证据引用；推测单列为分析。`complete:true` 代表本批工作已处理完，是否通过看 conclusion 和实际执行证据。历史、过期、外来和手工结果均不能冒充本机当前版本验证。

纯API候选按冷HTTP会话验证，不借用浏览器或已保存登录。混合工作流中的API缺少独立冷验证回执时保留未发布原因，由同一宿主技能续做纯API范围验证，不承诺已自动验证全部混合资产。常规录制通过 `record stop` 自动队列验证，用 `run-status` 等待真实结果；只有有意发起新验证时使用 `run <项目> --candidate <候选ID> --api-only --inputs <参数文件>`。

## 从接口观察继续完成独立验证

录制的 API 候选包是原始观察快照，`run --candidate` 不负责修改其中的断言。首次回放显示 Unverified 时，宿主应在同一用户任务中继续补全可确定的需求与数据链，不把这次未验证当作交付结束。

1. 沿用生成原表时的 `--session`，读取 `plan show` 的完整 JSON 回执；核对 `project`、`task_id` 和 `delivery_excel` 与本任务 `plan write` 的回执一致，保存要执行的设计行及其 `execution_source`。不为补做 API 再次 `plan write` 创建另一张表。读取需求及接口资料，按[来源与设计契约](design.md)准备纯 API 范围。执行 `workflow start <项目> --file <定义文件> --idempotency-key <本次标识>`；后续以 `workflow next` 返回的 `payload_schema` 为准，用同一次领取的 item、attempt、lease 和稳定提交标识执行 `workflow submit`。这些数据由宿主准备，用户无需填写。
   首次冷执行需要明确运行参数时，在定义中提供可选的 `validation.inputs`，例如 `{"validation":{"inputs":{"payload":{"quantity":2,"enabled":true,"optional":null}}}}`，候选用 `{{payload}}` 引用。JSON 数字、布尔值、null、数组和对象保留类型并随本次执行冻结；`RAND` 由运行生成，不能覆盖。参数值取自本任务已确定的条件，不必先执行失败再补参数。
2. sources 保留原文或接口契约；requirements 写独立预期与 `source_ids`。design/completion 写场景的 `requirement_ids`、`oracle_ids`，以及 `kind: api`、`checks` 的 oracle。需要功能文档映射时，oracle 的 `functional_expected` 与原设计预期原文对应；不能以当前响应值替代要求。
3. materialize 中以录制请求结构为依据构造完整 `cases`：接口登录提取令牌，创建本次数据并提取 ID，业务接口引用这些变量，最后清理本次数据。用 `extensions.depends_on` 和 `extensions.extract` 表达实际接口依赖，动态值用 `{{变量}}`，新数据用 `{{RAND}}`；不得依赖 UI 登录、浏览器 Cookie 或旧资源 ID。所有候选都必须通过 `bindings` 关联场景，API `assertions` 必须与相应 oracle 的 `checks` 一致。对应设计行的候选按下方示例设置 `extensions.test_case_document`，并记录 `extensions.oracle.source: requirement` 及依据；只换标签而没有独立检查不成立。原录制候选、失败结果和报告仍然保留。
4. 需要尚未发布的登录配置时，提交同批 `candidate_auth`；依赖案例也作为候选和场景提交，避免借用另一批历史成功。纯 API 工作流会冻结依赖并实际冷执行。继续读取 `workflow status` / `workflow next` 完成执行和必要诊断。在 `workflow status` 的 `items` 中按本次 `run_id`、`candidate_id` 找到执行项，核对 `plan_bindings` 的 `case`、`task_id`、`run_id` 与原设计行和本次运行一致；若有 `error`，明确记录原表尚未绑定及原因，不能声称已回填。再核对该项的 `publication_receipt.complete`、`published_ids`、`unpublished`；未完成时继续读取，分页时按 `next_cursor` 续读。命令退出码、流程结束或候选 ID 都不能代替入库事实。
5. 执行结束后用同一 session 的 `plan show` 核对原 `task_id`、原 `delivery_excel` 和每条对应行的 `status`、`note`；必要时 `plan sync` 后重读，但它不能补造缺失的任务绑定。检查 `test_report.status: ready`，读取正式14列 Excel 的执行结果、实际结果和问题记录，再交付原表及本批明细报告。失败未发布的候选也应回填该表，未执行行仍保留；某行仍显示未执行或绑定报错时，保留运行证据并说明缺口，不能只交付工作流报告便宣称主任务完成。条件修复后的新验证与原失败分开记录，不能覆盖原预期，也不能声称所有捕获请求都已可复用。

### 显式后置检查：删除已受理不等于业务已完成

接口只能返回受理状态时，用 `extensions.postconditions` 指定独立检查该动作结果的 API 用例；它是非空、不重复的 API case ID 数组。每个后置用例必须有来源明确的业务断言，并通过 `extensions.depends_on` 直接或间接追溯到原动作；不能指向无关的成功用例或 observation 基线。

例如接口契约明确 DELETE 返回 201，随后按本次资源 ID 查询应返回空列表。已有登录和创建用例已提供本次 `session_token`、`record_id` 时，可在完整候选中写以下字段；其他必填业务文档、场景关联和 oracle 仍由本次需求提供：

```json
[
  {
    "id": "TC-DELETE",
    "kind": "api",
    "method": "DELETE",
    "path": "/records/{{record_id}}",
    "headers": {"Cookie": "session={{session_token}}"},
    "assertions": {"status": 201},
    "extensions": {
      "depends_on": ["TC-CREATE"],
      "postconditions": ["TC-ABSENT"],
      "oracle": {"source": "requirement", "requirement_ids": ["REQ-DELETE"]}
    }
  },
  {
    "id": "TC-ABSENT",
    "kind": "api",
    "method": "GET",
    "path": "/records",
    "params": {"id": "{{record_id}}"},
    "headers": {"Cookie": "session={{session_token}}"},
    "assertions": {"status": 200, "json_equals": {"/items": []}},
    "extensions": {
      "depends_on": ["TC-DELETE"],
      "oracle": {"source": "requirement", "requirement_ids": ["REQ-DELETE"]}
    }
  }
]
```

单选 `TC-DELETE` 会连同所声明后置和实际依赖一起执行。单步 201 状态码通过只证明动作已执行，业务仍为 Unverified；所有声明后置均在同一运行真实通过，才可据此形成该动作的业务 Pass，并发布满足条件的完整依赖与后置集合。原 HTTP attempt 仍保留其检查结果。后置失败、阻塞、缺失或未验证时，不能发布动作或借另一轮成功证明本轮完成。已有 204/205 无响应体契约继续适用，但一旦声明了后置，也必须满足这些后置。

Cookie 头可以使用 `session={{session_token}}; csrf={{csrf_token}}`，每个 Cookie 的值完整引用对应提取变量；不要粘贴浏览器里的固定令牌。录制编译器生成的 Cookie 合并元数据由程序维护，无需宿主手填。

### 将公开设计行绑定到 API 候选

下面只组装 materialize 中一个候选的功能文档；`shown` 是同一 session 的 `plan show` JSON，`candidate` 是依据真实接口资料构造的完整 API 候选。将 `DESIGN-001` 换成选中的设计行编号，其他候选和场景仍按当前 `payload_schema` 提交。

```python
from copy import deepcopy

row = next(item for item in shown["cases"] if item["id"] == "DESIGN-001")
document = {key: deepcopy(row[key]) for key in ("preconditions", "steps", "expected")}
document["description"] = row.get("description") or row["title"]
document["source"] = deepcopy(row["execution_source"])
if row.get("business_path"):
    document["business_path"] = deepcopy(row["business_path"])
candidate.setdefault("extensions", {})["test_case_document"] = document
```

`execution_source` 已包含 `kind: case_plan`、`file`、`case_id`、`task_id`、`business_row_hash`，以及原设计有来源时的 `upstream`。整个对象原样复制；不自行计算哈希、填任务 ID、改写 upstream，或将其中的 `file` 换成对外交付的 `delivery_excel`。这些值全部来自公开回执，不需要读取内部状态文件。缺少 `execution_source` 表示该行尚无与原任务匹配的公开绑定来源，应核对是否仍在原 session、原任务及原设计版本，不能拼一个对象冒充原设计。场景 `bindings` 负责关联场景与候选，不能代替这里的原 Excel 行绑定。

功能文档的 `expected` 和 `steps` 可保留原有文字或非空文字数组；数组表示仍保存在 `extensions.test_case_document` 中。对照原设计及 oracle 的 `functional_expected` 时，仅将数组逐项以单个 LF 连接为完整文字，不去掉空格、换行或编号；顶层 `expected` 使用该完整文字。这里的表示兼容不改变 API/UI 技术 `checks` 的 JSON 类型，也不允许根据执行观察修改原预期。
