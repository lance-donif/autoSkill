# 安装与使用

Linux / 统信 UOS ARM64 请使用匹配 CPU 的 Linux 包，先运行 `sh setup-linux.sh`；安装、系统库和 DSH 接入见 [Linux-DSH.md](../docs/Linux-DSH.md)。以下为 Windows 安装说明。

## 拿到的是什么

`autoSkill-Windows-x64-Offline.zip` 解压后：

```
<解压目录>\
  package.json  cordis.patch.yml  plugin\     DeepSeek Harness 插件（在 DSH 里选这一层目录）
  autoskill\                                  Skill 本体：SKILL.md、命令入口、运行时、示例
```

支持 Windows 10 / 11 x64。不需要安装 Python、Node 或浏览器，不需要联网。解压路径可以包含中文和空格。

**从 GitHub 下载的源码不含运行时，不能用来安装。** 在 DSH 里选了源码目录会报「所选目录里没有运行时」。

## 三种用法，选一种

### 1. DeepSeek Harness 插件（推荐给用 DSH 的同事）

1. 解压到一个固定位置，例如 `D:\autoSkill`。
2. DSH → Plugins → 安装本地插件，选择 **解压目录本身**（里面有 `package.json` 的那一层），启用 `dsh-plugin-autoskill`。
3. 启用后：autoSkill 技能自动注册给 DSH 的智能体；侧边栏出现「autoSkill 仪表盘」和「autoSkill 定时任务」。

**不需要把 Skill 复制到 `.agents/skills` 或其他技能目录**：插件直接登记解压目录里的 `autoskill\`，并把它的绝对路径告诉智能体。输入无参数的 `/autoskill-use` 加载技能，提示已加载后直接描述任务；不填写 `task` 或 `mode`。该命令本身不启动模型任务，后续对话沿用宿主模型配置。

输入框上方不再增加 autoSkill 任务栏，也不自动打开任务弹窗。原生工具结果默认折叠，需要详情时展开；完整页面入口在侧边栏仪表盘，包括项目、设计用例表、压测、原生资产、多轮对话、需求工作流、项目资料、定时任务和运行诊断。

安装后不要移动或删除解压目录；要换位置，先在 DSH 里移除插件，移动后重新安装。

**DSH 默认把智能体的命令放在沙箱里执行**（不能写会话目录以外的文件，不能启动浏览器）。不需要为此调整权限：插件启动的仪表盘进程在沙箱外，智能体发出的 autoskill 命令发现自己受限时，会自动交给这个进程执行。所以请保持插件处于启用状态。

### 2. 其他智能体（不装插件）

任选其一：

- 直接告诉智能体：「使用 `D:\autoSkill\autoskill` 里的 autoSkill 技能测试 <网址>」。
- 或把 **整个 `autoskill\` 文件夹**（约 615 MB，不能只复制 `SKILL.md`）复制到该智能体的技能目录，例如 `.agents\skills\autoskill`。

程序和数据目录分开。默认使用同一用户的 `autoSkill-data`，显式选择的 `workspace` 优先；智能体、命令行和仪表盘应使用同一个数据目录。

如果这个智能体也把命令放在沙箱里执行，请先在自己的终端里运行 `autoskill\autoskill.cmd dashboard` 并保持窗口不关：命令会自动交给它执行。或者给该智能体的会话完全访问权限。

### 3. 只用仪表盘

```bat
autoskill\autoskill.cmd dashboard
```

浏览器打开 <http://127.0.0.1:8787>，点「加载示例项目」→「运行全部」。

## 自检

```bat
autoskill\autoskill.cmd verify            校验随包文件是否完整
autoskill\autoskill.cmd doctor --launch   真实启动一次浏览器
```

## 数据放在哪

```
%USERPROFILE%\autoSkill-data\
  projects\<项目>\
    testcases\          用例原文件及设计用例表
    knowledge\          知识库原文件
    reports\            新任务Excel、简报、Bug清单与冻结快照
    domain\             来源、工作流、原生资产的完整版本
  .local\               本机运行索引、凭据、登录态、执行证据及设置
```

分享项目可在「项目资料 → 分享与导入」生成和下载项目包，也可使用 `autoskill.cmd pack` 与 `autoskill.cmd import`。项目包保留知识、用例、项目报告和版本来源，大小取决于实际内容；可选择口令加密携带本机引用的凭据。`pack --full` 连程序运行时、已保存的明文凭据和登录态一起携带，无需另设口令。要完整恢复运行历史、截图和全部设置，使用「数据管理 → 导出全部数据」。详见[项目资料与分享](../docs/项目资料与分享.md)。

更新到新版本只替换程序目录，继续使用同一数据文件夹。Windows 默认 `%USERPROFILE%\autoSkill-data`；DSH 的 `workspace` 可显式指定其他位置。旧安装的数据首次通过「数据管理」导入到空目录，原数据保留，按回执配置该目录后重启；以后不再搬数据。重新加载插件模块或重启 DSH，确保仪表盘与内嵌页面使用新代码。完整步骤见 [升级与数据保留](../docs/升级与数据保留.md)。当前凭据和保存登录态也包含在完整数据文件夹中；旧版加密存储保持兼容读取。

## 常见问题

| 现象 | 原因与做法 |
| --- | --- |
| DSH 报「所选目录里没有运行时」 | 选的是源码目录，或选到了 `autoskill\` 里面一层。选解压目录本身 |
| DSH 报「仪表盘没有在端口 8787 上启动」 | 端口被占用（例如已经手动运行了 `autoskill.cmd dashboard`）。关掉它，或在插件配置里改 `port` |
| 智能体报「当前命令运行在受限沙箱里」 | 智能体的命令被关在沙箱里，而 autoSkill 后台服务没在运行。在 DSH 里启用插件（或运行 `autoskill.cmd dashboard`）后让智能体重试 |
| 智能体报「后台服务是旧版本」 | 升级了 autoSkill 但仪表盘进程还是旧的。DSH 里停用再启用插件 |
| 录制时提示页面加载很慢 | 页面本身慢，不是故障。默认最多等 90 秒；更慢的页面：`autoskill.cmd project update <项目> --page-load 180` |
| 想显示或隐藏浏览器窗口 | 顶部「回放设置 → 显示浏览器窗口」会保存到当前数据目录，录制、回放、登录及DSH对话任务沿用。明确 `--headed` / `--headless`、`AUTOSKILL_HEADED=1/0` 优先；定时任务使用自己保存的窗口选择 |

## 更新与外联

autoSkill 没有在线更新、版本检查或数据上报。更新方式是手动替换为新的完整包。浏览器与接口按测试流程访问站点，没有内置站点白名单；需要限制网络时由实际环境的网络策略配置。
