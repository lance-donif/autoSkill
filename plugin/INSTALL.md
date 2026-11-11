# autoSkill：Skill 与 DSH 插件两种安装方式

## 环境

Node.js 22+、Python 3.10+。代码已对照 DeepSeek Harness 官方 master 的 SkillRegistry、Client Modules、主面板/侧边栏槽位及 TypeScript SDK 接口实现。当前环境没有配置DSH模型，也无法正常启动浏览器，不能将契约测试视为完整宿主验收。

先在仓库目录准备依赖：

```bash
python -m venv .venv
# Linux / macOS
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
npm install
npm run build
npm test
```

Windows PowerShell 激活使用 `.venv\Scripts\Activate.ps1`。如DSH不继承该虚拟环境，启动DSH前设置 `AUTOSKILL_PYTHON` 为虚拟环境Python绝对路径：Windows为 `.venv\Scripts\python.exe`，Linux/macOS为 `.venv/bin/python`。

`npm install` 的可选依赖是官方 `agent-browser@0.42.0`。AI浏览器执行还需按官方Skill运行 `agent-browser install` 安装浏览器，并用 `agent-browser skills get core` 加载匹配版本指南。脚本UI执行使用Playwright自己的浏览器，不需要调用AI。

## 方式一：仅安装 Skill

把 `skills/autoskill/` 与 `skills/agent-browser/` 两个完整目录并列放到DSH发现目录 `.dsh/skills/`（项目）或 `~/.dsh/skills/`（用户）。需要Selenium固定用例时，也可复制另外两个顶层Skill目录。

主技能标识为 `autoskill`，显示名为autoSkill。DSH按顶层Skill目录发现技能，主技能会主动读取自己的嵌套能力模块与项目UI/API用例Skill。仅Skill方式不注册仪表盘或调度页面。

## 方式二：安装DSH插件

1. 完成上面的依赖准备和构建。
2. 在本机DSH Web的 **Plugins** 页面，使用插件管理器安装本仓库目录的本地Bundle：`file:/绝对路径/autoSkill`。该格式来自官方插件管理器支持的本地依赖规范。
3. 启用 `dsh-plugin-autoskill` Bundle；包的 `dsh.bundle.patch` 指向 `cordis.patch.yml`，插入 `autoskill` 插件。
4. 插件注册4个随附Skill，并启动本地Python后台。DSH侧边栏增加 **autoSkill 仪表盘** 与 **autoSkill 定时任务**。前端工厂构建产物为 `lib/client.js`。
5. 默认后台地址为 `http://127.0.0.1:8787`；状态数据默认保存于DSH Home的 `autoskill/`，包括SQLite任务、项目和运行快照。插件配置可设 `port`、`stateRoot`、`python`、`chromium`。

目前面板使用本机回环地址的iframe，要求DSH宿主与浏览器在同一台机器。远程DSH Web部署不能直接用访客机器的127.0.0.1访问服务器后台，需另做经过DSH身份校验的传输桥；此限制不隐藏。

AI引擎另需要与已安装DSH相匹配、提供 `DeepSeekHarness` 的 `@deepseek-ai/dsh-sdk-client`，以及用户已有的provider/model与审批设置。该SDK是可选peer dependency；脚本模式不会导入它。官方源码版本与npm发布版可能不同，不能随意混用；`npm run doctor`检查依赖是否能解析。未配置模型、SDK或审批不可用时任务应受阻，不能自动放宽权限。

插件安装/启用流程会执行宿主代码，应由用户在自己的DSH插件管理器中操作；本仓库不会修改用户现有DSH配置。

## 不依赖DSH的本地预览

```bash
npm run dashboard
```

打开终端输出的本机地址。它使用与插件版相同的后台与页面，便于先检查依赖和数据。单独预览不等于已在真实DSH宿主中通过验收。

## 正常使用顺序

创建项目 → 导入JSON或让DSH AI根据需求生成 → 校验并保存项目UI/API用例Skill → 在运行配置中明确授权origin/操作 → 选择AI或脚本引擎 → 立即执行或创建定时任务 → 查看断言、证据、版本、报告与回放。

AI生成自然语言用例后，UI用例必须有 `extensions.automation` 白名单动作和断言，才能脚本运行。接口用例必须有确认来源、请求与断言。没有可执行定义的用例应先补充，不能把自然语言当作脚本直接eval。脚本可完全不调用模型，但浏览器与HTTP运行时仍必须可用。
