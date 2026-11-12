# DeepSeek Harness 接入依据与验证边界

官方项目：https://github.com/deepseek-ai/deepseek-harness

## 插件做什么

只做两件事：把 `skills/autoskill` 注册为 DSH 技能；启动本机仪表盘，并在侧边栏注册两个面板（仪表盘、定时任务），面板内容是 `http://127.0.0.1:<端口>` 的页面。

仪表盘进程同时是「后台服务」：DSH 把智能体的命令放在沙箱里执行时，autoskill 命令会把自己转交给这个在沙箱外的进程执行（见 `skills/autoskill/references/commands.md`「在受限沙箱里」）。

插件不调用模型，不包含任何业务逻辑。用例的录制由智能体按 `SKILL.md` 通过命令行完成；回放和定时任务由 autoSkill 自己的 Python 进程执行，不经过 DSH 的会话或定时机制。

## 依据的接口

- `packages/skill/skill/src/index.ts`：`ctx.skills.register`、`SkillRegistration` 与 `resourceBase`。
- `packages/client/modules/README.md`：`package.json` 的 `dsh.client` 与 `./client` 导出。
- `packages/client/tsdown.client.ts`：`window.__ModuleLoader__.load({ id, factory })`。
- `packages/client/ui-schedule/src/client/index.ts`、`ui-layout/src/client/index.ts`：`main` 槽位与 `sidebar.panellist` 的注册方式。
- `packages/bundle/sdk-app/cordis.patch.yml`：Bundle patch 的 `insert` 格式。

## 已验证

`tests/plugin.test.mjs`（7 项）：技能注册信息、仪表盘启动参数（argv 方式、离线开关、不经过 shell）、缺少运行时时如实报告、Linux 本地 venv、客户端面板注册与地址、清单指向的文件存在。

## Linux DSH Web（2026-11-12）

在当前 Linux x64 安装官方 npm `@deepseek-ai/dsh@0.2.2-rc.1`，用 `link:/workspace/autoSkill` 安装插件，真实宿主启动仪表盘；在真实 Web 页面点击两个侧栏入口，确认 iframe 内仪表盘和定时任务页面显示正常。已选择 `/workspace/autoSkill` 工作区。

命令通过宿主启动的后台服务完成 `project list`、`record start`，受控登录页返回 `needs_login`。这是明确设置 `AUTOSKILL_BRIDGE=on` 的命令行验证，不是 DSH 智能体或默认沙箱验证。随后已配置并选中 HappyCoding 的 `deepseek-v4.1-flash`，但真实 DSH 连接检查被环境代理 CONNECT 403 拒绝，没有执行技能任务。

Linux 源码部署说明：[docs/Linux-DSH.md](../docs/Linux-DSH.md)。证据：[reports/evidence/linux-dsh/](../reports/evidence/linux-dsh/)。当前工作区已移除站点白名单和代理围栏，原报告中的网络拦截结论属于旧版本。百度实测受当前环境系统代理 403 阻断，不能声称通过。

## 真实宿主中的情况（2026-11-12，DSH 桌面版）

DSH 以 `link:` 方式安装本地目录并调用了插件入口：安装阶段通过；启用阶段因所选目录没有运行时而失败（已修正包的目录结构和报错文字）。改选完整包目录重装后启用成功：DSH 的操作记录为 applied，仪表盘进程由 DSH 启动并使用包内运行时，使用者确认可用。

## 智能体在 DSH 沙箱里调用技能（2026-11-12）

使用者在 DSH 桌面版里让智能体用这个技能测试一个公网站点，没有成功：沙箱里的命令写不了技能自己的数据目录（`project list` 直接以退出码 1 报数据库错误），写不了 `~/.agent-browser`，Chrome 起不来，HTTPS 不通；给了完全访问权限之后，又因为目标页面的脚本要 28 秒才下载完，超过浏览器工具固定的 25 秒等待而报错。

针对这次记录做的修改：受限进程把命令转交给后台服务；所有失败都以 JSON 输出；页面加载等待改为最长 90 秒且可配置；首个 snapshot 等页面画出元素；跳到登录页时明确提示。

**这些修改没有在 DSH 的沙箱里实测。** 开发侧的验证方式是：让一个进程无法创建 `.agent-browser` 目录、或无法写数据目录，确认它把整套录制与回放交给了后台服务并成功。DSH 沙箱是否允许访问本机回环端口，开发侧没有办法确认；使用者的记录显示沙箱里普通 HTTP 是通的。

## 未验证

以下没有由开发侧逐项实测：面板在 DSH 界面内的显示细节、智能体在 DSH 沙箱内对技能的调用（见上一节）、热更新、其他 DSH 版本的兼容性。

仪表盘面板使用浏览器本机的回环地址；远程访问 DSH Web 时，还需将仪表盘端口（默认 8787）转发到浏览器所在机器的相同端口。
