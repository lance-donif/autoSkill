# 原始 PNG 条件专家：固定 A/B 解码（均已否决）

本轮为独立的原始 PNG 研究，状态 **REJECTED_BOTH_FIXED_PNG_DECODERS**。没有改产品、训练、切换权重或剪枝；不读取 test、公开集及任何盲测。只用原固定 **1200 train / 400 dev**，两个事先定义的解码方案共用每张触发图的一次源推理。

## 预测前固定的协议

`png_specialist_protocol_v2.json` 在本轮首次预测前写入，固定 old 源 SHA `b8f2ad9c…`、其 8210 项 charset、旧 1.5.6 规范化及 v4.1 条件。源权重始终未经修改。

- 物理条件：由真实 `captcha.solve` 的 PP48 输入 tensor 计算，全部 1600 张与冻结 flags 一致。未触发图复用已验证的 B583 baseline text，不调用专家。
- 原图预处理：PNG→RGBA，在白底合成为 RGB；先按官方顺序 Lanczos 到高 64、宽 `max(1,int(w*(64/h)))`，再转 L；严格 `float32/255` 后 `(x-.5)/.5`，扩为 `[1,1,64,W]`。这次保留原始 PNG 信息，没有重建或放大 PP48 图。
- A：完整 raw8210 first-index ArgMax→源索引查目标表，unsupported/blank→0→greedy CTC collapse。
- B：按现有目标 charset 顺序 Gather blank+62 个 alnum raw logits，再作 first-index greedy。它是固定领域先验，不聚合尾部概率、不伪造置信度。
- 门槛：普通与无线 pale 字符串 0 变化；四个线噪声组各 exact 不退、CER 不高；浅色首位 exact 至少净增 2。只有满足者才按噪声 exact、浅色首位 gain、较低噪声 CER 选择，完全同分固定 B。

三个无模型测试已通过：透明像素的白底语义、RGB Lanczos→L 和规范化操作顺序逐元素一致、A/B 的域外竞争与 CTC 分隔语义。

## 单次结果

1600 张中 **439 张**调用源模型各一次；其余 **1161 张**完全没有专家推理。A/B 使用完全相同的 raw 输出，没有重跑模型来比较解码。

| dev 分组 | B583 | A | B |
|---|---:|---:|---:|
| 普通（200） | 178 | 178 | 178 |
| pale（40） | 33 | 33 | 33 |
| through-line（40） | 29 | 23 | 23 |
| dark-through-line（40） | 26 | 25 | 25 |
| first-symbol-touching（40） | 2 | 2 | 2 |
| dark-first-symbol-touching（40） | 9 | 12 | 12 |
| 全部（400） | 277 | 273 | 273 |

普通和 pale 字符串变化 0；总体 dev CER **161→214/2000**。浅色首位没有净增，两个贯穿线组 exact/CER 均退化，因此两个方案都否决，未选择唯一解码、未剪枝投影、未送盲测。

A/B 在全部 train/dev 上有 **37 条字符串不同**，即使表中聚合 exact/CER 相同。专家的 10975 帧里有 54 帧出现非 blank 域外 top1，分布在 50 张图；这个领域先验没有形成合格的正确率收益。浅色首位已触发的 23 张中，A 输出相对标签缺 2 字 5 张、缺 1 字 5 张、等长 11 张、多 1 字 2 张。以上只是失败描述，不用于改 norm、gate、映射或后续选参。

单次 pass 的预处理中位 **0.704ms**、源 ORT inference 中位 **5.981ms**。没有控制宿主负载，不能视为独立 benchmark 或产品速度承诺。真实 runtime 分流没有实施。

## 可复现工具与保留证据

`png_specialist_core.py` 保存纯预处理和两个固定解码；`test_png_specialist_core.py` 使用合成像素/logits 测试，不需要源权重；`evaluate_png_specialist.py` 只接受固定 train/dev provenance，使用真实产品预处理和缓存 baseline，输出两套严格门槛结果。loader/metrics 复用前一任务已冻结的研究 helper。

`evidence/png-specialist-v2/` 保存完整分组 exact/CER、运行锁、post-pass 描述、源码 SHA 和失败冻结记录。原始 PNG、源模型和逐图预测只在仓库外只读研究目录，未提交二进制。协议包含历史本机路径；跨机复现须在首次预测前为相同算法创建新路径/provenance，不得改写历史协议。

```bash
python -m unittest discover -s training/captcha-research -p test_png_specialist_core.py
OPENBLAS_NUM_THREADS=1 python training/captcha-research/evaluate_png_specialist.py \
  --protocol /absolute/new-research/protocol.json --product "$PWD" \
  --output /absolute/new-research/original-png-evaluation
```

原始 PNG 信息和固定 alnum 先验均未使本轮达到门槛。产品继续使用既有 B583，本研究停止于归档结论。
