---
name: todomvc-ui-cases
description: 读取官方开源TodoMVC的20条UI定义，可供AI执行、生成脚本或直接使用白名单脚本执行器复跑。
---
# TodoMVC UI用例 Skill

主动读取 `../../ui-cases.json`、`../../requirements.md` 和 `../../source-manifest.json`。这里是模板定义，不是已完成测试。

先用autoSkill的项目导入/seed-demo或save-cases建立完整项目；确认用户已授权本地8765 origin和仅合成任务的write操作。脚本从每条extensions.automation读取start_url、actions和assertions，使用runtime/engines.py的确定性解释器，无需模型，不执行任意代码。AI执行则先读官方Agent Browser Skill并真实操作，不把源代码推断当成已执行。

每条独立浏览器上下文。结果单独保存run_id、case_id、修订、case_content_sha256、状态、实际结果、断言与证据。初始环境受阻记录不等于未来结果；回放阅读真实步骤日志，重新执行创建新快照。无法运行时保持Blocked/Not Run，绝不写Pass。
