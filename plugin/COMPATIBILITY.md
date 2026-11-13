# DeepSeek Harness 接入依据与验证边界

官方项目：https://github.com/deepseek-ai/deepseek-harness

## 插件做什么

把 `skills/autoskill` 注册为 DSH 技能，启动本机仪表盘，在侧边栏注册两个面板（仪表盘、定时任务），并将工作区及宿主上下文传给技能。面板内容是 `http://127.0.0.1:<端口>` 的页面。

仪表盘进程同时是「后台服务」：DSH 把智能体的命令放在沙箱里执行时，autoskill 命令会把自己转交给这个在沙箱外的进程执行（见 `skills/autoskill/references/commands.md`「在受限沙箱里」）。

`host-adapter.mjs` 还负责处理持久 `host_outbox`：将定时维护或独立 AI judge 工作交给指定 DSH 会话，保存投递意图，调用宿主 prompt，确认 flush 并回传收据。未知投递先查询宿主证据，不盲目重发。消息送达与 workflow/业务完成分开记录。

插件不另配模型 SDK 或密钥，AI 推理由 DSH 宿主承担。录制由智能体按 `SKILL.md` 调用 CLI；确定性 UI/API/原生执行由 Python 进程处理。autoSkill scheduler 是唯一时钟，DSH adapter 不创建第二份周期计划，但维护和判定任务会进入 DSH 会话。宿主需正在运行，这不包含唤醒电脑或启动已退出的 DSH。

## 依据的接口

- `packages/skill/skill/src/index.ts`：`ctx.skills.register`、`SkillRegistration` 与 `resourceBase`。
- `packages/client/modules/README.md`：`package.json` 的 `dsh.client` 与 `./client` 导出。
- `packages/client/tsdown.client.ts`：`window.__ModuleLoader__.load({ id, factory })`。
- `packages/client/ui-schedule/src/client/index.ts`、`ui-layout/src/client/index.ts`：`main` 槽位与 `sidebar.panellist` 的注册方式。
- `packages/bundle/sdk-app/cordis.patch.yml`：Bundle patch 的 `insert` 格式。
- DSH `0.2.2-rc.1` 的 `SessionController.resolveAgent/create/prompt`、会话 inbox/events 与 `sessions.flush`：工作会话定位、持久投递及恢复依据。实际调用封装见 `plugin/host-adapter.mjs`。

## 本轮服务与适配器验证（2026-11-13）

`tests/plugin.test.mjs` 9 项与 `tests/host-adapter.test.mjs` 7 项通过：技能注册、启动参数、运行时/面板、工作区传播，以及 prompt/flush、投递日志、重试和未知状态恢复。DSH 是 SDK 形状的替身，没有把测试替身当作真实宿主。

`tests/test_host_outbox_loop.py` 通过真实本机 HTTP outbox、JS adapter、CLI/bridge judge 和 SQLite 回执形成闭环：两任务/宿主会话分别保留 Fail 与 Pass，重复 poll 不重复投递。它验证服务边界，不证明 DSH 真机中的模型能自主完成本轮新 workflow。

v2.2.1 全项目回归：Python 486 项通过、无失败或跳过，Node 16 项和三项静态检查通过，见[整体回归](../reports/整体回归-v2.2.1.md)。详细方法、自然提示词验收与脱敏证据见[系统化技能与多轮对话验证](../reports/系统化技能与多轮对话验证.md)。

## 升级后的内嵌页面缓存（v2.2.2）

使用者确认过：外部浏览器 8787 显示新版 Apple UI，DSH 显示旧 UI，清缓存后恢复。旧实现只有 API 禁止缓存，HTML/CSS/JS 使用固定 URL，不能根据包内容正确就推断已加载的 WebView 页面已经更新。

v2.2.2 每次插件激活生成新的 iframe 加载标识，同次普通重渲染保持 URL 不变；服务端以整个 UI 的内容 hash 为 CSS/JS 和模块依赖统一命名空间，入口及静态资源不存储缓存。真实 Chromium 在同一上下文保留旧缓存的验收通过，Cookie 和 localStorage 保留；详见[升级页面缓存验证](../reports/升级页面缓存验证.md)。

DSH 0.2.2-rc.1 的 ModuleLoader 会缓存已加载的模块，替换时先失效/预取再刷新，服务端 entry revision 使用文件时间信息。升级仍需宿主重新加载新版插件模块，或重启 DSH；已经运行的旧客户端不会凭空获得新逻辑。本轮验证使用真实浏览器和产品服务/客户端、最小 SDK 形状宿主，没有宣称新版缓存机制已在真实 DSH WebView 完成实机验收。

## 对话框原生任务进度与报告交付

固定核对 DSH `0.2.2-rc.1` 的官方源码：输入框上方的 TodoDock 由 `todo_write` 的完整 `todos` 数组驱动，每项包含 `content/status`；新一轮 `turn/start` 会清空当前清单，恢复任务需要按真实状态重新写入。`/plan` 和 `exit_plan_mode` 是需审核的规划模式，不用于 autoSkill 的自动执行进度。

插件为入口 Skill 和定时投递加入共同指引，要求宿主通过本轮实际可用工具更新待办，Excel、简短 Markdown 与 Bug Markdown 生成、读回核验后再用 `present` 声明三个文件交付。`present` 仅记录当前宿主能访问的普通文件路径，不复制文件，卡片可能在本轮结束后显示。任务报告与执行结果按各自任务保存；旧版两份报告完成补齐前不能假称第三份已交付。

Node 检查覆盖指引注入、用户用例 Skill 保留与原有宿主适配合同；不是 DSH 真机模型调用或原生卡片验收。GoFile 直连此前返回代理 403，随后已从用户提交的 `test-cases.rar` 读取完整 Skill，随包注册为 `test-cases`；用户可替换的 `autoskill-cases` 仍是独立的具体用例目录。通用 Skill 和离线渲染不依赖 DSH。详细协议、源码与待验收项见 [DSH 对话进度与 Excel 交付](../docs/DSH对话进度与Excel交付.md)。

## Linux x64 / ARM64 与统信 UOS

Release 提供 Windows x64、Linux x64、Linux ARM64 三个包，UOS ARM64 使用 Linux ARM64 包。插件按 CPU 选择 `vendor/linux-x64/python/bin/python3` 或 `vendor/linux-arm64/python/bin/python3`；源码部署可用本地 `.venv`。Linux / UOS 先运行 `sh setup-linux.sh`。Node / DSH 为宿主依赖，不包含在技能包中。

agent-browser、Playwright、Chrome for Testing 均有 Linux ARM64 程序；Selenium 4.38.0 通过显式 chromedriver Service 使用 ARM64，避免调用旧 Selenium Manager。官方依据和系统库要求见 [平台与第三方支持](../docs/平台与第三方支持.md)，安装见 [Linux-DSH](../docs/Linux-DSH.md)。

当前 Debian 13 x64 的两种浏览器本机启动通过。ARM64 原生 runner 只组装和检查包，没有 UOS ARM64 真机或 UOS DSH 验收；默认沙箱智能体也仍未验收。不能把 Ubuntu ARM64 组装成功写成 UOS 已通过。

## 历史 Linux DSH Web 验证（2026-11-12）

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

2026-11-12 已在真实 Linux DSH Web 打开两个面板，不再属于“完全没有真实宿主验证”。未完成的是：默认沙箱中的智能体技能全链路，本轮定时维护/独立 judge/动态多轮的真实模型驱动闭环，热更新及其他 DSH 版本兼容性。桌面版显示细节也未逐项复核。

Windows/UOS 未做本轮新功能实机验收。历史 Linux 升级器专项见[升级保留数据验证](../reports/升级保留数据验证.md)，该升级方式已移除；当前采用外置完整数据目录，不能用旧方案通过记录替代现行数据迁移或其他平台验收。

仪表盘面板使用浏览器本机的回环地址；远程访问 DSH Web 时，还需将仪表盘端口（默认 8787）转发到浏览器所在机器的相同端口。

## 原生工具、任务卡与外置数据（2026-11-13）

插件注册 `autoskill_read`、`autoskill_task` 原生工具、只读 pre-step 摘要、会话任务绑定和持久请求回执。任务卡、侧栏和输入区任务入口使用当前会话，取消响应未知时先查询同一请求；报告下载先核验对应任务的三份文件。仪表盘地址由已认证宿主元数据提供，已在真实 Linux DSH Web 中核对非默认端口、双会话隔离、取消恢复和报告入口。

上述界面验收使用真实 DSH 0.2.2-rc.1、真实浏览器及本地协议模型夹具。它验证宿主交互与工具链，未验证远程模型自主执行；远程模型请求被当前环境代理 CONNECT 403 拒绝。实际 SDK 的工具/PTC、命令、路由、pre-step 和 present 另有专项，记录见 `reports/evidence/2026-11-13-dsh-native/`。

程序目录与数据目录独立，默认 Windows `%USERPROFILE%\autoSkill-data`、Linux/UOS `$HOME/autoSkill-data`；插件 `workspace` 或 `AUTOSKILL_WORKSPACE` 显式配置优先。插件等服务确认实际数据根后注册该目录中的用例 Skill。更新程序继续复用这一目录，不运行升级脚本。旧数据首次通过内置「数据管理」导入到空目标，然后配置 `workspace` 并重新加载插件；导入回执不代表当前宿主已经切换。详细操作见[升级与数据保留](../docs/升级与数据保留.md)。
