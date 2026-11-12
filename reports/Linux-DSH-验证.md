# Linux DSH 接入与网络限制修正验证

日期：2026-11-12。工作区 `/workspace/autoSkill`，分支 `fix/sandbox-and-slow-pages`，基线 `f6343a7` 加本次 Linux 适配与网络修正。

## 实际部署

- 官方 DSH npm `0.2.2-rc.1`；Node `24.22.1`；Python `3.12.14`；agent-browser `0.42.0`。
- Debian Chromium 与 chromedriver `156.0.8078.79`；Python venv 在仓库 `.venv`，Node 工具在仓库外 `/workspace/autoskill-linux-tools`。这是 Linux 源码部署，不是 Linux 离线完整包。
- DSH 用 `link:/workspace/autoSkill` 安装插件，实际启动了包内 CLI 对应的 Python 仪表盘。已在 DSH 页面选择 `/workspace/autoSkill` 工作区。
- 在真实 Web 页面点击了 autoSkill 仪表盘和定时任务，iframe 中显示正确页面。截图见 `evidence/linux-dsh/dashboard.png`、`schedules.png`、`workspace.png`。
- `doctor --launch`：Playwright、Selenium 均真实启动成功，依赖齐全；原始输出见 `evidence/linux-dsh/doctor.json`。源码部署没有 Windows 离线包清单，没有运行并宣称 `verify` 通过。

## 网络限制

按使用者要求，移除录制器和两种页面回放的 `--proxy-server` / `--proxy-bypass-list` 围栏，移除 Playwright 的站点拦截路由、service worker 禁用和浏览器/接口入口的域名白名单校验。旧 `allowed_origins` 保留配置兼容，不再限制站点访问。保留 URL 格式检查和既有业务操作审批。

真实浏览器回归覆盖：跨站导航、刷新、跨站资源请求；录制跨站动作并生成 UI 用例，Playwright 与 Selenium 均回放通过。这是本机两个不同端口站点间的验证。

**百度没有验证通过。** 当前环境的系统代理对 `https://www.baidu.com` 返回 CONNECT 403；普通 curl 同样失败，录制报 `net::ERR_TUNNEL_CONNECTION_FAILED`。不能据此承诺公网站点已经实测通过。应在网络可达环境中重新执行用户的百度搜索天气场景。

## Linux 后台转交

修复 Linux 特有的 500：后台转交与录制器原来共用 `agent-io`，录制器清理临时文件时会 unlink 后台转交仍在使用的输出文件（Linux 允许删除打开中的文件）。后台转交改用独立 `bridge-io`，并新增真实录制回归。

在 DSH 实际启动的后台服务上，显式设置 `AUTOSKILL_BRIDGE=on`，`project create`、`project list`、`record start` 和 `record cancel` 均退出码 0；受控登录页的 `record start` 返回 `needs_login`。原始记录见 `evidence/linux-dsh/bridge-smoke.json`。本次创建的临时测试项目已清理，最终项目列表为空且没有活动录制。

Linux 凭据和登录态改用 Fernet 加密，本地密钥权限 0600。原有非 Windows Base64 开发降级不再被当作加密数据读取；Windows DPAPI 逻辑保留。

## 未完成的验收

- DSH 已配置 HappyCoding 模型并提交一次仅回复 OK 的连接检查，但实际请求被环境代理 CONNECT 403 拒绝。没有在 DSH 默认沙箱中执行技能 CLI；上面的后台转交测试不等于真实沙箱验收。
- Windows 安装目录、新包替换、用户项目数据、GitHub Release 附件均未动；原先 happycoding.xyz/keys 的 DSH 沙箱验收仍待进行。
- 两份 Windows 项目记忆当前环境不可用。
- 没有合并或推送 main，没有更新 Windows zip 的大小或 SHA-256。旧交付报告中对网络围栏的验证描述属于旧版本，不适用于本次工作区。

部署与启动步骤见 `../docs/Linux-DSH.md`。

## 最终检查

- `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`：105 项，145.163 秒，全部通过，无失败、错误或跳过。输出中的 Fail/Blocked 是负向测试的预期结果，套件结论为 OK。
- `node --test tests/plugin.test.mjs`：7 项全部通过，无跳过。
- `git diff --check` 和两个 shell 入口语法检查通过。
- 原始输出：`evidence/linux-dsh/python-tests.txt`、`plugin-tests.txt`。

后续公网只读访问、接口代理兼容修正与最新检查见 `公网只读访问验证.md`；此前“最终项目列表为空”是当时状态，当前保留了本次成功的 `public-web-readonly` 项目。
