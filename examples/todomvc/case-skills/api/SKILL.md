---
name: autoskill-control-api-cases
description: 读取四条已实现的autoSkill本地控制台只读API定义，使用独立HTTP脚本验证，不依赖AI，也不冒充TodoMVC业务接口。
---
# 本地控制台接口用例 Skill

读取 `../../api-cases.json` 与 `../../requirements.md`。目标是同机autoSkill服务8787端口的status/projects/runs/tasks四个GET接口；TodoMVC仅使用本地存储，没有此处虚构的业务API。

将定义导入完整项目，确认授权origin后运行脚本接口执行器。请求方法、路径、参数与断言从JSON读取，不需要LLM。用户改动端口须同时更新项目base_url、allowed_origins和后台监听配置。

响应必须实际采集并独立断言。记录用例修订、内容哈希、状态、耗时和日志；UI捕获、源码查看和ASGI集成测试不能冒充本次真实网络调用。AI仅在用户选择解释或AI执行时介入。
