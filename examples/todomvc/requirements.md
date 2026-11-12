# 本地开源 TodoMVC 测试需求

源项目：https://github.com/tastejs/todomvc/tree/master/examples/javascript-es5
本地site文件与上游Git blob校验值一致，未修改业务代码；许可证和来源清单随附。

范围：20条UI用例，包括新增、空白校验、字符转义、状态、编辑、筛选和刷新后的行为（原版使用内存存储，刷新即清空）。每条使用新浏览器上下文，仅合成数据。不包含删除、批量清空、兼容性和全部无障碍验收。

TodoMVC本身是本地存储页面，没有业务CRUD API。附带4条API用例只验证autoSkill控制台自身的公开只读接口，不冒充TodoMVC接口。

| 用例 | 功能 |
| --- | --- |
| TC-TODO-UI-001 | 空列表展示 |
| TC-TODO-UI-002 | 新增一条任务 |
| TC-TODO-UI-003 | 连续新增与顺序 |
| TC-TODO-UI-004 | 拒绝仅空白的任务 |
| TC-TODO-UI-005 | 新任务裁剪首尾空白 |
| TC-TODO-UI-006 | 特殊字符按纯文本显示 |
| TC-TODO-UI-007 | 提交后清空输入 |
| TC-TODO-UI-008 | Unicode任务录入 |
| TC-TODO-UI-009 | 任务标记完成 |
| TC-TODO-UI-010 | 完成状态可恢复 |
| TC-TODO-UI-011 | 回车保存编辑 |
| TC-TODO-UI-012 | Escape取消编辑 |
| TC-TODO-UI-013 | 失焦保存编辑 |
| TC-TODO-UI-014 | 编辑后裁剪空白 |
| TC-TODO-UI-015 | Active仅展示未完成 |
| TC-TODO-UI-016 | Completed仅展示完成 |
| TC-TODO-UI-017 | All恢复显示全部 |
| TC-TODO-UI-018 | 剩余数量复数展示 |
| TC-TODO-UI-019 | 刷新后列表清空（原版为内存存储） |
| TC-TODO-UI-020 | 刷新后保留筛选路由 |
