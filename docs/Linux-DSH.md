# Linux / 统信 UOS 安装与 DSH 接入

## 选择安装包

运行 `uname -m`，从 [最新 Release](https://github.com/lance-donif/autoSkill/releases/latest) 选择：

| 系统 / CPU | 附件 |
| --- | --- |
| Windows 10/11，Intel / AMD x64 | `autoSkill-Windows-x64-Offline.zip` |
| Linux，`x86_64` | `autoSkill-Linux-x64-Offline.zip` |
| Linux / 统信 UOS，`aarch64` 或 `arm64`，鲲鹏 / 飞腾等 | `autoSkill-Linux-arm64-Offline.zip` |

共 3 个包，UOS ARM64 与其他 glibc Linux ARM64 共用原生包。龙芯、申威、32 位 ARM、musl Linux 不属于这三个包的运行架构。第三方支持依据见 [平台与第三方支持.md](平台与第三方支持.md)。

Linux 包包含 Python 3.12.10（python-build-standalone）、agent-browser 0.42.0、Chrome for Testing 157.0.8103.78、同版本 chromedriver，以及 Playwright 1.62.0、Selenium 4.38.0 等 Python 库。运行时不联网安装或更新，不需要系统 Python。DSH 宿主单独安装，需 Node >= 24.9。

**系统前提：glibc >= 2.28，以及浏览器需要的系统共享库。** Python 库按 manylinux2014 / manylinux_2_28 获取，未把 Ubuntu 系统库塞进 UOS。包内新版 Chrome 可能需要高于 2.28 的 glibc 或其他库；旧版 UOS 可配置本机 Chromium 和匹配的 chromedriver，不能用新版驱动控制旧版浏览器。UOS 不在 Playwright 官方支持发行版名单内，当前没有 UOS ARM64 真机验收结果。

## 安装离线包

先在下载目录校验 ZIP（`SHA256SUMS.txt` 也从同一 Release 下载）：

```sh
sha256sum -c SHA256SUMS.txt --ignore-missing
```

解压到可写目录，终端进入解压根目录（有 `package.json`、`plugin/`、`autoskill/` 的那层）：

```sh
sh setup-linux.sh
autoskill/autoskill verify
autoskill/autoskill doctor --launch
autoskill/autoskill dashboard
```

`setup-linux.sh` 修复解压丢失的执行权限、识别 CPU、检查浏览器与驱动主版本，并保存本机浏览器配置。`verify` 检查包内运行时 SHA-256；`doctor --launch` 在本机示例页实际启动 Playwright、Selenium，同时检查 agent-browser 能否运行。这些命令的真实结果才是目标电脑的验收结果。仪表盘：<http://127.0.0.1:8787>。如果文件系统以 `noexec` 挂载，请把目录放到允许执行程序的位置。

## UOS / 老系统的浏览器

如果包内 Chrome 报 `GLIBC_* not found` 或缺少 `.so`，安装脚本尝试 PATH 中的系统 Chromium 与 chromedriver。也可以显式指定：

```sh
sh setup-linux.sh --chromium /usr/bin/chromium --chromedriver /usr/bin/chromedriver
autoskill/autoskill doctor --launch
```

路径以实际位置为准，浏览器和驱动的 `--version` 主版本必须相同。配置保存在 `autoskill/vendor/linux-<架构>/runtime-settings.json`；覆盖安装前保留此配置。临时覆盖可设置 `AUTOSKILL_CHROMIUM` / `AUTOSKILL_CHROMEDRIVER`，启动 DSH 的进程也需继承这些环境。

缺少系统浏览器或驱动时，请管理员从适用于当前 UOS 版本和 CPU 的官方软件源准备 Chromium 及匹配驱动。常见共享库包括 NSS、ATK、GTK、GBM、X11、ALSA、字体库；具体缺失项看安装输出或 `ldd <浏览器实际二进制路径>`。Debian / Ubuntu 与统信软件源的包名可能不同，不在 UOS 上直接执行 `playwright install-deps` 或硬装其他发行版系统库。

Selenium 4.38.0 的 Selenium Manager 不支持 Linux ARM64；autoSkill 显式使用包内或配置的 chromedriver，不调用 Manager 下载驱动。导出的独立 Selenium 脚本在 ARM64 上运行时也应设置 `CHROME_BINARY`、`CHROMEDRIVER`。

## 接入 DSH

官方 DSH：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。在已安装并能运行的 DSH 中，选择本地插件目录为上述解压根目录，或执行：

```sh
dsh plugin --profile web add link:/你的绝对路径/autoSkill
dsh web --no-open
```

启用插件后，用当前 CPU 的包内 Python 启动仪表盘，侧栏出现仪表盘、定时任务。无需全局 Python，不会运行 npm / pip 安装器。进程需能写 `autoskill/workspace/`，并访问本机 8787 端口。远程访问 DSH Web 时，应把 DSH 的 3080 和仪表盘的 8787 都转发到浏览器所在电脑的相同端口。

给 DSH 智能体的任务示例：

> 使用 autoSkill 测试这个网址，只做正常访问和查询。先运行 project list，再按技能说明新建项目、录制、断言、回放。

多个 AI 会话可以共用同一个插件和仪表盘。给每个测试任务指定不同的会话标识，并让它的所有命令都带上全局前缀，例如：

```sh
autoskill/autoskill --session "订单任务-甲" record start <项目>
autoskill/autoskill --session "库存任务-乙" record start <项目>
autoskill/autoskill --session "订单任务-甲" browser snapshot
autoskill/autoskill --session "库存任务-乙" browser snapshot
```

也可在每个任务自己的命令进程中设置 `AUTOSKILL_SESSION`；不要给整个 DSH 服务统一设置一个值后让所有任务共用它。标识可使用任意文字，内部按哈希隔离浏览器、录制状态与默认用例表。`record stop`、`check`、`plan` 和 `run` 都延续同一标识；取消一项任务不会关闭另一项任务的浏览器。项目和已保存用例仍然共享，同项目保存自动排队，不同会话可并行操作。未指定会话时保留旧命令的默认录制行为。

回放全局默认并行 2 个任务，可在服务和命令环境中设置 `AUTOSKILL_MAX_PARALLEL` 调大；同项目回放会复用共享登录态，因此按顺序执行。多个会话的录制浏览器仍可同时操作同一个项目。

本机后台转交会传递调用者的会话身份，不使用仪表盘进程恰好继承的 `AUTOSKILL_SESSION`。这项隔离已通过本地 CLI 与真实 HTTP 转交测试；它不代表已经完成 DSH 智能体实机验收。

升级前先在 DSH 停用插件，确认仪表盘退出；备份并保留旧目录的 `autoskill/workspace/`（包括 `.local`），再换包、重新启用。当前凭据明文保存在 `workspace/.local/credentials.json`，项目设置中可查看和修改；Windows DPAPI、Linux Fernet 仅用于兼容读取旧版本的加密存储。项目分享和完整包迁移按技能中的 `pack` 命令说明执行。

## 从源码部署

需要 Python 3.12+、Node >= 24.9、系统 Chromium 和匹配驱动。下面命令在联网准备阶段执行，日常技能命令不安装依赖：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-linux.txt
npm install --prefix ../autoskill-linux-tools --offline=false @deepseek-ai/dsh@0.2.2-rc.1 pnpm@12.18.0 agent-browser@0.42.0
```

从 `node_modules/agent-browser/bin/` 复制匹配 CPU 的 `agent-browser-linux-x64` 或 `agent-browser-linux-arm64` 到 `skills/autoskill/vendor/linux-<架构>/agent-browser/agent-browser`，赋执行权限；也可用 `AUTOSKILL_AGENT_BROWSER` 指向原生程序。设置浏览器和驱动环境变量后执行 `skills/autoskill/autoskill doctor --launch`。源码部署无完整包清单，`verify` 不适用。通过 `sh scripts/run-dsh-linux.sh` 启动预装的 DSH Web。

## 验证边界

Debian 13 x64 开发环境实际运行 agent-browser 0.42.0，`doctor --launch` 的 Playwright、Selenium 本机页面启动通过。历史公网只读录制、双引擎回放和真实 DSH Web 面板结果见 `reports/公网只读访问验证.md`、`plugin/COMPATIBILITY.md`。这些不等于新离线包或 UOS 的功能验收。

Release 按使用者要求跳过完整功能回归、浏览器启动测试，只做组装导入、架构、ZIP 结构/CRC、大小、上传 SHA-256 检查。UOS ARM64 真机、各 UOS 版本、DSH 默认沙箱智能体执行均待验证。当前模型网关被环境代理 CONNECT 403 阻断，不能把模拟转交写成智能体验收通过。没有重新添加站点白名单或代理围栏。
