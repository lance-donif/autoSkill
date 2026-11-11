# 数据契约与执行配置

项目由 init 创建，空文件是待填数据，不是已完成分析。所有 JSON 为 UTF-8。

requirements.json 是数组：`{"id":"REQ-001","module":"登录","title":"登录成功","source":"PRD 第2节"}`。每条 requirement 的 source 指向输入文档，ID 唯一。

两类用例共用字段：
`id, requirement_ids(非空数组), module, title, priority(P0/P1/P2), preconditions(字符串), steps(非空字符串数组), test_data(对象或字符串，只有引用/合成数据), expected(非空字符串), source(字符串), risk(read/write/delete/payment/refund/message)`。
功能用例 ID 推荐 TC-F-001；接口 ID 推荐 TC-API-001。所有用例 ID 跨文件唯一。

接口附加：`api_source(openapi/browser/inferred), source_ref, method, path, headers(对象), params(对象), json(任意JSON，可省略), assertions`。method 仅 GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS；path 是以 / 开头的相对接口路径。inferred 不可执行。参数定义、必填性、认证方式与响应结构在 OpenAPI 清单或 source_ref 指向文档中保留，不臆造。

assertions 至少含 `status`（整数 HTTP 状态码），另可有 `json_equals`（JSON Pointer 到预期值的对象，如 `{"/code":0}`）和 `schema`（JSON Schema）。对业务成功必须增加业务字段或 schema 断言，不能仅检查 HTTP 200。JSON Pointer 支持 ~0 和 ~1；空指针指向整个文档。不支持的断言不能静默忽略。

运行时 `headers/params/json` 中的完整字符串 `${ENV:QA_TEST_TOKEN}` 会从环境读取。只能用完整字符串引用，不能内嵌或输出展开后的内容。

execution/ui-results.json 与 api-results.json 为数组。每个结果：
`{"case_id":"TC-F-001","case_revision":1,"run_id":"RUN-example","status":"Pass","actual":"跳转至首页","assertions":[{"name":"主页可见","passed":true}],"evidence":["execution/screenshots/TC-F-001.png"],"reason":"","started_at":"ISO8601"}`。
Pass/Fail 必须有实际结果、非空断言和存在的证据文件；Fail 至少一条断言失败。Blocked 必须有 reason。Not Run 不写成 Pass。一个通道每条用例仅保留一次当前结果，历史运行先归档到独立目录；不得 API/UI 相互冒名。

qa-config.json 默认示例：
```json
{
  "mode":"review",
  "execution_authorized":false,
  "base_url":"",
  "allowed_origins":[],
  "approvals":[],
  "timeout_seconds":20,
  "cleanup_status":"未执行；未创建数据"
}
```
`execution_authorized=true` 只在用户实际授权后填写；review 的确认发生在宿主对话，不是启动命令。`mode=auto` 同样需要该授权。origin 如 `https://qa.example.test`，base_url 必须是 origin 不带路径。approvals 是用户专门允许的风险字符串数组，例如 write，禁止未经授权自行添加。读操作也要求一般执行授权。

## 示例接口用例
```json
{
  "schema_version":"1.0.0","kind":"api","revision":1,"lifecycle":"active","tags":[],"extensions":{},
  "id":"TC-API-001","requirement_ids":["REQ-001"],"module":"登录",
  "title":"无效凭据被拒绝","priority":"P1","preconditions":"测试环境可达",
  "steps":["提交合成的无效凭据"],"test_data":"合成账号",
  "expected":"401 且 code 为 INVALID_CREDENTIALS","source":"OpenAPI#/paths/~1login/post",
  "risk":"write","api_source":"openapi","source_ref":"input/openapi.json#/paths/~1login/post",
  "method":"POST","path":"/login","json":{"username":"invalid-example","password":"synthetic"},
  "assertions":{"status":401,"json_equals":{"/code":"INVALID_CREDENTIALS"}}
}
```

## 强制存储字段
所有用例还必须包含 schema_version=1.1.0（读取兼容1.0.0）、kind=ui/api、revision>=1、lifecycle=active/archived、tags=[]、extensions={}。所有结果必须有 run_id 和 case_revision；详见 storage-standard.md。输出必须通过 case.schema.json 校验。

Pass/Fail结果现在必须有case_content_sha256，并与当前用例内容一致；调用主引擎case_fingerprint，不从旧结果补造。使用save-cases保存完整用例目录，sync-skills生成UI/API项目Skill。
