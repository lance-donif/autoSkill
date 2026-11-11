---
name: qa-case-storage
description: autoSkill 用例存储规范；统一 UI/接口用例、Schema 版本、稳定 ID、执行结果与附件索引，供后续 Excel/CSV/平台适配导出。
---
# 用例存储与转换规范

在生成、修改、执行、导入、导出用例前，主动读取本模块和主技能 references/storage-standard.md、references/case.schema.json、references/result.schema.json。

1. 用标准 JSON 作为唯一权威源；UI 存 functional-cases.json，接口存 api-cases.json。Excel/CSV/Markdown 是派生视图，不从中静默覆盖源数据。
2. 按 storage-standard.md 固定字段、类型、枚举、版本和映射。不把中文列名作为内部键，不用行号作为 ID。
3. 新用例分配不可变 ID，编辑内容递增 revision。重命名、排序或移动模块保持 ID；拆分/合并产生新 ID 并通过 extensions.supersedes 记录来源；删除采用 lifecycle=archived，不物理移除历史追踪。
4. 把用例定义、运行结果、附件分离。运行结果保存 case_revision 和 run_id；每次运行先归档既有结果与证据，禁止导出把旧版本通过结果套到新版本用例。
5. 保存前 validate；迁移必须备份并验证，不认识的新 schema_version 拒绝转换；扩展字段放 extensions，导出器不支持的字段写明映射与损失。敏感数据只存环境引用。
6. 导出前验证来源和覆盖率，记录 export-manifest.json：源文件 SHA-256、Schema 版本、字段映射版本、时间和产物清单。中文显示名、状态和优先级映射由统一常量定义。
7. 外部导入先进入 staging；保存原始 ID 到 extensions.external_id，生成或匹配稳定内部 ID，检测重复和冲突。未经明确合并规则不得直接覆盖。无法映射的字段保留在 extensions 并汇报。
