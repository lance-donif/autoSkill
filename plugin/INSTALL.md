# 安装与使用

## 拿到的是什么

`autoSkill-Windows-x64-Offline.zip` 解压后有两个目录：

| 目录 | 用途 |
| --- | --- |
| `autoskill\` | Skill 本体，自带 Python、Chrome、chromedriver、agent-browser 和全部库。**只要这个目录就能用。** |
| `dsh-plugin\` | 可选：DeepSeek Harness 插件，把仪表盘和定时任务放进 DSH 侧边栏。 |

支持 Windows 10 / 11 x64。不需要安装 Python、Node 或浏览器，不需要联网。解压路径可以包含中文和空格。

从 GitHub 下载的源码 ZIP 不含运行时，不能代替完整包。

## 第一次使用

```bat
autoskill\autoskill.cmd verify            校验随包文件是否完整
autoskill\autoskill.cmd doctor --launch   真实启动一次浏览器，确认这台电脑能跑
autoskill\autoskill.cmd dashboard         启动仪表盘，浏览器打开 http://127.0.0.1:8787
```

仪表盘里点「加载示例项目」→「运行全部」，即可看到完整流程。

## 方式一：作为 Skill（任何智能体）

把 `autoskill\` 整个目录放到你的智能体能读取的位置（不能只复制 `SKILL.md`），然后告诉它：

> 使用 `<路径>\autoskill` 里的 autoSkill 技能测试 `<网址>`。

智能体会阅读 `SKILL.md`，通过 `autoskill` 命令完成建项目、录制、回放。如果平台有固定的 Skill 目录（例如 `.dsh/skills/`），把 `autoskill\` 放进去即可。

## 方式二：DeepSeek Harness 插件

DSH 需已安装。把 `dsh-plugin\` 里的三项（`plugin\`、`package.json`、`cordis.patch.yml`）放到 `autoskill\` 的上一级，使目录结构为：

```
<安装目录>\
  package.json
  cordis.patch.yml
  plugin\
  skills\autoskill\        ← 把 autoskill\ 改名并放到这里
```

在 DSH 的 Plugins 页面安装本地 Bundle `file:<安装目录>` 并启用 `dsh-plugin-autoskill`。插件会注册 autoSkill 技能、启动仪表盘，并在侧边栏增加「autoSkill 仪表盘」和「autoSkill 定时任务」。

可配置项：`port`（默认 8787）、`workspace`（数据目录，默认在 Skill 目录内）。

本插件在真实 DSH 宿主中的加载尚未实测，只通过了入口契约测试，见 `COMPATIBILITY.md`。

## 数据放在哪

```
autoskill\workspace\
  projects\<项目>\     用例、知识库、登录方式定义 —— 可以分享
  .local\              凭据、登录态、运行记录、截图 —— 只在本机
```

升级到新版本时，把旧目录里的 `workspace\` 复制到新目录即可。凭据和登录态用当前 Windows 用户加密，换电脑或换用户后需要重新设置凭据。

## 更新与外联

autoSkill 没有在线更新、版本检查或数据上报。更新方式是管理员手动替换为新的完整包。

运行时只连接两类地址：本机（仪表盘、浏览器驱动）和项目里授权的被测地址。浏览器的后台联网、组件更新、崩溃上报已关闭，未授权地址在浏览器层面不可达。

需要严格的零外联保证时，仍建议由内网出口策略限制，只放行被测系统。
