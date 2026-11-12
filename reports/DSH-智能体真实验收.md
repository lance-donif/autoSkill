# DSH 智能体真实验收：未通过

日期：2026-11-12。Linux DSH Web，工作区 `/workspace/autoSkill`，权限保持 Workspace Write，没有切换完全访问权限。

已将当前默认模型切换为 `dddai / gpt-6.1-sol`，Base URL `https://api.dddai.dev/v1`，OpenAI Chat Completions 协议。密钥仅存本机 DSH 凭据库，权限 0600，没有进入仓库。

已在真实 DSH 会话提交任务：通过已安装的 autoSkill 正常读取 GitHub 仓库、搜索、刷新、断言，录制后用 Playwright 和 Selenium 回放；不登录、不发帖、不提交业务数据、不攻击。

持久会话中的技能目录确实发布了 `autoskill` 条目。实际在模型连接阶段失败，未执行任何工具调用。界面显示 AUTH / API key is invalid，但持久会话日志的底层原因是 `Proxy response (403) !== 200 when HTTP Tunneling`。这不能证明密钥无效；也没有证明智能体能够调用技能、通过默认沙箱或完成任务。

当前阻塞：Linux 运行环境的出站代理拒绝 `api.dddai.dev:443` 的 HTTPS 隧道。需要该环境能连接用户指定网关后，重新提交同样任务并核对项目、用例和回放记录。

此前通过后台服务直接运行 CLI 的 GitHub/npm 公网只读测试两种引擎均 2/2 通过，见 `公网只读访问验证.md`；这不能替代本项真实智能体验收。Windows 换包、Release 替换与合并 main 均未执行。

证据：`evidence/dsh-agent-gpt-6.1-sol/attempt.json`、`task-result.png`。
