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
| 不确定某个上游命令能否回放 | 原生执行和可移植用例是不同的能力 | `autoskill capabilities <命令>` 或 `browser help <命令>`；先看两种引擎/导出布尔值与前提，查询无需录制/运行时 |
| `replayable=false`、`native_step` 或脚本导出阻塞 | 此原生命令/额外选项执行过，但缺少完整的双引擎与导出语义 | 保留原因；重录为能力表里的可移植语法，或如实报告不支持。不要删除步骤凑通过 |
| 原生命令要求先 `record stop` / `record cancel` | 命令会换浏览器/会话配置、关闭录制或接管 HAR | 保留则 stop、放弃则 cancel，再执行并重新录制；不能在原生 batch 里绕过 |
| `browser mcp` 返回 `executed=false` | stdio 服务需要 MCP 客户端持续维护输入输出 | 用返回的 command 配置客户端，或 `browser mcp call <工具> <JSON>` 执行一次；描述符不代表操作已执行 |
| 视频提示缺少 ffmpeg | 原生视频编码需要本机 ffmpeg | 说明缺依赖；其他浏览器/截图/PDF 功能不依赖它，不联网安装 |
| trace / profiler 提示正在占用 | 两者使用同一个 CDP Tracing 管道 | 先 stop 当前捕获，再 start 另一种；结束业务录制时会收尾已有捕获 |
| 弹窗恢复后用例标成不可移植 | 先触发了原生弹窗，之后才选择处理方式 | 重录：先 `browser dialog accept [文字]` 或 `dismiss`，再执行触发操作；选择会按记录回放 |
| AI 回答还没结束就断言失败 | 普通元素等待预算短，或局部文字先出现 | 断言完成标志；用 `check ... --timeout <秒> --stable-for <秒>`；browser wait 的对应选项单位为毫秒。项目可设 `--assertion-timeout` |
| SSE/语音对话 HTTP 成功但没有结论 | 状态码不代表流式会话完成 | 用 `api capabilities` 查看协议合同；增加 text/reasoning/结束事件、消息数、音频字节/帧等实际断言，配置总超时、首事件与空闲预算 |
| 测试麦克风文件无效/找不到 | 文件不是有效的项目内非空 16-bit PCM WAV，或包里缺资源 | `project update <项目> --audio-input <WAV>` 重新验证并复制；单/双声道 8–192 kHz。分享时带项目资源；`off` 清除 |
| context / plan show 只显示 30 条 | 默认有界摘要避免几千条用例占满上下文 | 看 total/has_more/next_offset；用 `--query`、`--module` 筛选、`--offset` 翻页，选中后 `cases ... --case` 或 `plan show --case ... --details` |
| 知识正文没有全部返回 | context/search 返回的是片段，show 默认只读 4000 字符 | 先 `knowledge search` 找文件，再 `knowledge show <项目> <文件名> --offset <字符偏移>`；不要一次把所有文档放进上下文 |
| knowledge add 缺少来源/引用不属于项目 | 没有可追踪依据，或引用了另一个项目的用例/运行 | 给出文档/用户确认来源，或同项目 --case/--run；失败运行不会自动生成业务规则 |
| export scripts 列出 API 前置用例并拒绝 | 独立页面脚本暂不支持执行接口前置链 | 在 autoSkill 用 run 执行完整混合链；不要删前置步骤导出一个行为不完整的脚本 |
| export postman 列出 SSE/WebSocket 并拒绝 | 流式/双向协议不能转换成等价的普通 HTTP 集合 | 用 autoSkill run 回放完整协议用例；不要静默删掉流式用例或把状态码当对话断言 |
| `已有进行中的录制` | 上一次录制没有结束，浏览器还开着（浏览器已经关掉的旧录制会被自动丢弃） | `autoskill record status` 看内容；要保留就 `record stop --title …`，否则 `record cancel` |
| PowerShell 下 `页面上找不到 <要输入的文字>` | `@e5` 没加引号，被 PowerShell 当成语法吞掉，参数错位 | 写成 `"@e5"` |
| `引用 @eN 已失效` | 页面变了，编号是旧 snapshot 的 | 用上一条命令返回的 snapshot，或 `autoskill browser snapshot` |
| `页面上找不到 …` | 元素还没出现、在弹层 / 折叠区里，或目标写法不匹配（`text=` 需完整文字一致，`testid=` 需元素有该属性） | `autoskill browser snapshot --all` 找到 `@编号` 直接用；或 `autoskill browser describe @e5` 看可用写法；元素未出现时先 `autoskill browser wait <选择器>` |
| `断言现在不成立，没有记录` | 期望值和页面实际不同 | 报错里有页面实际值；确认业务上哪个对，再改期望。不要删断言凑通过 |
| 旧版本报 `地址 … 不在项目授权范围内` 或代理围栏错误 | 旧进程仍在限制站点 | 更新后重启插件/仪表盘并取消旧录制、重新开始；新版本不再限制站点 |
| 页面打不开、一直空白 | 地址、证书或系统网络有问题 | `autoskill browser get url` 确认；检查系统网络与证书。autoSkill 不再注入代理围栏 |
| 证书错误 | 内网自签名证书 | `autoskill project update <项目> --insecure-tls on` |
| `缺少凭据 XXX` | 用例引用的凭据本机没有 | `autoskill secret set XXX <值>`（值向用户要） |
| `自动登录没有成功，录制未开始` | 登录用例失败：密码变了、页面改了、需要验证码 | `autoskill login <项目> --force` 看具体失败原因；页面改了就用 `--as-login` 重新录一次登录 |
| 回放结果「阻塞：前置用例未通过」 | 它依赖的用例（通常是登录）没通过 | 先解决前置用例；这条本身没有被执行 |
| 回放结果「定位已降级」 | 首选定位失效，备选定位命中 | 用例仍然有效；页面结构改过，建议重新录制这一条 |
| 回放失败但录制时成立 | 断言依赖会变的数据（序号、时间、已有数据条数） | 重新录制，断言改成不随数据变化的内容（包含刚输入的文字、状态标签等） |
| 接口用例 `期望 200，实际 202/403` 而页面用例通过 | 站点的反爬网关只认真实浏览器，普通 HTTP 请求被挡 | 这类公网站点以页面用例为准；内网系统不会遇到 |
| 回放时报「名称已存在」或断言到旧数据 | 用例每次都新增同一个名字 | 录制时输入写成 `"名称-{{RAND}}"`，每次回放自动换一个 |
| 验证码识别错误 | 文字验证码偶尔会读错 | 回放会自动换图重试 3 次；滑块、点选、算式不支持 |
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
- `capabilities` 的声明与 `verified=false` 不是通过证据；管理命令返回的执行描述符也不算已执行。
- 页面没有业务接口时，说明接口用例是资源可访问性检查。
