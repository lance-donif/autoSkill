# Selenium 官方测试页验证

验证日期：2026-11-13。目标为 Selenium 官方公开 [Web form](https://www.selenium.dev/selenium/web/web-form.html) 和 [动态交互页面](https://www.selenium.dev/selenium/web/dynamic.html)。

已在 autoSkill 保存项目 **Selenium 官方测试页**（`selenium`），用真实 Selenium 4.38.0、Chrome/ChromeDriver 151.0.7922.34 回放 17 条 UI 用例。窗口设置沿用已保存的隐藏偏好。用例根据实际页面、官方 HTML 源码与控件语义编写，并保存用户可读的功能设计；没有声称这些用例由成功录制得到。

## 页面目录

```text
Selenium 官方测试页（17）
├── 表单（含子目录共14）
│   ├── 文本输入（5）
│   ├── 选择控件（6）
│   ├── 状态与约束（2）
│   └── 提交与校验（1）
├── 页面导航
│   └── 返回索引（1）
└── 动态交互
    ├── 延迟元素（1）
    └── 延迟输入（1）
```

[目录与全部用例截图](evidence/2026-11-13-selenium-official/目录层级与全部用例-最终V2.png) · [文本输入子目录截图](evidence/2026-11-13-selenium-official/文本输入子文件夹-最终V2.png)。这是普通 Chromium 打开由 DSH 插件实际启动的仪表盘所得截图；真实 DSH 模型验收另见对应报告，不混淆来源。

## 实际结果与历史

| 轮次 | 运行编号 | 范围 | 通过 | 失败 |
| --- | --- | --- | --- | --- |
| 功能设计 V2 首轮 | `run-eb5f000a734c` | 17 | 14 | 3 |
| 同版本连接复测 | `run-379d716c7644` | 首轮失败的3条 | 2 | 1 |
| 引擎实现更新后复验 | `run-d65d6b29d751` | 原先通过的14条 | 10 | 4 |

**V2 各用例当前最新记录为 12 条通过、5 条失败；17 条的冻结执行证据与当前定义/引擎范围均核验有效。** 不能把不同轮次合并成“首轮全部通过”。5 条失败截图均显示外部上游连接错误；表单提交目标的独立 HTTP 请求也返回 503。这些结果不足以认定为官方页面的功能缺陷，原始 Fail、断言、截图和轮次全部保留。

[连接失败原始截图](evidence/2026-11-13-selenium-official/官方站点连接失败原始截图.png) 的原文为 `upstream connect error or disconnect/reset before headers. reset reason: remote connection failure`。没有修改断言或继续反复重试以凑全绿。

早期 V1 `run-50c0ca310df1` 是补充功能设计前的旧版本，原始报告与执行记录原字节保留，不作为 V2 通过证据；旧 Excel SHA-256 仍为 `59ad59f06e55465dce8c1adefa035db914be856055de94cec8327c7ecebe11f2`。

## 用例规范与交付

实际读取并核对用户原始 `test-cases.rar`：SHA-256 `08f913f3c8d4de28fac9c19acd9481355c78f6ccbb2e427788dc3ec7df72307f`。其中原 SKILL 与工作簿规则的字节数和哈希均与仓库来源清单一致。

正式工作簿主表为 14 列、真实业务层级及拼音 FT 标识。D–G 列读取保存的功能目标、必要条件、具体【界面原文控件】操作/输入、对应编号的可观察预期；未将 GET 路径、选择器或浏览器 IR 当作功能步骤。原始技术数据和定义哈希保留在附表，项目、文件夹与任务导出使用同一内容规则。

还直接执行了用户原包中未修改的校验器：项目导出以7个功能项、17条用例校验通过，表单导出以4个功能项、14条用例校验通过。这是原版结构规则的实际通过结果，不替代业务语义核对或真实执行。

实际读回确认：项目 17 条，表单父文件夹递归 14 条，均含完整业务路径且原用例 ID 有一对一映射。未提供真实编制人的字段保持待确认，没有虚构人员。结构与内容读回不等同于 Excel 原生视觉渲染，本轮未执行原生工作簿视觉渲染。

- [项目用例 Excel（17条）](/workspace/scratch/dsh-acceptance-private/data/projects/selenium/reports/export-fb899f5fddba42428b37fe00aef50693.xlsx)
- [表单文件夹 Excel（14条）](/workspace/scratch/dsh-acceptance-private/data/projects/selenium/reports/export-0e03bd7a76c24bc595b2503cbb39dc86.xlsx)
- [项目汇总 Excel](/workspace/scratch/dsh-acceptance-private/data/projects/selenium/reports/project-summary/selenium-测试报告-20261113-102300-705033-c01a0e.xlsx)
- [项目资料 ZIP](/workspace/scratch/dsh-acceptance-private/data/.local/material-exports/materials-196e575f9c024031852b1e858b16036f.zip)，SHA-256 `513ec599b06c1bbed22ab161b6a7c10c717af901739e110fa092e4a12c239919`。

资料包保留知识库、用例、报告、完整项目领域版本及18份原始执行截图的逐字节副本与来源清单。它是资料归档；完整本机运行历史恢复仍需使用整套数据导出，不将导入资料冒充另一台机器的当前验证。

项目实际位置：`/workspace/scratch/dsh-acceptance-private/data/projects/selenium`。知识、用例和报告分别在 `knowledge/`、`testcases/`、`reports/`。页面的嵌套目录由版本化文件夹元数据关联原始用例，不伪造多套可编辑用例副本。

可复现的来源脚本为 [seed-selenium-official.py](../scripts/seed-selenium-official.py)，运行时与真实项目数据均未加入 Git。范围未覆盖文件上传、原生颜色选择弹窗、其他浏览器/平台及线上容量。

## 稳定交付目录

最终用户文件另以原字节复制至 [/workspace/shared/autoSkill-交付/Selenium官方测试页](/workspace/shared/autoSkill-交付/Selenium官方测试页)。三份真实DSH下载Excel分别为项目17条、表单父目录14条、文本输入子目录5条，文件名可读且SHA不变。

目录中另有资料包与项目分享包。项目分享包已在独立空数据目录实际导入，17条定义与文件夹树逐项一致，导入环境执行结果数为0，不把本机历史当作目标环境的验证。导入验证仅使用临时目录，验证后删除，原权威项目未移动。
