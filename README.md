# autoSkill

**录一次，到处回放。** AI 操作网页时，autoSkill 同步录下页面动作和接口流量，编译成不需要 AI 的回放用例：页面用例可用 Playwright 或 Selenium 回放，接口用例走 HTTP，并可导出 Postman 集合和独立脚本。用例按项目归档，可以定时执行，结果在仪表盘里查看。

它是一个 Skill：任何能读文件、能执行命令的 AI 智能体都可以使用，不绑定某个平台。Windows x64 完整包自带全部运行依赖，内网离线可用。

## 它解决什么

| 以前 | 现在 |
| --- | --- |
| 每次回归都要 AI 重新理解页面、重新操作，慢且结果不稳定 | AI 只在第一次录制时参与；之后回放是确定性的脚本，不调用模型 |
| 页面测试和接口测试各写一套 | 录一次页面操作，同时得到页面用例和它背后的接口用例 |
| 每次都要提醒「先登录」 | 项目有登录用例后，录制和回放都自动先登录，登录态在本机复用，过期自动重登 |
| 用例散落在个人电脑上 | 用例、知识库、登录方式跟着 Skill 目录走，一条命令打包分享 |

## 目录

| 路径 | 内容 |
| --- | --- |
| `skills/autoskill/` | 产品本体：`SKILL.md`（给 AI 的使用说明）、命令行入口、应用代码、仪表盘、示例 |
| `skills/autoskill/references/` | 命令参考、常见问题、随包的 agent-browser 离线指南 |
| `skills/autoskill/examples/ticket-desk/` | 示例工单系统：带登录和业务接口的演示站点 |
| `plugin/` | DeepSeek Harness 插件：注册 Skill，并把仪表盘、定时任务放进侧边栏 |
| `scripts/` | 维护者用：下载运行时、组装离线包 |
| `tests/` | 自动化测试（真实浏览器、真实 HTTP、真实数据库） |
| `docs/`、`reports/` | 需求与验收清单、测试证据 |

## 使用（完整离线包）

解压后不需要安装任何东西。给人看的入口是仪表盘：

```bat
autoskill\autoskill.cmd dashboard
```

浏览器打开 <http://127.0.0.1:8787>。第一次使用点「加载示例项目」，再点「运行全部」，就能看到完整效果。

给 AI 用：把 `autoskill` 目录交给你的智能体，告诉它「使用这个目录里的 autoSkill 技能测试 <网址>」。它会阅读 `SKILL.md` 并按下面的流程工作：

```
autoskill project create --name "订单中心" --url http://10.0.0.8:8080
autoskill context <项目>
autoskill record start <项目>
autoskill browser click @e5
autoskill check contains testid=toast "已创建"
autoskill record stop --title "新建工单"
autoskill run <项目>
```

全部命令见 [`skills/autoskill/references/commands.md`](skills/autoskill/references/commands.md)。

自检：`autoskill.cmd verify`（按清单校验随包文件）、`autoskill.cmd doctor --launch`（真实启动一次浏览器）。

## 仪表盘

- **仪表盘**：默认汇总全部项目，可按项目和时间范围筛选；筛选同时作用于所有数字、图表和列表，并写入网址。每个指标旁有统计口径说明，点击进入同一筛选条件下的明细。
- **项目与用例**：单条运行、勾选批量运行、整个项目运行；可选 Playwright 或 Selenium。
- **执行记录**：实时进度、可取消、只重跑未通过的；每条用例展开可见逐步时间线、逐条断言（期望与实际）、请求与响应、结束时截图。
- **定时任务**：每天、每周、固定间隔、仅一次；可编辑、暂停、立即运行。仪表盘运行期间才会触发。

统计口径：通过率 = 通过 ÷（通过 + 失败）。阻塞（没有验证到结果）单独展示，不进分母；为满足依赖而自动执行的前置用例不计入。

## 分享

```
autoskill pack                       # 打包全部项目：用例、知识库、登录方式定义
autoskill import <分享包>            # 对方导入
```

分享包不含凭据、登录态、运行记录和截图。对方首次使用时按提示设置凭据即可；团队共用测试账号时可加 `--include-secrets --passphrase <口令>`，凭据用口令加密后随包带走。

## 安全与离线

- 浏览器只能访问项目授权的地址，其他地址在浏览器层面不可达；访问范围、高风险操作（删除、支付等）按项目显式开启。
- 凭据和登录态只保存在本机，用当前 Windows 用户的 DPAPI 加密；用例、报告、导出文件里只有凭据名称。
- 仪表盘只监听 127.0.0.1，拒绝跨站写请求。
- 不联网安装，不检查更新，不上报数据。浏览器的后台联网、组件更新、崩溃上报均关闭。

## 已验证与未验证

实测结果、证据和仍未解决的问题见 [`reports/交付验证报告.md`](reports/交付验证报告.md)。没有真实运行过的项目不写成通过。

## 开发

需要本机 Python 3.12+ 和 `requirements.txt` 中的库，以及 Chrome for Testing 与 chromedriver（通过环境变量 `AUTOSKILL_CHROMIUM`、`AUTOSKILL_CHROMEDRIVER`、`AUTOSKILL_AGENT_BROWSER` 指定）。

```bash
python -m unittest discover -s tests -p "test_*.py"      # Python 测试
node --test tests/plugin.test.mjs                        # 插件契约测试
python scripts/fetch-windows-assets.py --assets <仓库外目录>            # 维护者：从官方源下载运行时
python scripts/build-windows-bundle.py --assets <目录> --output <新目录> --zip   # 维护者：组装离线包
```

完整离线包约 272 MB，超过 Git 单文件限制，作为 GitHub Release 附件发布，不进入仓库历史。
