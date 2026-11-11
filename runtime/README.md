# 共享执行核心

`engines.execute` 从权威项目JSON创建每次运行的独立快照。script/API调用独立HTTP引擎；script/UI使用Python Playwright且不初始化DSH SDK或模型；ai显式调用官方DSH TypeScript SDK，由模型读取项目用例Skill并真实执行。

UI自动化动作存入用例 `extensions.automation`：`start_url`、有序 `actions`、非空 `assertions`。动作支持 fill/click/dblclick/press/check/uncheck/select/reload/wait_visible，断言支持 count/text/value/visible/checked/attribute。定位可用selector、placeholder或role+name。无eval、任意代码和shell命令入口。没有脚本动作的自然语言用例不能直接当作可执行脚本。

每次运行保留动作日志、断言、截图、用例修订与内容哈希；AI解释只读真实日志，重新执行产生新run。AI需要用户已配置的DSH运行时、模型与可用审批通道，不能以SDK投递成功代替测试成功。

当前环境启动Chromium受系统进程限制，脚本UI尚未通过本机浏览器验收。公共Selenium和Playwright演示站在交互浏览器不可达。本地官方TodoMVC副本不修改业务代码，可用于用户机器上的真实验收；不能把尚未执行的用例标Pass。
