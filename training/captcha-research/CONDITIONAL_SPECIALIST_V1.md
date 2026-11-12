# 固定条件专家 ONNX 包装原型（已否决）

结果：**REJECTED_FIXED_WRAPPER_DEV_GATE**。本任务只在原固定 **1200 train / 400 dev** 运行一次，不训练、不搜索 norm/resize/gate，不读取旧 test、公开或任何盲测图像。候选未替换产品，未提交任何模型、图片或缓存。

## 固定协议与结构

`specialist_protocol_v1.json` 在全部 dummy/准确率推理前写入，固定 ddddocr old 权重 `b8f2ad9c…`、其独立 8210 项 charset、原始 B583、v4.1 gate 及输入适配。官方 1.4.11/1.5.6/1.6.1 对应权重 initializer 内容相同；本轮固定旧 1.5.6 的 `[-1,1]` 规范化，不尝试新 wrapper 的 `[0,1]`。

模型输入 `[1,3,48,320]`，单输出 `[1,None,63]`。外层标量 `If` 在固定 v4.1 稀疏贯穿线条件为 false 时，只运行完整 B583；true 时只运行以下专家分支：

1. 裁产品有效宽度，将产品 tensor 还原 uint8 BGR，按 PIL 定点系数精确转灰度。
2. 固定 ONNX FP32 bilinear(gray)、half-pixel，放大到高 64、宽 `max(1,floor(validW*64/48))`，再作 `gray/127.5-1`。这是明确的适配，不声称复刻源 wrapper 的 RGB Lanczos→L，也无法恢复产品已缩到 48 高时损失的信息。
3. 原 old 专家输出 raw `[T,N,8210]`。保持源 first-index ArgMax，按模型自己的 charset 查表；blank 与域外 token 映到 0，转 `[N,T]` 再输出 OneHot63。它仅适配现有 greedy decoder，**不是校准置信度**，不做 8210 Softmax 或尾部概率聚合。

候选 SHA `cd9c8b0d3ea37958be30b9ab9995ec2a83fc07f9639ffc3415af041a18c24bf6`，体积 **26,722,594 bytes**。expert **127 个**原 initializer 内容与 B583 **249 个**原 initializer 字节全部保留。源模型陈旧二维输出注解修正为实际三维；opset 12→17 的 292 个源节点中，287 个序列化字节不变，仅 5 个 Unsqueeze 的 axes 属性改为等价 Constant tensor 输入。

适配器使用标准 ONNX 算子，批准使用的源 `com.microsoft::DynamicQuantizeLSTM` 保留；没有把它误称为标准算子。实际执行验证仅 Linux x64 / ORT 1.19.2 CPU；ARM/UOS/Windows 的源码注册证据不等于实机通过。

## 推理前 contract 验证

checker、包装模型/源转换图/adapter/B583 的 CPU session load 均通过。有效宽 **1、2、7、100、147、320** 的专家输入宽分别为 **1、2、9、133、196、426**，输出时间帧数分别为 **1、1、2、17、25、54**。全部 dummy 的专家 OneHot 与源 greedy 完全一致，源 ArgMax ties 取第一项，域外字符变 blank；白图、暗背景、彩线三类未触发 dummy 与直接 B583 概率差为 0。

定点灰度与 Pillow 逐元素一致。初始 float64 半像素参考在宽 320、x=400 的归一化差达到 `2.840e-5`；诊断证实 ORT 以 FP32 scale/coordinate 执行。改正独立参考的算术精度后，最大差为 **3.576e-7**。原模型和插值设置没有改变，不声称任意输入逐位一致。

实际 ORT profiler：false 分支执行 **107 个 base / 0 个 expert kernel**，true 分支执行 **0 个 base / 255 个 expert kernel**，确认单张图只运行一个 backbone。一次 dummy 首次 run 分别约 25.28 / 7.55ms；加载约 298.84 / 87.43ms，这些不是控制负载的 benchmark。

## 固定 train/dev 单次结果

使用真实产品 loader、预处理和 `captcha.solve` decoder。1600 张中 **439** 走专家、**1161** 走 B583；实际产品输入的物理 flag 与冻结值全同，专家输出全为合法 OneHot63，未触发 B583 概率对 teacher cache 最大差 **3.576e-7**。全程 1 个空输出，按识别失败计入指标。

| dev 分组 | B583 exact | 包装候选 exact |
|---|---:|---:|
| 普通（200） | 178 | 178 |
| pale（40） | 33 | 33 |
| through-line（40） | 29 | 26 |
| dark-through-line（40） | 26 | 24 |
| first-symbol-touching（40） | 2 | 3 |
| dark-first-symbol-touching（40） | 9 | 10 |
| 全部（400） | 277 | 274 |

普通组和 pale 字符串变化 0。总体 dev CER **161→223/2000**；浅色首位只净增 1，低于预先要求的至少 2，两个贯穿线组均退步，故否决。未因失败而切换 norm、映射、插值或权重，也未送新盲测。

单次数据 pass 的 ORT inference 中位：B583 **14.10ms**、专家 **5.59ms**。这个观察没有控制负载，不作为独立速度优化结论；更快的失败识别分支不足以晋级。

## 工具与证据

- `build_conditional_expert.py`：固定资产 SHA、opset/注解转换、权重保全、If/adapter 构建。
- `check_conditional_expert.py`：PIL gray、FP32 bilinear、边界宽度、源 greedy/onehot、普通路径、实际分支 profiler。
- `evaluate_conditional_expert.py`：严格只允许固定 train/dev，一次真实产品评估并应用预注册门槛。
- `evidence/conditional-specialist-v1/`：build、dummy、静态转换证明、完整分组评价、运行锁与冻结记录。仅工具和 metadata 入 Git；模型、逐图预测与 profiler 原始文件留在仓库外只读研究目录。

历史 protocol 含固定本机资产路径与 SHA。跨机器复现需要在新的目录、首次预测前重新记录相同算法的本地路径/provenance，不能回写历史协议。命令依次为：

当前仓库的复现命令使用[冻结的恢复前 solver](HISTORICAL_SOLVER.md)，保留历史分支输入；新输出单独记录实际快照身份。

```bash
OPENBLAS_NUM_THREADS=1 python training/captcha-research/build_conditional_expert.py \
  --protocol /absolute/new-research/protocol.json --output /absolute/new-research/build
OPENBLAS_NUM_THREADS=1 python training/captcha-research/check_conditional_expert.py \
  --protocol /absolute/new-research/protocol.json --build /absolute/new-research/build \
  --output /absolute/new-research/dummy
OPENBLAS_NUM_THREADS=1 python training/captcha-research/evaluate_conditional_expert.py \
  --protocol /absolute/new-research/protocol.json --candidate /absolute/new-research/build/conditional-expert.onnx \
  --product "$PWD" --dummy /absolute/new-research/dummy --output /absolute/new-research/evaluation
```

下一路线是否让专用权重直接读取原 PNG，由后续独立决定；本任务没有改产品运行时代码。
