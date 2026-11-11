---
name: test-data-knowledge
description: autoSkill 内部模块，检索本地业务规则、角色、字段和测试数据知识库。
---
# 测试知识库
使用项目 knowledge/ 下 business-rules.md、user-roles.md、data-dictionary.md、test-accounts.md、test-data-rules.md、historical-defects.md。支持经宿主读取 Excel 后转换到项目文本索引；数据库连接另按用户配置，不假设存在。
对每条规则记录来源、版本、适用模块。用 search 缩小范围后读原文，空结果不能当成无规则。知识库与新 PRD 冲突时显式记录，按已确认的新规则处理。
账号仅记录用途、角色和环境变量名，实际密码/Token 在运行时读取。生成独立测试数据，记录创建 ID、清理方式与清理结果；不要自动向公共数据或生产库写入。
