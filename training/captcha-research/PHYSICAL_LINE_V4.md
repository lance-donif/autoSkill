# v4 固定贯穿深灰线信号：仅诊断

这一步不训练、不导出、不应用残差，也不改变识别字符串。它只检查用户明确指定的“浅字上有近黑/深灰线贯穿整图”能否用固定物理条件在 train/dev 区分。阈值在运行前写入 `physical_line_protocol_v4.json`，未读取公开集或任何盲测来设参数。

真实 `autoskill.captcha.solve` 使用缓存 be9 logits 的 session double，捕获实际产品预处理 tensor；没有创建 ORT session。有效列以 `any(tensor!=0, axes=channel,height)` 判断，排除产品右侧全零 padding，保留有效图像左右边缘。真实 RGB uint8 像素经产品缩放后不可能三通道恰好全零。用 `rint(127.5*(tensor+1))` 还原整数；每个有效列只要有一个 `maxRGB<128` 且 `maxRGB-minRGB<=16` 像素即为覆盖，覆盖有效列比例 **≥0.90** 才触发。

| 类别 | train 触发 | dev 触发 |
|---|---:|---:|
| plain | 0/120 | 0/40 |
| ordinary-aug | 0/120 | 0/40 |
| paired-clean | 0/360 | 0/120 |
| pale | 0/120 | 0/40 |
| through-line | 82/120 | 29/40 |
| dark-through-line | 87/120 | 29/40 |
| first-symbol-touching | 82/120 | 23/40 |
| dark-first-symbol-touching | 83/120 | 24/40 |

普通组原本识别正确的 train **554 张 / dev 178 张**均无触发；全部普通 train 600 张 / dev 200 张也无触发。这不是“0 新识别错误”：本阶段没有改变任何预测。噪声中原本识别正确的 dev 99 张有 40 张触发，若以后加残差仍需检查新错和泛化。浅字但没有贯穿线的 pale 组不触发。

固定信号有当前 train/dev 上的物理区分价值，但遗漏部分线干扰，不能宣称通用抗线能力。保留阈值和边缘列，不因漏检而放宽。是否使用已训练 checkpoint 构造条件候选，需要下一项独立协议和判断；本结果不选择权重、scale 或模型。

四项轻量测试覆盖全幅线、普通局部字形、彩线、有效宽度与边缘、亮度 127/128、RGB 极差 16/17、全零 tensor。证据位于 `evidence/physical-line-v4/`，只含协议、元信息、计数和冻结 SHA，不包含图像或候选模型。

```bash
python -m unittest discover -s training/captcha-research -p test_physical_line.py
OPENBLAS_NUM_THREADS=1 python training/captcha-research/diagnose_physical_line.py \
  --root "$CAPTCHA_RESEARCH_DIR" --manifest "$CAPTCHA_RESEARCH_DIR/residual-v2/data/manifest.jsonl" \
  --cache "$CAPTCHA_RESEARCH_DIR/residual-v2/cache" --product "$PWD" \
  --protocol training/captcha-research/physical_line_protocol_v4.json \
  --output "$CAPTCHA_RESEARCH_DIR/residual-v4/new-physical-diagnostic"
```
