---
name: selenium-api-cases
description: 独立发送真实 GET 请求，执行 Selenium 官方索引页依赖的 GitHub Tree API 四条用例，校验递归参数、响应结构、目录文件和无效引用，输出有证据的接口结果。
---
# Selenium 接口用例

读取 references/cases.json、references/case.schema.json 和 references/api-contract.md。使用真实且独立的 HTTP 请求验证索引页依赖；这不是 Selenium 自有的业务 API，也不是本轮浏览器 Network 抓包。

运行 `python <技能目录>/scripts/run_api.py <项目输出目录>`。依赖为 httpx（代理要求时包含 socks 支持）和 jsonschema；若缺少依赖，不改变全局环境，可将缺少的包安装到项目隔离目录。运行器使用默认环境代理，不绕过网络限制或 TLS 校验。

只允许 cases.json 内的公共 api.github.com /repos/SeleniumHQ/selenium/git/trees/ GET 路由，使用无认证公开访问；不读取用户 Token，不创建仓库、提交或其他数据。限流/认证限制/网络异常记 Blocked，不循环重试；不自动跟随重定向。记录所有失败。

来源 source-and-official-docs 表示官方源码和文档已确认；禁止写成 OpenAPI 或 browser。统一数据版本1.1.0 扩展来源枚举；导入旧 autoSkill 前需显式更新校验器或转换，不静默改成不真实来源。

每次运行创建新 RUN 目录，保存状态码、结构断言、响应内容校验值、目录 SHA 与响应大小作为证据；不保存完整目录和任何认证信息。结果记录用例内容哈希和修订号。响应 truncated=true 且所需路径缺失时记 Blocked，不能据此判断文件不存在。已确认路径存在且其他断言成功时可通过相应用例，但不得宣称整个目录完整。
