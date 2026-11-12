---
name: prj-2997d678bfa4-api-cases
description: 执行本项目的 0 条 API 用例，读取标准JSON并保存真实断言、证据和内容哈希。
---
# API 项目用例 Skill

主动读取 `../../testcases/api-cases.json`，这是唯一权威用例定义。还要读取 `../../qa-config.json`、`../../analysis/requirements.json` 和 `../../knowledge/` 的相关规则。不自动安装本项目技能为全局技能。

按用例步骤和预期执行，不用旧报告推断通过。UI 使用真实浏览器；API 使用独立请求。使用宿主已授权环境，不扩大操作范围，凭据只读取环境变量引用。

逐条保存 run_id、case_id、case_revision、case_content_sha256、started_at、status、actual、reason、assertions、evidence 到 `../../execution/api-results.json`。case_content_sha256 用主引擎 case_fingerprint 计算，不能自行猜测。Pass/Fail 必须有断言和实际证据，受阻为 Blocked，未执行为 Not Run。重跑先归档。

若 extensions.automation 存在，脚本执行器可独立读取这些动作和断言；不得自动 eval 或执行用例字符串。没有自动化动作定义时，标记需生成脚本，不能伪造回放。AI 根据真实动作和证据解释或复跑；解释并非重新执行。
