# 页面录制与断言

下列命令沿用技能入口绑定的程序路径、`--runtime-context` 和本任务的 `--session`。

## 录制时怎么操作页面

`record start` 和每次操作之后都会返回 `snapshot`：页面上可操作的元素，每个带 `@e编号`。**编号只对最近一次 snapshot 有效，每次操作后都可能变**（新增一条数据后会多出新元素）——始终用最近一条命令返回的 snapshot 里的编号；页面变了就看新返回的 snapshot，或运行 `autoskill browser snapshot`。要对标题、提示文字、表格内容做断言时，用 `autoskill browser snapshot --all` 找到它们的编号。

主文档的原生快照可能给无名或重名控件附上 `hint="..."`，包含图标、DOM 属性或邻近行文字。用提示区分当前 `@e编号`；提示不是控件的可访问名称，不能把它拼成 `role=button:提示内容`，也不能用来代替业务断言。需要稳定定位器时，继续用 `browser describe` 读取真实定位定义。

| 目的 | 命令 |
| --- | --- |
| 点击 / 双击 / 悬停 | `browser click @e3`、`browser dblclick @e3`、`browser hover @e3` |
| 输入（先清空） | `browser fill @e3 "文字"` |
| 下拉选择 | `browser select @e3 "选项的 value"` |
| 勾选 / 取消 | `browser check @e3`、`browser uncheck @e3` |
| 按键（作用于当前焦点） | `browser press Enter` |
| 跳转 / 刷新 | `browser open /orders`、`browser reload` |
| 查看 / 切换标签页 | `browser tab list`、`browser tab t1`（编号以列表为准） |
| 新建 / 关闭标签页 | `browser tab new /orders`、`browser tab close`（当前页）、`browser tab close t2`（指定页） |
| 文字验证码 | `browser captcha <验证码图片> <输入框>`（本地模型识别字母数字并填入；回放时重新识别） |
| 页面弹出确认框 | `browser dialog accept` / `browser dialog dismiss`（回放时确认框自动点确定） |
| 只是查看，不记录 | `browser snapshot`（可操作元素）、`browser snapshot --all`（含标题、文字、表格）、`browser get text @e3`、`browser get url`、`browser wait 800` |
| 进入嵌套区域 | `browser frame <目标>` 进入 iframe；`browser shadow <宿主>` 进入开放的 Shadow DOM；可交替多层进入 |
| 返回或查看区域 | `browser context parent` 返回一层；`browser context top` 返回主文档；`browser context show` 查看当前路径 |
| 录错了一步 | `autoskill record undo`（只撤销记录，页面不会回退） |
| 放弃这次录制 | `autoskill record cancel` |

目标也可以直接写 CSS 选择器，例如 `browser click ".todo-list li .toggle"`。

**链接打开新标签页或新窗口时**，会自动跟到新页面，返回 `tab` 编号和该页的 snapshot；打开、切换和关闭返回都会录入用例，Playwright、Selenium 和导出的独立脚本都按记录执行。要返回旧页面，用 `browser tab list` 查看后再 `browser tab t1`；每次切换后都使用新返回的元素编号。标签页编号在一次录制内不复用；关闭最后一页请用 `record stop` 或 `record cancel`。新页没有打开或已关闭会明确报错，不会拿另一个页面代替。

**密码等敏感内容不要直接写在命令里。** 先 `autoskill secret set 名称 值`，输入时写 `secret:名称`，例如 `browser fill @e3 secret:ORDER_PASSWORD`。用例里只会保存名称。

## 断言：没有断言的用例只证明"能点完"

每个场景至少写一条 `autoskill check`，验证业务结果而不是"页面没报错"。

```
autoskill check text      <目标> "期望的完整文字"
autoskill check contains  <目标> "包含的文字"
autoskill check count     ".todo-list li" 2          # count 的目标用 CSS 选择器；数量随数据变化时用 min_count
autoskill check min_count "table tbody tr" 1         # 至少有 1 行
autoskill check visible   <目标>          # hidden 同理
autoskill check value     <目标> "输入框里的值"
autoskill check checked   <目标>          # 未勾选：autoskill check checked <目标> false
autoskill check attribute <目标> "期望值" --attribute class
autoskill check url_contains "/orders/"   # url、title 不需要目标
```

`<目标>` 可以写 `@e编号`（来自 `browser snapshot --all`）、CSS 选择器（如 `.todo-count`）、`xpath=//button[@id='save']`、`testid=名称`、`label=标签`、`role=button:名称`、`text=文字`。其中 `text=` 要求与元素的**完整文字完全一致**，`testid=` 只在元素确实有这个 `data-testid` 时可用。快照可能提供属性提示，但不列出全部定位器；想知道某个元素有哪些可用写法，运行 `autoskill browser describe @e5`，它会列出可直接使用的目标及结构化 `locator_definitions`。明确输入的 XPath 保留为主定位；录制自动生成的 XPath 经真实 DOM 唯一命中检查后放在末位备选，不会替换稳定的 testid、标签等定位。

动态列表中的同名按钮若 `describe` 只给出序号定位，先读取目标卡片或行的实际 HTML、属性，找到真实稳定的业务标识或可见名称，以它锚定容器，再用显式 XPath/CSS 操作并核对目标身份。当前的 `nth` 只表示位置，不能代替业务身份；不要猜选择器，也不要让用户填写。列表筛选或重排后重新确认目标，保留原业务预期。

表单控件共用标签时，用快照里显示的角色和名称区分它们，例如 `role=checkbox:订单通知` 与 `role=switch:订单通知`。录制会保留所选控件的角色、名称及可用的唯一标识。同一角色也重名时，用快照编号和 `describe` 核对唯一标识；仍有多个匹配时，回放会报告歧义。

iframe 和 Shadow DOM 按当前区域逐层进入，`snapshot`、`describe`、操作和元素断言都使用同一范围，保存的步骤带完整上下文路径。跨域 iframe 由浏览器驱动进入；XPath 不能跨 iframe 或 Shadow 边界。开放 Shadow DOM 内优先使用编号、CSS 或语义定位，自动 XPath 另存当前根内唯一的 `xpath_anchor` 元素锚点并使用相对表达式，不要把带锚点的定位截成单独 XPath。封闭 Shadow DOM 明确不支持；区域不存在不能当作元素隐藏或数量为零。打开地址或切换标签页回到主文档，刷新会重新解析原路径。`get url/title` 和地址断言始终指当前标签页顶层地址。断言不成立时保留 Fail、原 expected 与实际 actual；依据独立需求诊断差异，只有需求明确变化才维护 oracle，不能按错误现状改绿或删除断言。

期望值应来自需求或业务规则，而不是「页面现在显示什么就写什么」：后者只能当回归基线，发现不了页面本来就有的错误。

每项业务预期都要有对应检查。选择能验证直接结果的稳定断言，按需求覆盖本次操作的全部验收点。

验证方向键移动 DOM 焦点时，在按 Enter 前检查目标选项已获得焦点。例如，页面用 `data-channel="短信"` 标识该选项，按方向键后执行 `autoskill check count 'css=[data-channel="短信"]:focus' 1`，再按 Enter 验证最终选择。

- 断言自己造的数据：列表里 `contains` 刚新增的那个名字、成功提示、跳转后的 `url_contains`。
- 不断言会随数据或环境变化的东西：总条数（用 `min_count`）、排序位置、时间、自增 ID、分页、别人的数据、整段文案。
- 目标优先用 `testid=`、`label=`、`role=`；不要用带层级和序号的长 CSS（`div:nth-of-type(4) > span`）。
- 断言对象在列表里时，断言列表容器「包含」某文字，而不是「第 N 行等于」。

## 增删改查：数据自己造、自己清

**需要唯一的输入一律写 `{{RAND}}`**（例如 `browser fill @e7 "工单-{{RAND}}"`，断言也写 `"工单-{{RAND}}"`）：录制时填入一个随机串，每次回放换一个新的，重复执行不会撞上「名称已存在」，也不会断言到上次留下的数据。

- 一条用例走完整的生命周期最稳：新增 → 查询到它 → 修改 → 确认已改 → 删除 → 确认查不到。它不依赖别的用例留下的数据，自己清理干净。
- 需求要求分开验证时，每条用例仍然自己先新增数据，再做改或删；不要依赖「上一条用例新增的那条」。
- 录出来的接口用例会自动串起来：新增接口返回的 ID 变成变量传给后面的查询、修改、删除接口；`{{RAND}}` 同样会带进接口请求体和断言。
- 删除前的确认框：页面内的弹层按普通元素点击；浏览器原生确认框用 `browser dialog accept`。

## 录制可复用的自动登录

`autoskill context <项目>` 会告诉你项目有没有登录用例。

- **已有登录用例**：直接 `record start`。它会先自动登录，录出来的用例自动依赖登录用例；以后回放时也会自动先登录，登录态保存在本机复用，过期自动重登。
- **还没有登录用例，且已有可自动使用的账号密码**：在开始业务场景前录一次登录本身——
  ```
  autoskill secret set ORDER_PASSWORD <密码>
  autoskill record start <项目> --as-login
  autoskill browser fill @e2 "账号"
  autoskill browser fill @e3 secret:ORDER_PASSWORD
  autoskill browser click @e4
  autoskill check text testid=current-user "张三"        # 断言"已登录"的标志，之后用它判断登录态是否有效
  autoskill record stop --title "登录"
  ```
  先用 `run-status` 等待登录候选验证和发布，再继续业务录制。账号密码使用用户提供或已配置的信息，不要猜；用户选择手工登录时不要求提供密码。
- 登录页有文字验证码（字母数字图片）：`autoskill browser captcha <图片> <输入框>`，图片的编号用 `browser snapshot --all` 找。识别偶尔会错，回放时会自动换一张重试，最多 3 次。
