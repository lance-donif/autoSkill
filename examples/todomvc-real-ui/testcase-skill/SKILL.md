---
name: real-ui-todomvc-case-guidance
description: TodoMVC JavaScript ES5 本次真实UI测试的页面规则与断言方式
---
# TodoMVC 测试项目技能

- 测试页：`https://todomvc.com/examples/javascript-es5/dist/`。
- 测试数据只用合成待办标题，不访问外链或真实账号。
- 新建后以页面列表出现文本为新增断言；勾选后以 checkbox checked 状态和 item left 数量为完成断言。
- Completed 筛选以路由 `#/completed` 与已完成行显示为断言。
- 双击待办文本后以编辑输入框出现为编辑入口断言；提交修改后以更新后的文本和仍处于 completed 筛选为断言。
- 执行结果必须逐项引用观察日志；不得把计划、mock 或手写预期当成真实通过。
