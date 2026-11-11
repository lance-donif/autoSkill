# 来源与接口契约

索引官方源码： https://github.com/SeleniumHQ/selenium/blob/trunk/common/src/web/index.html
源码 blob SHA：4c2ae61e37a620b4d079e9200191d62bba86c2f2。
源码显式 fetch： https://api.github.com/repos/SeleniumHQ/selenium/git/trees/gh-pages?recursive=true
官方契约： https://docs.github.com/en/rest/git/trees?apiVersion=2022-11-28

本技能4条用例：
1. recursive=true：200，JSON 树结构，确认3个目标页面路径存在。
2. 不传 recursive：200，根层树，不含嵌套路径。
3. recursive=false：参数存在仍表示递归，200，确认 web/web-form.html。
4. 合成的无效引用：404，JSON 含非空 message。

公共资源允许无认证 GET；无效资源文档给出404。429、服务错误、网络错误及非预期403属于本轮环境/访问受限，记录 Blocked；其余与预期不一致记 Fail。recursive 的字符串 false 不是关闭开关，省略才不递归。文档说明大树可能截断，不能把部分清单作为全量。

schema_version=1.1.0：在1.0.0基础上新增 api_source=source-and-official-docs。是对旧验证器的显式不兼容，需升级或人工审阅映射后导入；不伪造 browser/openapi 来源。请求负例只改变已知路由的 ref 值，不发起破坏性操作。
