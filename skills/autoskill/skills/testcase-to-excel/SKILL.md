---
name: testcase-to-excel
description: autoSkill 内部模块，将标准用例与执行证据输出为 Excel 工作簿。
---
# Excel 输出
先校验 JSON 与结果，再运行主技能 export。分别生成两份中文工作簿，包含用例、需求覆盖、执行统计 Sheet。冻结首行、自动筛选、按列宽换行、状态着色；长响应留在脱敏证据文件中，Excel 保留摘要和路径。
确认用例数量、ID、需求关联与 JSON 一致，含 Not Run 与 Blocked。禁止把用户输入解释成 Excel 公式。不得将密码、Cookie、Authorization、Token 写入单元格。新文件不要覆盖输入文档。
