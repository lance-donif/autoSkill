# 常见情况怎么处理

| 现象 | 原因 | 做法 |
| --- | --- | --- |
| `当前命令运行在受限沙箱里…` | 宿主把命令关在沙箱里，而 autoSkill 后台服务没在运行 | 请用户在 DeepSeek Harness 里启用 autoSkill 插件，或在自己的终端运行 `autoskill.cmd dashboard` 并保持窗口不关；然后原样重试。不要换数据目录、不要改 `USERPROFILE` |
| `…后台服务是旧版本，不能代为执行命令` | 后台服务还是升级前启动的那一个 | 请用户重启它：DSH 里停用再启用 autoSkill 插件，或关掉再运行 `autoskill.cmd dashboard` |
| `浏览器没有启动成功…` | 多半是沙箱拦住了启动进程、证书或用户目录 | 确认后台服务在运行，把环境变量 `AUTOSKILL_BRIDGE` 设为 `on` 后重试（强制交给后台服务执行）；或请用户给当前会话完全访问权限。不在沙箱里时运行 `autoskill doctor --launch` |
| `record start` 返回 `needs_login` | 页面上有密码框，要先登录 | `record cancel`，用 `--as-login` 录一次登录，再重新开始 |
| `notes` 提示页面加载用了几十秒 / 仍未完全加载 | 页面本身慢（例如脚本文件很大） | 不是故障，可以继续；元素没出齐时再 `autoskill browser snapshot`。需要更久：`autoskill project update <项目> --page-load 180` |
| 回放结果「页面没有在限定时间内加载完成」 | 页面比等待时间还慢 | `autoskill project update <项目> --page-load <秒>`（默认 90，最大 600） |
| `读写 autoSkill 数据失败…` | 数据目录写不进去（权限、磁盘、被占用） | 错误里有具体原因；标准错误输出里有详细信息，如实转告用户 |
| `当前没有进行中的录制` | 还没 `record start`，或上一次已经 `stop` / `cancel` | `autoskill record start <项目>` |
| `已有进行中的录制` | 上一次录制没有结束，浏览器还开着（浏览器已经关掉的旧录制会被自动丢弃） | `autoskill record status` 看内容；要保留就 `record stop --title …`，否则 `record cancel` |
| PowerShell 下 `页面上找不到 <要输入的文字>` | `@e5` 没加引号，被 PowerShell 当成语法吞掉，参数错位 | 写成 `"@e5"` |
| `引用 @eN 已失效` | 页面变了，编号是旧 snapshot 的 | 用上一条命令返回的 snapshot，或 `autoskill browser snapshot` |
| `页面上找不到 …` | 元素还没出现、在弹层 / 折叠区里，或目标写法不匹配（`text=` 需完整文字一致，`testid=` 需元素有该属性） | `autoskill browser snapshot --all` 找到 `@编号` 直接用；或 `autoskill browser describe @e5` 看可用写法；元素未出现时先 `autoskill browser wait <选择器>` |
| `断言现在不成立，没有记录` | 期望值和页面实际不同 | 报错里有页面实际值；确认业务上哪个对，再改期望。不要删断言凑通过 |
| `地址 … 不在项目授权范围内` | 跳到了项目地址以外 | 确属被测系统的一部分：`autoskill project update <项目> --allow-origin <地址>`；否则不要访问 |
| 页面打不开、一直空白 | 地址不可达，或页面依赖的其他服务未授权 | `autoskill browser get url` 确认；检查网络与 `--allow-origin` |
| 证书错误 | 内网自签名证书 | `autoskill project update <项目> --insecure-tls on` |
| `缺少凭据 XXX` | 用例引用的凭据本机没有 | `autoskill secret set XXX <值>`（值向用户要） |
| `自动登录没有成功，录制未开始` | 登录用例失败：密码变了、页面改了、需要验证码 | `autoskill login <项目> --force` 看具体失败原因；页面改了就用 `--as-login` 重新录一次登录 |
| 回放结果「阻塞：前置用例未通过」 | 它依赖的用例（通常是登录）没通过 | 先解决前置用例；这条本身没有被执行 |
| 回放结果「定位已降级」 | 首选定位失效，备选定位命中 | 用例仍然有效；页面结构改过，建议重新录制这一条 |
| 回放失败但录制时成立 | 断言依赖会变的数据（序号、时间、已有数据条数） | 重新录制，断言改成不随数据变化的内容（包含刚输入的文字、状态标签等） |
| `项目未允许「删除数据」类操作` | 高风险操作默认关闭 | 用户同意后 `autoskill project update <项目> --approve write --approve delete` |
| `api_traffic` 是 `resource` | 页面没有调用业务接口 | 正常情况（纯前端页面）。照实说明：接口用例是资源可访问性检查 |
| 接口用例回放 401 | 令牌过期且没有登录用例可以续 | 用 `--as-login` 录一次登录，接口用例会自动依赖它 |
| `bundled runtime not found` | 拿到的不是完整离线包 | 向提供者要完整包；不要尝试安装 |
| `没有找到随包的 agent-browser` | 同上 | 同上 |

## 录制质量自查

结束录制前确认：

1. 标题说的是业务含义（「新建高优先级工单」），不是操作过程（「点击按钮」）。
2. 至少一条断言验证了业务结果。
3. 输入的测试数据是合成的，没有真实个人信息。
4. 密码类内容用的是 `secret:名称`。
5. `record stop` 之后 `autoskill run <项目> --case <新用例>` 回放通过。

## 汇报结果时

- 分开说通过、失败、阻塞的数量；阻塞不是通过。
- 失败要给出原因（命令输出里的 `detail`）。
- 没有回放过的用例说「未执行」。
- 页面没有业务接口时，说明接口用例是资源可访问性检查。
