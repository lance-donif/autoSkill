# autoSkill：Windows x64 内网完整包

## 用户环境不需要联网初始化

使用随交付文件提供的 **Windows x64 完整包**。依赖已直接置于 `skills/autoskill/vendor/windows-x64/`：Python嵌入运行时、Python库、Node运行时、测试浏览器、Agent Browser原生程序、DSH SDK及其依赖。浏览器core指南及references也已在Skill内，无须联网获取。

用户无需安装Python、Node、pip、npm或浏览器，不运行任何在线安装命令。解压后保留目录结构，建议目录 `C:\autoSkill`。支持Windows 10及以上x64；系统本身必须能够运行这些Windows程序。DSH宿主与内网模型服务仍由用户现有环境提供，不捆绑整个DSH应用或模型权重。

仓库源码与完整包有区别：GitHub源码不包含数百MB的运行时二进制；**下载源码ZIP不能替代完整包**。完整包中依赖确实位于Skill内部，不要求用户另找联网机器准备。组件版本、SHA256及依赖清单见完整包中的 `offline-manifest.json`、`requirements-windows.lock` 和仓库 `reports/windows-bundle-components.json`。

## 首次检查与使用

在cmd中执行（路径可自行修改）：

```bat
C:\autoSkill\skills\autoskill\run-windows.cmd verify
C:\autoSkill\skills\autoskill\run-windows.cmd doctor --ui
C:\autoSkill\skills\autoskill\run-windows.cmd dashboard
```

`verify`校验随附运行文件的SHA256，`doctor`仅检查本地依赖和浏览器文件，不联网。文件存在不等于浏览器已启动成功。仪表盘地址默认 `http://127.0.0.1:8787`，后台只监听回环地址。

核心Skill命令示例：

```bat
C:\autoSkill\skills\autoskill\run-windows.cmd qa init C:\qa-workspace\demo
C:\autoSkill\skills\autoskill\run-windows.cmd qa validate C:\qa-workspace\demo
```

项目数据放在包目录外，避免覆盖或污染交付文件。UI用例的URL、接口地址、账号引用、授权origin必须按内网测试环境配置。

## 方式一：仅Skill

把完整包的 `skills/autoskill/`（包括vendor，不能只复制SKILL.md）与 `skills/agent-browser/` 并列放入DSH的 `.dsh/skills/` 或 `~/.dsh/skills/`。主技能名autoskill，显示名autoSkill。仅Skill形式不注册仪表盘或定时任务页面；独立仪表盘启动命令仍可用于本地预览。

AI需要直接读取Skill内的 `references/agent-browser-core.offline/SKILL.md`；不得执行官方发现入口中的 `skills get` 或外部安装命令。AI操作浏览器使用完整包内离线包装器：它拒绝安装、升级、远程provider和策略覆盖，并读取项目授权origin配置。

## 方式二：DSH插件

DSH必须已在本机可用。在Plugins页面安装完整包的本地Bundle `file:C:/autoSkill`，启用 `dsh-plugin-autoskill`。插件自动优先选择随包Python和浏览器，注册4个顶层Skill，侧边栏增加仪表盘与定时任务页面。包内无强制npm依赖安装步骤。

支持配置port、stateRoot、python、chromium。显式配置优先于随包路径。仪表盘目前要求浏览器与DSH宿主在同一台机器；远程DSH Web访问桥尚未实现。

## AI与脚本模式

脚本模式不加载SDK、不调用模型，只访问获授权的内网测试网站或接口。

AI模式必须显式设置以下环境变量，并由DSH继承：

- `AUTOSKILL_DSH_COMMAND`：已有本地DSH JSON-RPC运行程序的绝对路径。
- `AUTOSKILL_DSH_ARGS`：对应参数的JSON数组，默认空数组；禁止使用npx等联网启动器。
- `AUTOSKILL_AI_PROVIDER`：DSH中已配置的内网provider。
- `AUTOSKILL_AI_MODEL`：该provider中的模型标识。

未设置时任务受阻，不回退到SDK默认的公网provider/model。随附SDK为0.0.1-rc.1的launch/provider/model接口，需要与实际DSH JSON-RPC宿主兼容；本轮没有真实DSH/模型联调。技能不会下载DSH或模型。

## 更新和遥测

autoSkill没有远程更新器、版本检查服务或遥测上传服务；运行入口禁用pip索引查询、浏览器下载、npm更新提示/审计/资助请求，设置DO_NOT_TRACK及OTEL_SDK_DISABLED。Node包管理器和Agent Browser的安装脚本不随完整包交付，PATH中的Agent Browser入口拒绝install/upgrade/update/skills等设置命令。浏览器启动关闭后台联网、组件更新、域可靠性、同步、崩溃上报及ping；脚本浏览器阻止Service Worker并按项目origin拦截请求。

这些设置不能证明任意第三方二进制或用户既有DSH宿主已经实现零外联。需要零外联保证时，应由内网出口策略只允许测试服务和模型服务，并在Windows实机记录进程网络流量验收。技能不能修改或审计用户整个DSH应用；本轮也没有完成Windows断网/抓包验收，报告不得写成通过。更新仅接受管理员手动替换经审核完整包，不在运行时联网更新。
