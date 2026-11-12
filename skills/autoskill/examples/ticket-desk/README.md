# 示例工单系统（Ticket Desk）

autoSkill 随附的演示站点，用来练习和验证「先登录 → 再操作 → 接口回放」。只用 Python 标准库，数据全部是合成数据并保存在内存里，重启即恢复初始 3 条工单。只监听 127.0.0.1。

启动：`python server.py --port 8790`，然后访问 <http://127.0.0.1:8790>。

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `qa_admin` | `Passw0rd!demo` | 管理员：可新建、可标记已解决 |
| `qa_viewer` | `Passw0rd!view` | 只读访客：只能查看 |

这两个账号只存在于本演示站点，不是任何真实系统的凭据。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 无需登录 |
| POST | `/api/login` | `{username,password}` → `{token,user}`，同时下发会话 Cookie |
| GET | `/api/me` | 当前用户；未登录 401 |
| POST | `/api/logout` | 退出 |
| GET | `/api/tickets?status=&q=` | 列表 `{items,total}` |
| POST | `/api/tickets` | 新建；标题必填且不超过 80 字，否则 422；访客 403 |
| GET / PATCH | `/api/tickets/{id}` | 详情 / 修改状态 |
| POST | `/api/test/reset` | 测试辅助：恢复初始数据并清空会话 |
| POST | `/api/test/expire-sessions` | 测试辅助：让所有会话立即失效 |

登录后页面把令牌放在 `localStorage`，请求用 `Authorization: Bearer`；服务端同时接受会话 Cookie。因此保存的登录态必须同时包含 Cookie 和本地存储。
