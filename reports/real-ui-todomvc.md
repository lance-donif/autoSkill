# TodoMVC 真实UI自动化与问题修复

日期：2026-11-12（北京时间）。目标：https://todomvc.com/examples/javascript-es5/dist/

## 实际结果

| 用例 | 真实断言 | 当前结果 |
| --- | --- | --- |
| TC-F-001 新增待办 | 合成标题准确，列表一条，1 item left | Pass |
| TC-F-002 标记完成 | checkbox为checked，0 items left | Pass |
| TC-F-003 Completed筛选 | URL为#/completed，合成待办可见，筛选链接selected | Pass |
| TC-F-004 双击编辑 | 进入编辑框，提交后标题准确，完成状态保留 | Pass |

Luna以不继承主会话上下文的会话独立执行首轮，逐条观察日志保留。主代理随后校正用例定义并重新实际执行，当前每个结果绑定最新修订与内容哈希，附真实截图与DOM观察日志。项目通过validate与coverage，需求关联4/4，并已导出Excel与执行报告。

## 问题与处理

1. 仓库主Skill指向的离线core指南缺失。已补入固定版本agent-browser@0.42.0的指南/引用/模板，附内容哈希，安装产物检查覆盖相关路径；已有Windows完整包原本含该指南。
2. save-cases输入说明不够明确，Luna首次传入分组对象而被拒绝。CLI正确要求扁平对象数组，现补清主Skill和生成模块说明，不改变存储API。
3. 初始四条用例误用了相同的“空列表”前置条件，后续实际操作依赖已有待办。已按各阶段状态修正，并补充供脚本新上下文使用的初始化动作与断言；旧结果归档后用新修订重跑。
4. 主代理复核时旧标签页输入后未提交；保留preflight-input-error.json。新建同一官方页面的标签页后完成真实操作。未把旧标签页失败记录删掉或计为通过。
5. Luna首轮截图只在工具中显示，未形成项目附件。主代理重跑时逐条保存截图文件、DOM、开始与完成时间，当前结果可定位到文件。

## 证据与范围

完整项目在[examples/todomvc-real-ui](../examples/todomvc-real-ui/)，标准UI用例、项目Skill、JSON结果、原始日志、4张截图与Excel均保留。当前执行模式为AI借助真实云浏览器工具操作，网页是官方公开TodoMVC；没有运行DSH模型、Windows运行时或脚本UI引擎，也没有生成或执行API用例。不得把本次4条通过扩展为原20条UI全部通过或Windows离线端到端通过。

样例默认取消执行授权；实际运行配置另存execution/run-config.json，重放需重新授权。脚本动作只做结构校验，本轮未把它们声明为脚本执行Pass。
