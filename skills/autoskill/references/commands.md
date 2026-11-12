# autoSkill 命令参考

所有命令输出一个 JSON 对象；`"error"` 表示没有成功，文字说明原因和下一步。退出码：0 成功，1 有用例失败，2 阻塞或用法错误，3 找不到随包运行时。

## 项目

| 命令 | 说明 |
| --- | --- |
| `project create --name 名称 --url 地址 [--category 分类] [--id 标识]` | 新建项目。地址即授权范围；标识默认由名称或主机名生成 |
| `project list` | 全部项目及用例数、缺少的凭据 |
| `project show <项目>` | 单个项目的设置 |
| `project update <项目> [--name] [--category] [--allow-origin 地址]… [--approve 操作]… [--timeout 秒] [--insecure-tls on\|off]` | 修改设置。`--approve` 可取 `write` `delete` `payment` `refund` `message`，给出的列表会替换原有列表（默认只有 `write`） |
| `context <项目>` | 登录方式、已保存的登录态、缺少的凭据、用例清单、知识库、建议的下一步 |
| `cases <项目>` | 用例清单 |

## 凭据与登录

| 命令 | 说明 |
| --- | --- |
| `secret set 名称 [值]` | 保存凭据到本机（Windows 用户级加密）。省略值时从标准输入读一行 |
| `secret list` / `secret delete 名称` | 只列名称，不显示值 |
| `login <项目> [--force]` | 执行登录用例并保存登录态；`--force` 忽略已保存的登录态 |

用例里的凭据写作 `${ENV:名称}`；录制时在命令行写 `secret:名称`。没有保存时回退到同名环境变量。

## 录制

| 命令 | 说明 |
| --- | --- |
| `record start <项目> [--as-login] [--no-login] [--url 地址]` | 打开浏览器并开始录制。有登录用例时先自动登录；`--as-login` 表示录的就是登录；`--no-login` 以未登录状态开始 |
| `browser <命令> …` | 操作页面并记录，见下表 |
| `check <类型> [目标] [期望] [--attribute 属性]` | 当场验证并记录断言 |
| `record status` | 已记录的步骤 |
| `record undo` | 撤销最后一步的记录 |
| `record stop --title 标题 [--module 模块] [--category 分类]` | 结束并生成用例 |
| `record cancel` | 放弃，不生成用例 |

同一时间只能有一个录制。

### browser 子命令

记录为步骤：`open <地址或路径>` `reload` `click` `dblclick` `hover` `fill <目标> <文字>` `type <目标> <文字>`（逐字输入，不清空）`select <目标> <value>` `check` `uncheck` `press <按键>`。

只查看、不记录：`describe <目标>`（列出该元素可用的定位写法，含 testid）`snapshot`（只列可交互元素）`snapshot --all`（含标题、表格等全部内容） `get text|html|value|url|title|count <目标>` `get attr <目标> <属性>` `is visible|enabled|checked <目标>` `wait <选择器或毫秒>` `screenshot [路径]` `scroll <方向> [像素]`。

目标：`@e编号`（来自最近一次 snapshot）或 CSS 选择器。按键名如 `Enter` `Escape` `Tab` `Control+a`。

不可用：安装、升级、连接外部浏览器、代理、网络与登录态相关命令——这些由 autoskill 统一管理。

### 录下来的定位方式

每一步保存多个候选定位，回放时按顺序尝试：测试标识（`data-testid`）→ 标签文字 → 占位提示 → 角色+名称 → CSS。首选失效而备选命中时用例仍通过，结果里标注「定位已降级」，提示页面改了。

### check 类型

`visible` `hidden` `text` `contains` `value` `checked` `count` `attribute`（需 `--attribute`）需要目标；`url` `url_contains` `title` 不需要。目标可写 `@e5`、`testid=…`、`text=…`、`label=…`、`placeholder=…`、`role=角色:名称` 或 CSS 选择器；`count` 的目标用 CSS 选择器。

## 回放

`run <项目> [--case 编号]… [--engine playwright|selenium] [--kind ui|api]`

- 浏览器默认在后台运行、不显示窗口；加 `--headed` 显示窗口并在每步之间稍作停顿（`record start`、`login` 同样支持；`dashboard --headed` 让从仪表盘发起的回放都显示窗口）。
- 前置用例自动先执行且每次运行只执行一次；前置没通过时，依赖它的用例记为阻塞。
- 登录用例：有保存的登录态且仍有效就直接复用，否则重新登录；回放中途发现登录过期会重新登录一次，只读用例自动重试，写操作不自动重发。
- 没有前置的接口用例不带任何登录信息发送（用来验证「未登录被拒绝」）。
- 接口用例之间的变量：`{{名称}}` 由前面用例从响应里提取。

失败归类：找不到元素、结果与预期不符、超时、地址未授权、缺少凭据、前置用例未通过、登录已过期、缺少前置数据、操作未被允许、环境问题。

## 导出与分享

| 命令 | 说明 |
| --- | --- |
| `export postman <项目> [--out 目录]` | Postman Collection v2.1 + Environment。按依赖顺序排列，含断言脚本和变量传递；凭据是环境里的空变量 |
| `export scripts <项目> [--out 目录]` | 每条页面用例各一份 Playwright 和 Selenium 的独立 Python 脚本（含前置步骤），运行时不需要 autoSkill；凭据从同名环境变量读取 |
| `report <项目> [--out 目录]` | Excel（用例、步骤、断言、最近结果）和 Markdown 摘要 |
| `pack [项目]… [--out 路径] [--include-secrets --passphrase 口令]` | 分享包：用例、知识库、登录方式定义。不含登录态、运行记录、截图；凭据仅在 `--include-secrets` 时用口令加密带上 |
| `pack --full [--out 路径]` | 完整包：整个 Skill（运行时 + 全部项目与知识库），对方解压即用。不含 `workspace/.local`（凭据、登录态、运行记录、截图） |
| `import <分享包> [--passphrase 口令] [--overwrite]` | 导入轻量分享包。已有同名项目默认跳过 |

## 其他

| 命令 | 说明 |
| --- | --- |
| `dashboard [--port 8787]` | 启动仪表盘（只监听本机）。定时任务在仪表盘运行期间才会触发 |
| `demo` | 安装示例项目「示例工单系统」；启动仪表盘时会同时启动示例站点 |
| `doctor [--launch]` | 检查运行时；`--launch` 真实启动一次浏览器 |
| `verify` | 按清单校验随包运行时文件（完整离线包） |

## 文件放在哪

```
<Skill 目录>/workspace/
  projects/<项目>/        可以分享：project.json、qa-config.json、auth.json、testcases/、knowledge/
  .local/                 只在本机：凭据、登录态、运行记录、截图、报告
```

环境变量 `AUTOSKILL_WORKSPACE` 可以把工作区放到别处。
