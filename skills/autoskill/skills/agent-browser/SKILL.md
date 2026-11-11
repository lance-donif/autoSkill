---
name: qa-agent-browser-adapter
description: autoSkill 内部浏览器适配模块，执行功能测试并采集截图与网络证据。
---
# 浏览器适配
本模块是适配说明，不是 Vercel 官方 Agent Browser。先检查宿主浏览器工具或 `command -v agent-browser`。有可用插件时优先按其说明操作；外部 CLI 的命令和当前能力先通过其本地 --help 或官方文档核对，不凭记忆生成命令，不安装不明同名包。
执行前读取项目授权 origin、用例和账号引用。登录、实时检查 DOM/截图、按真实可见元素定位，逐步操作并断言预期；登录和状态变更也需要授权。不得绕过验证码或登录验证。
可用时捕获 Fetch/XHR、请求详情、Console、HAR、截图和时间。浏览器工具不支持网络导出时记录限制，不伪造 HAR。捕获 API 标来源 browser，记录关联用例和证据；不能因此宣布接口测试通过。
结果按 contracts 写 execution/ui-results.json，逐条断言和相对证据路径。缺环境/账号/工具标 Blocked，未尝试为 Not Run。敏感原始 HAR 只在隔离临时目录处理，脱敏后保存；Cookie/Token 不进报告。适配器不能自动清除图片中的秘密，保存前检查画面。

## 内网完整包

优先直接读取主Skill的 `references/agent-browser-core.offline/SKILL.md` 及其本地 references。官方发现入口仍保留来源与许可证，但内网运行不得执行 `skills get`、`install`、`upgrade` 或 `npx`。只使用完整包内的离线包装器和随附浏览器；缺失时标Blocked，不下载。命令参数以本地指南核对，测试origin白名单必须随每次会话提供。
