# 独立 UI 黑盒证据合集

仅使用公开文档/Schema、CLI、真实浏览器与公开 HTTP API；未读实现、内部测试或其他 Agent 结果。测试站为本目录自建 HTTP 站；没有真实 DSH 或模型宿主调用。

本档聚合多个候选和轮次的证据。末次只增量复测仪表盘paint与正常导航下的输入保存两项，不是重新执行全部10项；详情379ms与列表570ms沿用上一轮c44cbb…，仪表盘565ms为末次3ce7f69d…新测。

候选记录：`revalidation-version.json`（14:24:25 UTC，UI revision 12c55b…）；修复候选 `candidate-fixed-version.json`（14:35:30 UTC，UI revision 328192f4…）；刷新优化候选c44cbb…；末次dashboard bundle候选3ce7f69d…，前后hash一致。实际版本均为 2.2.4。

| 用户任务 | 当前结果 | 实测步骤与证据 |
| --- | --- | --- |
| 执行阶段与业务/性能/报告分别展示 | Pass | 真实 native 运行；HTTP200但答案错误时业务失败。`performance-ui-checks.json` 验证正确答案+50ms目标失败时，UI为已结束/业务通过/性能失败/综合失败/报告就绪。 |
| 列表、详情、统计一致 | Pass | 对每次真实执行比对统一state与计数；再比对summary与可见统计。`consistency-ui-results.json`、`statistics-ui-checks.json`。 |
| 前台在线完成后1秒内刷新 | Pass（跨轮证据） | 详情379ms、列表570ms沿用c44cbb…轮；仪表盘565ms为3ce7f69d…末次增量新测。均按公开finished_at至真实DOM条件后双rAF、门槛1秒。原2122.8/3558/3887/1344ms失败全部保留，见历史记录。 |
| 筛选、输入、焦点、选区保留 | Pass | 三项组合筛选保留沿用c44cbb…轮。末次3ce7f69d…单独从仪表盘就绪后点击「项目与用例」，等待cases可交互，再打开新建项目草稿；3字段、np-cat焦点和选区1～4在真实背景运行结束/报告生成后均不变，无控制台错误。`dashboard-bundle-input-results.json`。 |
| 当前与历史周期可区分 | Pass（修复后） | 当前范围排除旧周期执行，all包含旧执行；修复后行标历史周期，详情同时列旧/当前cycle ID。`candidate-fixed-historical-check.json`、`candidate-fixed-runs_run-e8c9e3e0d741.png`。修复前缺标记证据保留于`native-only-candidate/`。 |
| 报告失败可手动重试且不重跑 | Pass | 只删除自己导出Excel→failed；真实UI点重试→三文件ready、下载hash吻合、source_hash不变、执行ID/结束时间/结果/运行数量不变。`report-retry-ui-results.json`。 |
| 仅原生资产/执行也显示首页统计 | Pass（修复后） | 全新`native-only-data`，0条规范用例、1原生资产、1真实通过执行；首页显示执行1/通过1，无“还没有项目”。`candidate-fixed-native-only-results.json`与截图。早期失败证据保留。 |
| 工作流完成与业务通过分别表达 | Pass | 同一第二周期按公开schema真实执行，故意业务失败保持violated；诊断/知识提交后complete=true、phase=settled、business=fail、report=ready。`55～68`命令证据。早期“未知需求”障碍复验已消失。 |
| 真实 DSH 宿主联动 | Blocked | 当前没有真实 DSH 宿主能力；本地dashboard不算DSH验收。 |
| 真实录制器完整链路 | Blocked | 公开record start返回随包agent-browser缺失；公开runtime.recorder=false。使用真实HTTP原生断言完成其余状态任务。 |

故意配置的订单业务失败和SLA失败用于验证autoSkill状态表达，不计autoSkill产品Bug。首次脚本下载定位错误、加载未完成的筛选操作与未完成页面截图均保留，并从最终产品失败中排除；每次修正都有独立原始证据。

证据合集10项用户任务：8 Pass、0 Fail、2 Blocked。本轮只增量执行仪表盘与独立输入保存两项，2 Pass；其他条目使用对应候选和轮次证据，不表述为本轮全量重跑。已报告的功能缺口和时效失败均有后续通过证据；真实DSH与完整录制器链路继续Blocked。

328192f4…候选历史时效组于2026-11-13 14:42:23～14:44:27 UTC执行，前后hash不变。页面visibilityState=visible。详情/列表/输入组负载load 1.63→4.93，3核cgroup窗口平均CPU占65.9%；仪表盘组load 3.10→3.32、平均CPU占62.1%。这是当时主任务指定安静窗口下的实测负载，并非零负载假设。

- `final-quiet-realtime-ui-results.json`：详情run-d8ff626393c5，finished 14:42:37.111、两帧后paint 14:42:38.027，916ms；列表run-b15762cfbe8e，finished 14:42:59.084、paint 14:43:02.642，3558ms。所有筛选值与URL在更新前后相同。
- `final-quiet-dashboard-results.json`：run-269eaee298b8，finished 14:44:20.432、paint 14:44:24.319，3887ms。界面已正确清除当前规范用例失败并展示最新通过结果，功能完成但时效超限。
- 列表公开响应时间线：/api/state-revision从14:42:57.273到14:43:00.933之间相隔3.66秒；对应筛选列表响应14:43:00.771和14:43:02.522。仅记录可观察时间，不推断实现原因。

证据保留规则：早期2.12秒记录未覆盖；修复前空首页/历史缺标记证据位于`native-only-candidate/`，原生-only修复后截图为`candidate-fixed-native-only-dashboard.png`，历史修复后截图为`candidate-fixed-runs_run-e8c9e3e0d741.png`。

## 刷新优化候选复验

后端重启后公开UI hash为c44cbb…，测量边界仍为同一公开finished_at至DOM条件满足后双rAF；1秒门槛未改。主任务明确安静窗口中，详情379ms、列表570ms通过，仪表盘1344ms仍失败。三轴状态及列表/详情6项回归通过；组合筛选仍保留。原始文件为`optimized-quiet-realtime-ui-results.json`、`optimized-quiet-dashboard-results.json`、`optimized-quiet-critical-axes.json`。

仪表盘网络时间线独立保存于`optimized-quiet-dashboard-timeline.json`。相对finished_at：summary响应+702.1ms、runs+710.4ms，辅助Blocked/Fail结果列表+1138.2/+1187.4ms，双rAF+1344ms。paint条件不检查report ready；随后读取到报告generated_at为+846ms，该后读值不等于首次可见ready时刻。该组load0.67→1.20，3核cgroup平均CPU46.9%。

这一轮原脚本在点击运行行后、详情尚未就绪时再次立即切换#/cases，20秒内未找到新建按钮，草稿弹窗未打开，故新版输入保存复核未执行，不能记为输入丢失Bug。随后独立打开真实#/cases，DOM中有可见role=button「新建项目」，项目用例正常且无控制台错误；证据`optimized-cases-input-precondition.json/.png`。保留这条未满足前置的原始记录，后续以正常入口独立复核输入保存。

## 末次dashboard bundle增量复验

第三安静窗口仅执行2项，均Pass。候选公开UI hash为`3ce7f69d701edf33a8874993bd08f8dbe8ab404c28f12f38f7fb16a76aa3ea9c`，前后保持一致；本次没有重复详情/列表379/570ms已过样本，也没有重复全部10项。

- 仪表盘：同一原workspace、同一规范用例、同一失败→通过前置；run-19083cdd061a的公开finished_at为2026-11-13 15:15:09.552 UTC，真实DOM条件满足后双rAF为15:15:10.117 UTC，565ms，严格按1秒通过。条件依旧为当前仍失败减少1与最近首条显示已结束/综合通过/业务通过，未加入report ready条件。前台visible，load0.19→1.15，3核cgroup窗口平均CPU40.2%。完整证据：`dashboard-bundle-quiet-results.json`及`dashboard-bundle-quiet-before.png`/`dashboard-bundle-quiet-after.png`。
- 输入保存：独立正常UI导航后再打开草稿，等待真实背景运行run-6ed961d0991d结束及报告生成；三字段值、焦点np-cat、选区1～4前后完全一致，控制台错误为0。完整证据：`dashboard-bundle-input-results.json`及前后截图。此前未满足输入测试前置的记录继续保留，没有将其伪造为输入丢失Bug。

归档前的判定文件已另存`pre-bundle-revalidation-results.json`与`pre-bundle-revalidation-results.md`；1344/3558/3887ms及更早2.12秒的原始文件未覆盖。
