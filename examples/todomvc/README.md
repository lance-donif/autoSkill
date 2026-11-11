# 官方开源 TodoMVC：脚本可读用例样例

本目录附带 tastejs/todomvc 官方 JavaScript ES5 的已构建页面。11个site文件与上游Git blob逐一校验一致，未修改业务代码；来源和许可证见 source-manifest.json 与 LICENSE。

## 内容

- ui-cases.json：恰好20条UI用例，每条包含白名单动作、可执行断言、稳定ID和来源。
- api-cases.json：4条autoSkill控制台自身的只读API用例；TodoMVC本身没有业务CRUD API，不伪造接口。
- requirements.json / requirements.md：需求追踪与范围。
- case-skills/ui、case-skills/api：可主动读取的项目样例Skill，引用上述唯一模板定义。
- execution/：前置环境验证记录；UI未实际逐条执行，不把生成或格式校验当作通过。

## 在浏览器可用的本机复跑

终端一提供原版页面：
```bash
python -m http.server 8765 --bind 127.0.0.1 --directory examples/todomvc/site
```

终端二运行后台：
```bash
npm run dashboard
```

在用例库“新建项目”中选择TodoMVC示例。也可使用 `python scripts/seed-demo.py --root <autoSkill状态目录> --example todomvc` 导入，初始不会授权执行。确认后在项目配置设置：
```json
{
  "execution_authorized": true,
  "base_url": "http://127.0.0.1:8787",
  "allowed_origins": ["http://127.0.0.1:8765", "http://127.0.0.1:8787"],
  "approvals": ["write"]
}
```

选择“脚本”+“UI”或“接口”执行，不需要模型。write授权仅用于本机原版待办页上的合成任务；每条UI用例使用新浏览器上下文，不碰个人待办。不包含删除或批量清空。浏览器需先通过 `python -m playwright install chromium` 安装，也可通过插件chromium配置指定已安装的可执行文件。

本轮当前工作环境阻止云浏览器访问站点、本地回环地址以及非HTTP(S)页面，同时脚本Chromium启动因系统进程限制失败。因此本轮20条用例的交互步骤未执行；不能宣称TodoMVC真实UI测试通过。原Selenium索引依赖的4条GitHub接口已在此前真实HTTP执行通过，记录保留在examples/selenium中，与本例控制台接口不同。
