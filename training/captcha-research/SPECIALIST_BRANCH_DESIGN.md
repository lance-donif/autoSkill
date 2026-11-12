# 条件专家模型接口设计（未实施）

状态：只读设计。没有下载、转换、训练、推理、计时或替换模型。v4.1 残差候选已否决，后续不再据其盲测调参数。以下设计保持已固定的 v4.1 物理条件；专家是否有效、权重是否可分发尚未验证。

后续记录：本文件保留早期设计快照；后来预注册的原型改用稳定源 greedy 的 ArgMax→查表→OneHot63，没有实施下面的概率聚合建议。实际规范化、转换、单次 train/dev 与否决结论见 [固定条件专家原型](CONDITIONAL_SPECIALIST_V1.md)。

## 单次条件路由

外层输入继续是产品现有 `[1,3,48,320]` FP32 BGR tensor，预处理和 `captcha.solve` 不改。外层输出可为 `[1,None,63]`：现有产品逐帧 argmax/CTC collapse 没有写死 40 帧，最后一维仍使用现有 blank+62 alnum 顺序，输出仍只有一个 tensor。

先在输入 tensor 上算固定稀疏贯穿线 bool，再用标量 `If`：

- `else_branch`：全部 B583 节点与原 initializer 在子图内，直接使用原输入，产生 `[1,40,63]`。
- `then_branch`：输入适配、全部专家节点和词表映射在子图内，产生 `[1,T_expert,63]`。

这样每张图只执行一个 backbone。不能先在外层计算两个 backbone，再用 `Where` 选结果。条件和字节还原等轻量运算留在外层；专家 resize 也放在 then 中。专家识别失败后再运行 B583 会产生两次 backbone，不属于这个单次路由方案。

`If` 两支的 dtype/rank 必须相同，时间维可不同，外层声明动态时间维。不要把专家时间轴插值、截断或硬压成 40 帧；那会改变 CTC 的重复字符/blank 结构。原 B583 权重和非触发图的 logits 通路保留，仍需实测子图优化后的数值和字符串一致性。

产品当前每次输入一张图。ONNX `If` 要求标量条件；如果以后要求动态 batch 的每图独立路由，需要额外设计 `Loop` 加变长时间轴的 padding/拼接，不能对 batch 做 any(flag) 后把整个 batch 都交给专家。

## 3×48×320 到 1×64×动态宽度

1. 以真实非零列求有效宽度，只裁产品右侧零 padding，保留左右边缘。输入源是先 resize 到 uint8 再归一化，`Round(127.5*(x+1))` 能还原这个已经缩放过的 BGR 字节图；再按顺序恢复 RGB。
2. 按匹配权重版本的 ddddocr wrapper 做 RGB resize 到高 64，宽度按其 `int(valid_width*64/48)`，然后转 L 灰度，再作该权重的规范化，扩展成 `[1,1,64,W_expert]`。不得将 resize/灰度的整数舍入顺序互换。
3. 宽度 320 时新宽最多约 426，但这里拿到的已经是产品缩到 48 高的图。第一次 bilinear resize 丢失的细线/字形信息无法通过升到 64 恢复；原图片若被 320 上限改变过比例，也无法从 tensor 推回原始比例。这是效果上限。

只读调查报告：ddddocr 1.6.1 wrapper 使用先 RGB LANCZOS resize、再 L、最后 `[0,1]`；旧 canonical wrapper 可能使用 `[-1,1]`，其与具体模型 SHA 的对应关系仍在核查。不能仅因最新 wrapper 版本而猜测旧权重的训练规范化。应先固定模型 SHA、匹配的源版本、逐步输入 reference contract，再决定转换。

标准 ONNX `Resize` 不提供 PIL LANCZOS；直接换成 linear/cubic 是输入行为变化，不能声称完全复现。若要求严格对齐，可研究固定高 48→64 的稀疏 Lanczos 系数表，以及有效宽 1…320 的固定 sparse tap/index 表，用 `Gather`、整数/双精度乘加、归一化和与 PIL 相同的中间舍入实现。该路线仍需核对 PIL 实际系数精度、横纵顺序、边界处理和 uint8 clipping；本设计没有生成表或声称已经等价。它也会增加表体积与转换复杂度。

## 8210 类到现有 63 类

只读调查称两个候选 charset 都有 8210 个唯一项、blank 为索引 0、包含全部 62 个大小写英数，但 old/beta 字符顺序不同。每个模型必须固定独立 charset SHA，并验证一对一映射；不能复用另一权重的索引。

先确认真实输出节点与维度顺序。当前模型声明的输出注解看似二维，而末端 Gemm 为 8210 类，注解不足以决定实际 rank。若输出是 logits，则先对 8210 类作 Softmax；若已有概率或 log-probability，则按实际语义处理，不能重复 Softmax。

按本路线预先固定的概率聚合定义：

- 目标 62 个 alnum 概率按现有 charset 顺序逐项 Gather。
- 原 blank 与其余 8147 个非 alnum 类的概率，直接 ReduceSum 成目标 blank。
- 将 blank 与 62 个概率 Concat 成 `[1,T,63]`，保持总概率质量，不把域外字符强行改成字母数字。

这是新输出语义：聚合后的 blank 可能大于原来任意一个单类，因而抑制字符或产生空结果。它不等价于原专家的全词表 greedy decoder；必须按固定 contract 单独评估，不把接口适配说成识别不变。该风险也不能靠看盲测后再换映射方案处理。

## 纯标准算子和跨平台风险

调查中的 `common.onnx` 约 **54.1 MB**，包含标准 Conv/LSTM、opset 12；`common_old.onnx` 约 **13.6 MB**，有 `ConvInteger` 和 `com.microsoft::DynamicQuantizeLSTM`。后者含厂商域算子，不满足本方案“纯标准算子”的直接合并前提；把 domain/name 改掉不能转换其计算。

标准版本也需将 opset 12 正确迁移到外层 B583/条件所用的 opset 17。不能只改 `opset_import` 数字：例如 Squeeze/Unsqueeze/ReduceSum 的 axes 签名跨版本有变化。转换后需要检查所有子图、LSTM 参数/方向/初始状态及推断出的输出 rank；原权重应逐项保留数据。

`If`、LSTM、Resize 或显式 resampling、Gather、ReduceSum、Concat 均可用标准算子表达；Windows/Linux 实际离线 ORT CPU 包仍须分别验证。只执行一个分支不等于只加载一份权重：模型体积、加载/优化时间和 RSS 可能接近两模型之和。54.1 MB 专家加 13.1 MB B583 已约 67.2 MB，尚不计适配常量；不能预先承诺更快冷启动或更低内存。

MIT 源码/包许可与权重的可再分发边界需分别确认，训练数据历史重叠也未知。许可调查未结束前，本设计不主张替换或分发权重。

## 若后续获准实施的验证顺序

先核对权重/源版本的灰度、LANCZOS、宽度取整与规范化 reference；再独立验证标准专家图、词表聚合及动态时间轴。之后才建立 `If` 包装：固定物理条件 NumPy/ONNX 一致；非触发图 B583 数值/字符串一致；触发图通过真实产品 solve；profiling 确认每图只执行一支；两平台离线 ORT 无自定义域依赖。

所有准确率路线只用 train/dev 做决策，单独冻结唯一候选后再使用新独立测试。任何贯穿线/普通/外部接受标准都应在看到新结果前固定。当前文档不选择专家、不修改阈值，也不安排下载或执行。
