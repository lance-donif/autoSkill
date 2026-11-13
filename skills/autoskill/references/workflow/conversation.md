# 页面到多轮接口与负载

1. 在页面真实进行一段对话，观察输入方式、反问、回答、流式完成和会话状态；不要从一个猜测 URL 直接开始压测。
2. 保存录制捕获，或 `conversation import --source <curl/HAR>`。查看候选请求与响应结构；cURL 只是被解析的资料，不作为 shell 执行。
3. 用 conversation build 的 mapping 指定 turns（请求序号）、question_pointer、answer_pointer、conversation_pointer/conversation_response_pointer、protocol 等。每轮 turn_overrides 提供独立 expected/checks/reference，复杂字段先根据实际元数据映射，未知项不编造。
4. 多轮属于同一 sample/case，维护该样本的 history、Cookie、conversation_id。不同样本及负载迭代独立；既不能把相关轮次拆成互不相干的测试，也不能让并发会话共享聊天状态。
5. 先 native run 验证对话与答案。用固定数据或明确资料计算 reference，绝不复制当前错误响应作为正确答案。然后再设置 load.concurrency/iterations 和性能目标。

宿主判定循环使用 judge next/submit。逐轮核对原任务、对话历史和独立事实：回答是否满足要求；反问是否必要、相关、已被回答；是否可以据已有事实回复并继续。提交简短 reason、clarification、facts_used/evidence_refs；action 为 finish、continue 或 reply。资料不足时保留 uncertain/needs_clarification，不猜出不存在的信息。

运行默认异步，宿主必须继续处理判定队列，不能用 --wait 阻塞唯一判定者。judge 耗时独立记录，不算被测模型的首次回答延迟。首个回答、语义完成、流结束、业务正确性和对话质量各自报告；一次 HTTP 200 或最后一轮成功不能覆盖中间失败。

## 从一段 cURL 建立多轮用例

把页面复制的 cURL 原文保存为 `request.curl`，执行 `autoskill conversation import <项目> --source request.curl --format curl`，保留返回的捕获 id。`conversation show` 读取真实字段；只有请求、没有响应时，可先用 `{"turns":[1],"answer_pointer":"/"}` 构建一次探查，读取 `native status <运行> --case <编号>` 中的实际响应。探查没有独立预期，业务结果应为 Unverified。

确认接口实际使用 `/question`、`/answer` 和 `/conversation_id` 后，映射文件可写为下面的结构。示例数据仅演示独立预期的算法，真实任务必须使用用户资料或事先冻结的数据。

```json
{
  "turns": [1, 1],
  "protocol": "http",
  "question_pointer": "/question",
  "answer_pointer": "/answer",
  "conversation_pointer": "/conversation_id",
  "conversation_response_pointer": "/conversation_id",
  "turn_overrides": [
    {"question": "2025 年北区销售额合计是多少？", "reference": {"operation": "sum", "field": "sales", "where": [{"field": "region", "value": "北区"}, {"field": "year", "value": 2025}]}},
    {"question": "那平均每笔是多少？", "reference": {"operation": "average", "field": "sales", "where": [{"field": "region", "value": "北区"}, {"field": "year", "value": 2025}]}}
  ],
  "dataset": [{"region": "北区", "year": 2025, "sales": 40}, {"region": "北区", "year": 2025, "sales": 60}, {"region": "南区", "year": 2025, "sales": 300}],
  "load": {"concurrency": 1, "iterations": 1}
}
```

`turns:[1,1]` 是两次使用同一个请求模板，问题来自各自 turn_overrides，并保持为一条多轮用例。执行 `autoskill conversation build <项目> <捕获> --file mapping.json`，再 `autoskill native run <项目> <资产>`。先核对 100、50 这两个独立预期；业务回答为自然语言时，按实际结构配置 checks 或宿主 judge，不强行把整段自然语言与数字直接相等比较。

验证后才增加负载：将 `{"load":{"concurrency":3,"iterations":6}}` 保存为 `load.json`，执行 `autoskill native run <项目> <资产> --file load.json`。原生任务用独立输入与证据目录，同项目也可并行；本机队列总并发仍由 `AUTOSKILL_MAX_PARALLEL` 管理，单次资产内部的 load 是另一层虚拟会话并发。

## 流式音频与网页语音

已知真实 WebSocket 协议时，可在原生 `ai-eval` 资产的 endpoint 中提供音频输入，音频文件随完整目录一起导入：

```json
{
  "protocol": "websocket",
  "url": "ws://127.0.0.1:9000/voice",
  "audio_input": {
    "file": "synthetic.pcm", "encoding": "base64_json", "chunk_bytes": 128, "interval_ms": 2,
    "frame": {"type": "audio.append"}, "field": "audio", "end_event": {"type": "end_audio"}
  },
  "audio_output_pointer": "/audio"
}
```

`encoding` 可为 `binary` 或 `base64_json`；frame、字段和结束事件必须匹配实际协议。发送前冻结文件字节和 hash，双工接收二进制帧或指定字段的 base64，保存到 `evidence/audio/*.bin`；逐轮记录发送/接收字节数、分片数、hash 和 `timings.first_audio_ms`。文本转录仍需单独 expected/reference/judge；音频链路成功不能代表语音识别准确。

网页入口可使用示例站点 `/voice.html` 检查设备、双向媒体与停止后的资源释放。独立脚本 `scripts/verify-voice-loopback.py -v` 执行真实 Chrome WebRTC 回环；浏览器策略导致无可用 ICE 候选时保留 Blocked/失败证据，不改策略或断言制造通过。本次环境的页面检查通过，但真实媒体接收被受管 WebRTC 网络策略阻塞；这不是识别模型验收。
