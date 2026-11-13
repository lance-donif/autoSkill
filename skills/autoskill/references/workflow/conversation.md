# 页面到多轮接口与负载

1. 在页面真实进行一段对话，观察输入方式、反问、回答、流式完成和会话状态；不要从一个猜测 URL 直接开始压测。
2. 保存录制捕获，或 `conversation import --source <curl/HAR>`。查看候选请求与响应结构；cURL 只是被解析的资料，不作为 shell 执行。
3. 用 conversation build 的 mapping 指定 turns（请求序号）、question_pointer、answer_pointer、conversation_pointer/conversation_response_pointer、protocol 等。每轮 turn_overrides 提供独立 expected/checks/reference，复杂字段先根据实际元数据映射，未知项不编造。
4. 多轮属于同一 sample/case，维护该样本的 history、Cookie、conversation_id。不同样本及负载迭代独立；既不能把相关轮次拆成互不相干的测试，也不能让并发会话共享聊天状态。
5. 先 native run 验证对话与答案。用固定数据或明确资料计算 reference，绝不复制当前错误响应作为正确答案。然后再设置 load.concurrency/iterations 和性能目标。

宿主判定循环使用 judge next/submit。逐轮核对原任务、对话历史和独立事实：回答是否满足要求；反问是否必要、相关、已被回答；是否可以据已有事实回复并继续。提交简短 reason、clarification、facts_used/evidence_refs；action 为 finish、continue 或 reply。资料不足时保留 uncertain/needs_clarification，不猜出不存在的信息。

运行默认异步，宿主必须继续处理判定队列，不能用 --wait 阻塞唯一判定者。judge 耗时独立记录，不算被测模型的首次回答延迟。首个回答、语义完成、流结束、业务正确性和对话质量各自报告；一次 HTTP 200 或最后一轮成功不能覆盖中间失败。
