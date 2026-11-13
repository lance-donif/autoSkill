# 示例工单系统（Ticket Desk）

autoSkill 随附的演示站点，用来练习和验证「先登录 → 再操作 → 接口回放」。只用 Python 标准库，数据全部是合成数据并保存在内存里，重启即恢复初始 3 条工单。只监听 127.0.0.1。

启动：`python server.py --port 8790 --title-max 80`，然后访问 <http://127.0.0.1:8790>。V2 使用 `--title-max 40`。

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `qa_admin` | `Passw0rd!demo` | 管理员：可新建、可标记已解决、可删除 |
| `qa_viewer` | `Passw0rd!view` | 只读访客：只能查看 |

这两个账号只存在于本演示站点，不是任何真实系统的凭据。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 无需登录 |
| GET | `/api/config` | 当前实现配置 `{title_max}`；它不是需求预期来源 |
| POST | `/api/login` | `{username,password}` → `{token,user}`，同时下发会话 Cookie |
| GET | `/api/me` | 当前用户；未登录 401 |
| POST | `/api/logout` | 退出 |
| GET | `/api/tickets?status=&q=` | 列表 `{items,total}` |
| POST | `/api/tickets` | 新建；标题必填且不超过 80 字，否则 422；访客 403 |
| GET / PATCH / DELETE | `/api/tickets/{id}` | 详情 / 修改状态 / 删除；删除后详情 404，列表消失 |
| POST | `/api/test/reset` | 测试辅助：恢复初始数据并清空会话 |
| POST | `/api/test/expire-sessions` | 测试辅助：让所有会话立即失效 |
| POST | `/api/test/bug` | `{on:true}` 故意把新工单的高优先级存成 medium；`false` 修复 |
| POST | `/api/test/config` | `{title_max:40}` 改变实现边界，不改变需求文件 |

登录后页面把令牌放在 `localStorage`，请求用 `Authorization: Bearer`；服务端同时接受会话 Cookie。因此保存的登录态必须同时包含 Cookie 和本地存储。

独立需求预期在 `oracles/v1.json`、`oracles/v2.json`。用同一条“优先级 high 必须保留”的断言验证正常实现 Pass → 开启故障 Fail → 关闭故障 Pass；不能把实际 medium 改成新期望。切换实现为 40 时，V1 的 80 字接受用例应失败；只有需求明确变成 V2 后才维护为 40 字接受、41 字拒绝，并保留旧版本证据。

完整人工习惯提示词与多会话验收见 [验收场景](ACCEPTANCE.md)。这些场景只操作本地合成工单，不使用真实业务系统。

登录后可打开 `/chat.html` 进行合成数据问数。它用固定数据和确定性计算模拟多轮对话及 SSE 流，便于验证“页面对话 → 保留真实接口请求 → 提取会话标识 → 接口多轮回放 → 性能测试”，不调用外部模型，不代表真实模型准确率。
