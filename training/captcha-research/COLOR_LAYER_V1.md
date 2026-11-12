# 三层物理颜色恢复 v1（未通过预注册门槛）

状态 **REJECTED_FIXED_COLOR_LAYER_GATE**。这次研究有明显浅色目标收益，但深色首位组 CER 退步，因此未晋级，不改产品。它是识别链路前处理，不是训练权重改善；同一 B583 权重保持不变。

## 预测前固定的算法

`color_layer_protocol_v1.json` 在任何识别预测前固定。只接受原 1200 train / 400 dev，未读取 test、公开集或任何盲测。原 PNG 白底 RGBA 合成 RGB 后转 L，物理 gate 仍使用真实产品 PP48 tensor 的固定 v4.1。

对触发图执行精确三非空类 multi-Otsu，最大化 `sum(s²/w)`。只枚举非零 bins 的分割；float64 向量化 score，取距离最大值不超过 `16*eps*N*255²` 的候选，再用 Python 整数有理交叉乘比较，完全同分取最早 `(t1,t2)`。类内和全局 mode 均固定最小 gray argmax。

只有满足以下全部条件才恢复：三 mode 有序；最高层 mode 等于全局 mode；去除最低/最高 mode 后的全局最高频 gray 落在中层；低 mode 在原 RGB 近灰像素中覆盖至少 90% 原图列；中 mode 覆盖小于 90%。近灰固定 `maxRGB-minRGB<=16`，左右边缘全部计入，没有增加黑度阈值。任何失败保留 baseline。

恢复时，`gray<=t1` 一律置 255；其余为 `rint(clip(255*(gray-a1)/(a2-a1),0,255))`，生成同尺寸灰度 PNG，调用实际产品 `captcha.solve` 一次。没有形态学、字符长度猜测、插补或 label 依赖。

九项无模型测试通过：80 个小 histogram 与独立 Fraction 全搜一致；对称/大计数/空 bin 的最早 ties；已知字体 alpha 恢复在灰度量化界 2 内；low 删除；暗字落 low 时弃权；三类缺失、背景 mode、全局 tie；列覆盖 90/89% 边界；彩色 low 不作为灰线。

## 单次原 train/dev 结果

全 1600 张：**239 恢复 / 200 弃权 / 1161 未触发**。弃权原因全为 `remaining_peak_outside_middle`。实际 B583 只对 239 张恢复图推理一次，其他复用验证过的 baseline；物理 flags 全部与先前冻结记录一致。

| dev 分组 | baseline exact | 恢复 exact | baseline edits | 恢复 edits | 恢复 / 弃权 / 未触发 |
|---|---:|---:|---:|---:|---:|
| 普通（200） | 178 | 178 | 22 | 22 | 0 / 0 / 200 |
| pale（40） | 33 | 33 | 8 | 8 | 0 / 0 / 40 |
| through-line（40） | 29 | 33 | 12 | 9 | 29 / 0 / 11 |
| dark-through-line（40） | 26 | 27 | 19 | 18 | 4 / 25 / 11 |
| first-symbol-touching（40） | 2 | 18 | 55 | 28 | 23 / 0 / 17 |
| dark-first-symbol-touching（40） | 9 | 10 | 45 | 48 | 3 / 21 / 16 |
| 全部（400） | 277 | 299 | 161 | 133 | 59 / 46 / 295 |

普通与 pale 字符串变化 0，全 1600 无新增空输出或异常。浅色首位 **+16/40**，总体 CER **8.05%→6.65%**，但深色首位 **45→48/200 字符错误**，违反预先固定的每线噪声组 CER 不退条件。因此严格否决，未据结果加条件或放宽门槛。

像素处理主线程 CPU 中位 **1.164ms**、p95 **1.728ms**；wall 中位 **1.166ms**、p95 **1.747ms**；其中 histogram/Otsu wall 中位 **0.302ms**。主线程 CPU 排除了空闲 ORT worker 的自旋。以上是单次研究 pass 的成本，不是控制负载 ABBA；不能宣称没有吃掉已有模型提速。

## 工具和冻结证据

`color_layer_core.py` 是纯像素算法，`test_color_layer_core.py` 不加载识别模型，`evaluate_color_layers.py` 固定资产 SHA、真实产品 gate/recognizer 和本轮门槛。`evidence/color-layer-v1/` 保存全部分组数量、exact/CER、CPU 成本、源码和协议 SHA、最终失败状态。原图、逐图预测及运行缓存留在仓库外只读目录，未入 Git。

当前仓库的复现命令使用[冻结的恢复前 solver](HISTORICAL_SOLVER.md)，保留历史物理条件；新输出单独记录实际快照身份。

```bash
python -m unittest discover -s training/captcha-research -p test_color_layer_core.py
OPENBLAS_NUM_THREADS=1 python training/captcha-research/evaluate_color_layers.py \
  --protocol /absolute/new-research/protocol.json --product "$PWD" \
  --output /absolute/new-research/evaluation
```

历史协议保留固定本机路径及 SHA。复现应在首次预测前登记新目录 provenance，不回写历史运行。若后续提出新规则，须作为独立研究重新预注册；本版代码、参数和结果已经冻结。
