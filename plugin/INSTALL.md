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

**不需要把 Skill 复制到 `.agents/skills` 或其他技能目录**：插件直接登记解压目录里的 `autoskill\`，并把它的绝对路径告诉智能体。之后对智能体说「用 autoSkill 测试 <网址>」即可。

安装后不要移动或删除解压目录；要换位置，先在 DSH 里移除插件，移动后重新安装。

**DSH 默认把智能体的命令放在沙箱里执行**（不能写会话目录以外的文件，不能启动浏览器）。不需要为此调整权限：插件启动的仪表盘进程在沙箱外，智能体发出的 autoskill 命令发现自己受限时，会自动交给这个进程执行。所以请保持插件处于启用状态。

### 2. 其他智能体（不装插件）

任选其一：

- 直接告诉智能体：「使用 `D:\autoSkill\autoskill` 里的 autoSkill 技能测试 <网址>」。
- 或把 **整个 `autoskill\` 文件夹**（约 615 MB，不能只复制 `SKILL.md`）复制到该智能体的技能目录，例如 `.agents\skills\autoskill`。

只保留一份在用：每份 `autoskill\` 有自己的 `workspace\`，两份之间的项目和运行记录互不相通。

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
autoskill\workspace\
  projects\<项目>\     用例、知识库、登录方式定义 —— 可以分享
  .local\              凭据、登录态、运行记录、截图 —— 只在本机
```

把自己维护的用例发给别人：`autoskill.cmd pack`（几 KB，对方 `autoskill.cmd import`），或 `autoskill.cmd pack --full --out <目录>`（连运行时一起，对方解压即用）。凭据不随包带走，对方首次使用时按提示设置。

升级到新版本：把旧目录里的 `autoskill\workspace\` 复制到新目录；用 DSH 插件时，换好后在 DSH 里停用再启用一次插件，让仪表盘进程用上新版本。凭据和登录态用当前 Windows 用户加密，换电脑或换用户后需要重新设置凭据。

## 常见问题

| 现象 | 原因与做法 |
| --- | --- |
| DSH 报「所选目录里没有运行时」 | 选的是源码目录，或选到了 `autoskill\` 里面一层。选解压目录本身 |
| DSH 报「仪表盘没有在端口 8787 上启动」 | 端口被占用（例如已经手动运行了 `autoskill.cmd dashboard`）。关掉它，或在插件配置里改 `port` |
| 智能体报「当前命令运行在受限沙箱里」 | 智能体的命令被关在沙箱里，而 autoSkill 后台服务没在运行。在 DSH 里启用插件（或运行 `autoskill.cmd dashboard`）后让智能体重试 |
| 智能体报「后台服务是旧版本」 | 升级了 autoSkill 但仪表盘进程还是旧的。DSH 里停用再启用插件 |
| 录制时提示页面加载很慢 | 页面本身慢，不是故障。默认最多等 90 秒；更慢的页面：`autoskill.cmd project update <项目> --page-load 180` |
| 看不到浏览器在操作 | 默认在后台运行。命令加 `--headed`，或在仪表盘「运行」按钮旁的菜单勾选「显示浏览器窗口」 |

## 更新与外联

autoSkill 没有在线更新、版本检查或数据上报。更新方式是手动替换为新的完整包。运行时只连接本机和项目里授权的被测地址。需要严格的零外联保证时，仍建议由内网出口策略限制。
