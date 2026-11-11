# autoSkill 本轮交付与验证

日期：2026-11-11（北京时间）。目标仓库：lance-donif/autoSkill，main分支。

## 已实现

| 能力 | 入口 | 本轮验证 |
| --- | --- | --- |
| autoSkill主技能 | skills/autoskill/SKILL.md | 统一JSON、Schema、覆盖率、导出、项目Skill与结果契约测试 |
| 官方Agent Browser Skill | skills/agent-browser/ | 固定上游来源、许可证、资源路径与DSH注册测试；CLI为可选独立运行依赖 |
| UI/API用例存储 | save-cases / sync-skills | 快照、修订、内容哈希、防止旧结果冒认通过 |
| 脚本UI执行 | runtime/engines.py | 白名单动作/断言，20条TodoMVC脚本定义可读；真实浏览器前置未通过 |
| 脚本API执行 | 主引擎run-api | 原Selenium依赖API有4条真实HTTP通过记录；不需要模型 |
| AI执行/生成/解读 | plugin/ai-worker.mjs | 使用官方DSH SDK，生成数量和结构校验、结果完整性和存储测试；模型用假实现做契约验证 |
| 仪表盘及任务页 | ui/、runtime/service.py | 页面资源与实际路由集成测试；未完成浏览器像素验收 |
| DSH插件入口 | package.json / cordis.patch.yml / plugin/ / lib/client.js | 按官方源代码契约实现；3项插件注册契约测试；未在真实DSH宿主安装启用 |
| 定时任务 | SQLite调度器 | 间隔、每天/时区、一次、暂停恢复、重启与一次触发测试；AI和脚本分别分发 |
| 安装产物 | npm pack检查 | 必要文件齐全，排除Python缓存、环境文件和数据库 |

## 自动测试

`npm test`：44项Python测试和4项Node测试通过。

Python覆盖原11项基础回归、初测缺陷修复、Schema/凭据/引用校验、结果版本与迁移、独立执行通道、后台/定时任务、AI生成存储契约、原版TodoMVC来源与20条动作定义。

Node覆盖真实构建的DSH客户端闭包工厂、面板槽位、顶层Skill注册、参数验证和安装产物文件检查。

AI相关契约测试使用假SDK/假生成结果，未消耗或配置真实模型；它们不能证明已在用户的DSH版本和模型配置中端到端跑通。测试通过不等于UI测试用例通过。

## 真实执行结果与限制

- Selenium索引依赖的GitHub Tree API：此前4条独立HTTP用例均Pass，证据位于examples/selenium/execution/。
- Selenium UI：云浏览器显示Site Unavailable，未执行页面交互。
- 开源TodoMVC：原版源码与Git blob逐文件校验一致。20条UI定义可供脚本读取；当前云浏览器不可访问远端/回环页面，脚本Chromium因系统进程限制启动失败，桌面截图接口也不可用，未逐条执行。共同前置失败记录与Blocked状态保留在examples/todomvc/execution/。
- TodoMVC没有业务CRUD API；附带4条API仅针对autoSkill控制台自身。本轮未完成它们的独立网络执行，保持Not Run；不得与Selenium依赖接口的4条Pass混为一谈。
- DSH原生安装、HMR、已配置模型的AI执行和实际浏览器像素验收尚未完成。
- 插件面板当前使用同机回环后台；远程DSH Web需要另做认证传输桥，未宣称支持。

## 已修复的初测6项问题

1. 无约束响应Schema不再充当业务断言。
2. 明文认证字段及常见自定义Token/API Key字段在保存/导出前拒绝，允许完整环境变量引用且不展开导出。
3. 错误JSON容器返回校验错误，不产生未处理异常。
4. Pass/Fail绑定用例修订和内容哈希，修改后不能沿用旧通过结果。
5. 重跑先校验定义、归档旧结果，再执行；版本变更不再挡住归档流程。
6. 非法JSON Pointer转义在执行前拒绝。

额外检查阻止运行时响应Schema外部引用自动访问，保存无效Schema不再先污染权威数据。旧1.0.0定义通过保存入口显式升级为1.1.0，并保留快照；版本变化参与结果失效判断。

## 使用

安装步骤见plugin/INSTALL.md；本地预览使用npm run dashboard。先通过npm run doctor检查依赖，再导入示例并明确授权origin及操作范围。只有浏览器和DSH模型实际可用且完成相应执行，才能将真实UI/AI验收状态更新为通过。

接口执行还增加异常回归：每次重跑先清除当前旧结果（旧记录已归档），逐条持久化新结果；执行器异常不能使上一次Pass继续冒充本次结果。
