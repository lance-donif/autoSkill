# 插件后台 API

启动：`python -m runtime.service --root <状态目录> --port 8787`。仅监听127.0.0.1；接口、页面与数据同源。拒绝DNS重绑定Host和浏览器跨站写请求，不向公网开放服务。

- `GET /api/status`：运行时状态。
- `GET/POST /api/projects`：列出项目、导入结构化需求与用例。
- `GET/PUT /api/projects/:id/cases`：权威用例与版本化保存。
- `GET/PUT /api/projects/:id/config`：授权origin、执行授权与运行参数。
- `GET/POST /api/runs`：查看历史，选择ai/script与ui/api启动。
- `GET /api/runs/:id`：结果、逐条断言和证据相对路径。
- `POST /api/runs/:id/replay`：基于当前项目用例创建新的运行；不是篡改原运行，旧快照仍保留。
- `GET /api/runs/:id/artifact/...`：仅允许本次运行下的execution/output/case-skills产物，不允许目录越界。
- `GET/POST /api/tasks`：列出/创建持久化定时任务。
- `PATCH /api/tasks/:id`：暂停/恢复；一次任务结束后需新的未来时间才能重启。

规则支持间隔（至少60秒）、一次（带偏移量ISO时间）与每天（HH:mm+IANA时区）。暂停/恢复和进程重启保存到SQLite。错过的间隔合并到下一次，不并发重复触发同一活动任务；重启中断的运行标Blocked，绝不当作已完成。

示例任务：
```json
{"title":"接口巡检","project_id":"demo","engine":"script","kind":"api","case_ids":[],"rule":{"kind":"daily","time":"09:00","timezone":"Asia/Shanghai"}}
```

只有已授权且匹配的活动用例才能创建任务。UI脚本模式要求用例已经有extensions.automation，缺少动作必须先由AI生成或人工补充并校验。AI调度调用官方DSH SDK，不从“任务已入队”推断模型或测试已成功。调度宿主必须保持运行。

仪表盘追加接口：POST /api/examples/import（内置用例导入，默认未授权）；POST /api/projects/:id/generate（AI生成指定数量与类型，校验后存储项目Skill）；POST /api/runs/:id/explain（只读AI解读，独立保存每次解读，不改变原测试结果）。生成数量、结构、ID或期间数据版本不一致时阻止覆盖。调度发生记录与推进时间在同一SQLite事务提交；宿主中断也保留Queued/Running收据并在重启后标Blocked。
