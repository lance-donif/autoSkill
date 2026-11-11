# 用例存储规范 v1.1.0（兼容1.0.0读取）

## 目录与权威来源

| 路径 | 内容 | 规则 |
| --- | --- | --- |
| project.json | 项目 ID、schema_version | 项目 ID 不变 |
| analysis/requirements.json | 需求及来源 | 需求 ID 唯一 |
| analysis/api-inventory.json | 接口及来源 | 不与实际测试结果混淆 |
| testcases/functional-cases.json | UI 用例数组 | 权威源，UTF-8 JSON |
| testcases/api-cases.json | API 用例数组 | 同一公共 Schema |
| execution/ui-results.json | 当前 UI 执行结果 | 记录用例修订号 |
| execution/api-results.json | 当前 API 执行结果 | 记录用例修订号 |
| execution/history/RUN-ID/ | 历次运行结果与证据快照 | 只追加；先归档再重跑 |
| execution/screenshots/、logs/ | 当前相对路径证据 | 不允许绝对路径或 .. |
| output/ | Excel、Markdown、manifest | 可由权威源重建 |

## 字段字典

所有用例固定公共字段，完整机器校验见 case.schema.json。

| 内部字段 | 类型/枚举 | 统一导出含义 |
| --- | --- | --- |
| schema_version | 1.1.0（读取兼容1.0.0） | 数据格式版本 |
| id | 非空字符串，项目内唯一且不可变 | 用例编号 |
| kind | ui / api | 用例类型 |
| revision | 正整数，修改递增 | 用例修订 |
| lifecycle | active / archived | 生命周期 |
| requirement_ids | 非空字符串数组 | 关联需求 |
| module、title | 非空字符串 | 所属模块、用例标题 |
| priority | P0 / P1 / P2 | 优先级 |
| preconditions | 字符串 | 前置条件 |
| steps | 非空字符串数组，有序 | 测试步骤 |
| test_data | 对象或字符串 | 合成数据或凭据引用 |
| expected | 非空字符串 | 预期结果 |
| source | 非空字符串 | 需求来源 |
| risk | read/write/delete/payment/refund/message | 执行风险 |
| tags | 字符串数组 | 标签 |
| extensions | JSON 对象 | 扩展数据；导出时保留为 JSON 列 |
| api_source、source_ref | openapi/browser/inferred/source-and-official-docs；来源定位 | 接口可信来源 |
| method、path | HTTP 方法、相对路径 | 请求方式、接口地址 |
| headers、params、json | JSON 对象/任意 JSON | Headers、Query、请求体 |
| assertions | 状态码、JSON Pointer、JSON Schema | 可执行接口预期 |

禁止把 status、actual、evidence 放入用例定义，它们属于执行结果。
结果额外必填 run_id（独立运行 ID）、case_revision（测试的用例版本）。仅结果修订号与用例一致才能进入当前导出，旧修订保留在历史中。缺结果显示 Not Run，不推断 Fail。
JSON 空对象/数组表示确实为空，未知描述在 extensions.open_questions 中记录；不能拿空字符串冒充未知的接口地址或状态码。扩展字段不改变已有字段语义。

## 版本与变更

schema_version 使用语义版本；本版写入1.1.0，读取兼容1.0.0。revision 是业务用例变更版本，两者互不替代。
修改先归档原始用例到项目 versions/ 下有时间戳的快照，再提升 revision；requirements 和 project 同时纳入快照。存储规范不等于自动并发编辑服务，多人编辑需外部版本库或锁。
迁移先备份、显式转换、校验记录数量/ID/引用、写迁移记录再替换；禁止静默丢弃未知字段。

## 导出契约

所有导出读取同一个校验后的数据模型；中文列仅是显示映射。列表用换行展示，对象用 JSON 保留结构。Excel 中用户内容强制按文本写入，防止公式解释。
状态固定 Pass、Fail、Blocked、Not Run；生命周期 archived 不进入当前执行统计，但保留在用例表与源 JSON。
每份工作簿包含用例表、需求覆盖表、执行统计表，附件使用项目相对路径；转交附件需一起打包。完整 JSON 的结构保留在权威源，Excel 是有长度限制的视图，超过单元格长度时截断并注明查看 JSON。
生成 output/export-manifest.json：schema_version、mapping_version、generated_at、source_sha256、artifacts、columns；记录未支持映射，默认 JSON extensions 列保留所有扩展。
CSV、其他测试平台、双向导入属于后续适配器：必须实现同样的 validate → map → export → manifest 流程，不能宣称现已支持所有格式。

## 已实现的存储命令

使用 `save-cases <项目目录> <完整用例目录JSON>` 统一更新用例；自动快照、修订递增、缺失旧用例软归档并生成项目UI/API用例Skill。直接手工编辑仍应递增revision；任何内容变化都会令旧结果哈希失配而禁止导出。

Pass/Fail结果额外必填case_content_sha256：对用例对象用Python json.dumps(ensure_ascii=False, sort_keys=True, separators=(",",":"))产生UTF-8字节后计算SHA-256。用主引擎case_fingerprint函数取得值，跨语言实现必须通过相同样本验证。旧的无哈希结果必须先归档并重跑，不能自动补哈希冒认通过。run-api先检查定义、归档旧结果，再执行并严格校验新结果。

通过save-cases保存旧格式定义时，会显式升级schema_version至1.1.0并保留快照；版本字段变化同样参与内容哈希，不沿用旧的通过记录。外部响应Schema引用须在分析阶段解析，执行器不会自动访问未知引用。
