# Linux 运行与 DSH 接入

官方 DSH：https://github.com/deepseek-ai/deepseek-harness 。官方 npm 启动方式为 `npx @deepseek-ai/dsh web`，Web 默认端口 3080。

Linux 使用源码部署，Windows 离线 zip 不能在 Linux 执行。本次部署基于 `fix/sandbox-and-slow-pages` 的 `f6343a7` 加工作区修改，没有改 Windows 的安装目录、项目或 Release。两份 Windows 项目记忆在本环境不可用。

## 维护者首次部署（需要网络）

需要 Linux x64、Python 3.12+、Node 24.9+，以及可运行的 Chromium 和版本匹配的 chromedriver。Debian/Ubuntu 可由管理员预装 `chromium`、`chromium-driver`。不要在日常测试会话里自动安装。

以下命令在仓库根目录逐条运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-linux.txt
npm install --prefix ../autoskill-linux-tools --offline=false @deepseek-ai/dsh@0.2.2-rc.1 pnpm@12.18.0 agent-browser@0.42.0
mkdir -p skills/autoskill/vendor/linux-x64/agent-browser
cp ../autoskill-linux-tools/node_modules/agent-browser/bin/agent-browser-linux-x64 skills/autoskill/vendor/linux-x64/agent-browser/agent-browser
chmod +x skills/autoskill/vendor/linux-x64/agent-browser/agent-browser skills/autoskill/autoskill
```

Chromium 和 chromedriver 可通过 `AUTOSKILL_CHROMIUM`、`AUTOSKILL_CHROMEDRIVER` 指定绝对路径（启动 DSH 的终端也要设置），或者分别放在：

- `skills/autoskill/vendor/linux-x64/chrome/chrome-linux64/chrome`
- `skills/autoskill/vendor/linux-x64/chromedriver/chromedriver-linux64/chromedriver`

当前环境的 Chrome for Testing 下载地址被环境代理拒绝，因此使用已安装的 Debian Chromium `156.0.8078.79`，链接到 `/usr/lib/chromium/chromium`；匹配的驱动来自 Debian 官方 `chromium-driver_156.0.8078.79-1~deb13u1_amd64.deb`。这是依赖当前系统的部署，不是可搬走的 Linux 离线包。

验证并安装插件（`link:` 路径换成自己的绝对仓库路径）：

```sh
skills/autoskill/autoskill doctor --launch
export PATH="$(cd ../autoskill-linux-tools/node_modules/.bin && pwd):$PATH"
dsh plugin --profile web add link:/workspace/autoSkill
sh scripts/run-dsh-linux.sh
```

源码部署没有 Windows 离线包清单，不能把 `verify` 写成通过；使用 `doctor --launch` 检查依赖和真实启动。

## 使用

打开 DSH 启动输出中的本机 URL（包含访问令牌）。本次启动日志在 `/workspace/autoskill-linux-tools/run/dsh.log`，仅本机用户可读。已在 Web 页面中选择 `/workspace/autoSkill` 为工作区（目录选择器使用 `/home/agent/autoSkill` 符号链接，DSH 保存的真实路径是 `/workspace/autoSkill`）。在 Settings → Models 配置自己的模型或账户。不要把 API 密钥粘到聊天记录里。

DSH 插件启动独立的 autoSkill 仪表盘，地址为 `http://127.0.0.1:8787`，侧栏有仪表盘、定时任务两个入口。命令行入口为 `skills/autoskill/autoskill`。项目仍在 `skills/autoskill/workspace`，私有数据在其 `.local` 下。Linux 凭据和登录态使用 Fernet 加密，本机密钥 `.local/encryption.key` 权限 0600；它不等同于 Windows DPAPI，不能解密 Windows 凭据，需重新设置。

远程打开 Linux Web 时，请同时转发 3080 和 8787 到浏览器所在机器的相同端口，iframe 才能显示。仅转发 DSH 的 3080 不够。

网络不再受 autoSkill 的站点白名单限制：录制、两种页面回放、接口执行均不注入代理围栏；旧的 `allowed_origins` 只保留配置兼容。系统代理、防火墙、证书、网站自身限制仍会影响访问。更新代码后须重启 DSH，并取消旧录制再新建录制，让新浏览器参数生效。

Linux 接入不替代 Windows DSH 默认沙箱复验。未配置模型、未通过真实智能体发出的命令，不能写成智能体或沙箱验收通过。

## 本次模型配置与公网复验

已配置 HappyCoding 自定义提供商，OpenAI Chat Completions 协议，Base URL `https://happycoding.xyz/v1`，模型 `deepseek-v4.1-flash`，并在 DSH 中选为默认模型。凭据仅在本机 DSH 凭据库中。DSH 真实连接请求的日志是代理 CONNECT 403；界面把它归类为 AUTH，不足以判断密钥是否有效。

GitHub 仓库读取、仓库搜索、刷新、跨站 npm 查询已通过后台服务真实录制，并用 Playwright 和 Selenium 分别回放通过；详见 `../reports/公网只读访问验证.md`。接口执行现在遵循系统代理、NO_PROXY 和 CA 设置；兼容 DSH 写入的方括号 IPv6 NO_PROXY 项。
