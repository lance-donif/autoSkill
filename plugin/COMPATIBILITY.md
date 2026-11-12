# DeepSeek Harness 接入依据与验证边界

官方项目：https://github.com/deepseek-ai/deepseek-harness

## 插件做什么

只做两件事：把 `skills/autoskill` 注册为 DSH 技能；启动本机仪表盘，并在侧边栏注册两个面板（仪表盘、定时任务），面板内容是 `http://127.0.0.1:<端口>` 的页面。

插件不调用模型，不包含任何业务逻辑。用例的录制由智能体按 `SKILL.md` 通过命令行完成；回放和定时任务由 autoSkill 自己的 Python 进程执行，不经过 DSH 的会话或定时机制。

## 依据的接口

- `packages/skill/skill/src/index.ts`：`ctx.skills.register`、`SkillRegistration` 与 `resourceBase`。
- `packages/client/modules/README.md`：`package.json` 的 `dsh.client` 与 `./client` 导出。
- `packages/client/tsdown.client.ts`：`window.__ModuleLoader__.load({ id, factory })`。
- `packages/client/ui-schedule/src/client/index.ts`、`ui-layout/src/client/index.ts`：`main` 槽位与 `sidebar.panellist` 的注册方式。
- `packages/bundle/sdk-app/cordis.patch.yml`：Bundle patch 的 `insert` 格式。

## 已验证

`tests/plugin.test.mjs`（5 项）：技能注册信息、仪表盘启动参数（argv 方式、离线开关、不经过 shell）、缺少运行时时如实报告、客户端面板注册与地址、清单指向的文件存在。

## 真实宿主中的情况（2026-11-12，DSH 桌面版）

DSH 以 `link:` 方式安装本地目录并调用了插件入口：安装阶段通过；启用阶段因所选目录没有运行时而失败（已修正包的目录结构和报错文字）。改选完整包目录重装后启用成功：DSH 的操作记录为 applied，仪表盘进程由 DSH 启动并使用包内运行时，使用者确认可用。

## 未验证

以下没有由开发侧逐项实测：面板在 DSH 界面内的显示细节、智能体对技能的调用、热更新、其他 DSH 版本的兼容性。

仪表盘面板要求浏览器与 DSH 宿主在同一台机器；远程访问 DSH Web 时面板无法显示。
