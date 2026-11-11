# DSH接入依据与验证边界

官方项目：https://github.com/deepseek-ai/deepseek-harness

已核对的接口：
- `packages/skill/skill/src/index.ts`：`ctx.skills.register`、SkillRegistration及resourceBase。
- `packages/client/modules/README.md`：package.json的dsh.client/platform/inject与./client导出。
- `packages/client/tsdown.client.ts`：`window.__ModuleLoader__.load({id,factory:(require)=>...})`，工厂返回module.exports。
- `packages/client/ui-schedule/src/client/index.ts`：main keyed槽位与sidebar.panellist注册模式。
- `packages/client/ui-layout/src/client/index.ts`：主面板键与layout服务。
- `packages/bundle/sdk-app/cordis.patch.yml`：Bundle patch的insert格式。
- `packages/sdk/client/README.md`、src/types.ts：DeepSeekHarness、cwd/profile与run/close。

本插件不把AI任务注册为DSH原生reminder：原生Schedule的投递目标是Agent Session，不能据此实现“无AI的脚本任务”。autoSkill使用自己的SQLite调度器，按任务engine分发AI或脚本，后台生命周期由DSH插件管理。这一选择避免把脚本任务也交给模型执行。

已执行：纯Node插件契约测试（真实构建产物、Skill目录与槽位注册）和Python后台/执行器测试。
未执行：用户已安装DSH版本中的实际加载、HMR、模型与审批端到端流程，以及受当前系统进程限制阻断的真实Chromium验收。不会以“Bundle已打包”代替这些验收。
