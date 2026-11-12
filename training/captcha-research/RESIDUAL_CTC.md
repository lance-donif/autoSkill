# 真正 CTC 时间上下文残差研究（v2/v3，已否决）

本任务提交可复现的研究工具和冻结证据，没有替换产品模型。唯一候选 `de5e0cc17b2f1a1232b6ca600c210c2254494fbd301121a8571a3e9f50ac52bd` 在独立验证后为 **REJECTED_NOT_PROMOTED**；产品保留速度模型 `b58376af…`。完整独立验证由另一任务记录，这里的拒绝摘要不参与训练或选参。

## 数据与固定基线

工具显式使用仓库外研究目录。先保留原始模型快照 `baselines/original-be9dd198/captcha-ctc.onnx`（SHA `be9dd198…`）、同目录原始 metadata（SHA `2641a736…`），以及导出基底 `baselines/speed-b58376af/captcha-ctc.onnx`（SHA `b58376af…`）。不从当前产品路径猜测 baseline。原始训练 checkpoint 未获得；本路线冻结全部既有 OCR 权重，只训练新残差。

`prepare_residual_data.py` 只读取最初合成集的 train/dev，补充配对干净图、深色字加同几何噪声、普通字轻微旋转/模糊。得到 **1200 train / 400 dev**，各 split 普通/噪声各半；标签、像素及字体族不跨 split，干扰符号不进标签。逐图参数见 `manifests/residual-v2-manifest.jsonl`（SHA `519b4c8d…`），字体路径及 SHA 见相邻 inventory。没有新增或读取 test，也未训练公开来源图片。

固定 be9 模型抽取 `[40,120]` 时间特征及 logits；标准化均值/方差仅从 train 求出。两个既有 RBF 校正在这 1600 张上的非零值计数均为 0，预注册的消融比例得到相同结果；此结论仅适用于该 train/dev，不能推断所有输入的 head 都不激活。

## 数学与选择过程

上下文半径 1，以边缘复制连接相邻三帧；tanh 隐层输出有界残差 `cap*tanh(raw/cap)`，加回原始 logits。输出层权重和 bias 零初始化，初始结果严格保持 baseline；使用求和所有合法路径的 log-space CTC forward/backward 梯度，辅以原本正确普通训练图的 teacher KL、残差 L2 和权重衰减。`residual_ctc.py` 的七项数学测试覆盖有限差分、重复字符、空标签、零残差和 KL。

`residual_protocol.json` 预注册四组参数、seed `2026111202`、每组最多 40 epochs。每次选择都要求普通组 exact/CER 不退、普通 baseline-correct 图 **0 新错**、至少两类噪声 exact 提升，并且总体 exact 净增。四组不缩放残差都因普通新错被拒绝；训练普通图均达到 600/600，开发集泛化不足。复跑四组 160 epochs 的非时间指标完全相同，外部保留每个 epoch 的诊断 checkpoint。

随后明确授权一次额外 dev 调参：固定全局 scale 网格 `(0,.03125,.0625,.125,.25,.5,.75,1)`，每个 checkpoint 只 forward 一次。1280 个组合仍使用原门槛，按事先固定效用排序，仅选出 trial 2 / epoch 29 / scale 0.25。选中权重 SHA 为 `37fa35bf…`。

| 开发集 | 原始模型 | 选中候选 |
|---|---:|---:|
| 全部 | 277/400 | 286/400 |
| 普通 | 178/200 | 182/200 |
| 浅色首位符号接触 | 2/40 | 2/40 |
| 深色首位符号接触 | 9/40 | 12/40 |

普通 baseline-correct 开发图新错 0。浅色首位目标没有改善，不能称为解决。开发集用于反复训练/选择，因此不是独立验收证据。

导出在固定 B583 图上增加同一残差，不改变原有权重、产品预处理或贪心 decoder。16 张分组 dev 的 ONNX/NumPy 概率最大差 `4.768e-7`；零残差对照最大差 `2.980e-7`、字符串变化 0；真实 `autoskill.captcha.solve` 的 400 张 dev 字符串与 NumPy 选择一致。旧 RBF 数学改写对任意输入并非逐位相同，参见先前 active-head 探测证据。

独立新盲测 exact **312→310/480**，plain 与 through-line 各退 1，首位接触无 exact 提升；公开兼容整体改善，但 Apache 样图退 1。因此本候选已否决，未用这些结果再调 scale、权重或门槛。

## 复现工具

需要原准备任务的 `datasets/manifest.jsonl`、对应像素和固定字体，既有 NumPy/Pillow/ORT，以及小型 `onnx` 包；不需要 Torch/Paddle。下面命令只在全新仓库外目录运行，不覆盖现有冻结证据。图像、特征缓存、epoch NPZ 和候选 ONNX 不进入 Git。

```bash
export CAPTCHA_RESEARCH_DIR=/absolute/path/outside-repository/captcha-research
export OPENBLAS_NUM_THREADS=1
python training/captcha-research/prepare_residual_data.py \
  --root "$CAPTCHA_RESEARCH_DIR" --manifest "$CAPTCHA_RESEARCH_DIR/datasets/manifest.jsonl" \
  --output "$CAPTCHA_RESEARCH_DIR/residual-v2/data"
python training/captcha-research/extract_residual_features.py \
  --root "$CAPTCHA_RESEARCH_DIR" --manifest "$CAPTCHA_RESEARCH_DIR/residual-v2/data/manifest.jsonl" \
  --directory "$CAPTCHA_RESEARCH_DIR/residual-v2/cache"
python training/captcha-research/train_residual_ctc.py \
  --root "$CAPTCHA_RESEARCH_DIR" --manifest "$CAPTCHA_RESEARCH_DIR/residual-v2/data/manifest.jsonl" \
  --cache "$CAPTCHA_RESEARCH_DIR/residual-v2/cache" --output "$CAPTCHA_RESEARCH_DIR/residual-v2/runs/replay"
python training/captcha-research/scale_residual.py \
  --root "$CAPTCHA_RESEARCH_DIR" --manifest "$CAPTCHA_RESEARCH_DIR/residual-v2/data/manifest.jsonl" \
  --cache "$CAPTCHA_RESEARCH_DIR/residual-v2/cache" --run "$CAPTCHA_RESEARCH_DIR/residual-v2/runs/replay" \
  --output "$CAPTCHA_RESEARCH_DIR/residual-v3/replay"
python training/captcha-research/export_residual.py \
  --root "$CAPTCHA_RESEARCH_DIR" --selection "$CAPTCHA_RESEARCH_DIR/residual-v3/replay/scale-training-and-freeze.json" \
  --product "$PWD" --manifest "$CAPTCHA_RESEARCH_DIR/residual-v2/data/manifest.jsonl" \
  --cache "$CAPTCHA_RESEARCH_DIR/residual-v2/cache" --directory "$CAPTCHA_RESEARCH_DIR/residual-v3/replay-export"
python -m unittest discover -s training/captcha-research -p 'test_residual*.py'
```

仓库副本将原实验对检查工具的动态导入改为纯 `residual_data.py`，并核对固定 metadata SHA；预处理已与真实产品函数通过逐元素 mock-session 测试，不运行 ORT。历史冻结脚本哈希与副本变更说明见 `evidence/residual-tool-provenance.json`。历史实验文件保持不变；重跑的代码 SHA 和目录路径应记录为新 provenance，不能冒称原冻结执行。

后续独立审查发现旧缓存只校验模型和数据，换 tensor 协议仍可能命中。现仓库工具还校验 tensor 映射、预处理函数/metadata/shape 及 NumPy/Pillow 版本的 contract；缺少 contract 的旧缓存会拒绝复用。复现时需在全新目录重新提取，不能给历史缓存补字段冒充新执行。历史冻结实验使用同一默认映射与预处理，经审查未受影响，原始文件保持不变。

`evidence/residual-ctc/` 包含数据/缓存元信息、RBF 消融、训练摘要、确定性复跑、scale 选择锁、导出冻结和拒绝状态。独立评估结果只用于接受或拒绝，未回流到本任务选参。
