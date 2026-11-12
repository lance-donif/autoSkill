# 验证码模型实验：数据与原始基线

本目录记录独立实验产物。数据准备未修改产品源码或原始模型；已经验证的速度优化已合并 `main`。准确率候选单独冻结和验证，失败候选不进入产品。

原始模型 SHA-256：`be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4`。
本分支等价图优化模型 SHA-256：`b58376af1642a97b0df1be84642170c1f9a88203b86b062c3fe3d272e1759662`。

## 已完成的数据准备任务

- 公开来源：固定到 `AakashKumarNain/CaptchaCracker@21fe487174290b1616c342184cd3787cbe08c457`。两份原始归档经编码 SHA-256 和解码像素 SHA-256 去重，得到 **1040 张** 200×50、5 字符验证码；排除 **2080 个**重复副本。标签来自原作者文件名，字符集 `2345678bcdefgmnpwxy`。其 `LICESNE.md` 仅回指原始 Kaggle 数据页，当前网络无法读取原站许可，因此**许可未核实，仅隔离评估，不训练、不随产品分发**，不能宣称 CC0。
- 明确许可的公开小样本：`PatrickLib/captcha_recognize@e4ed7a1513ddcf3b2aec7620f4d68e7c90ddc5a4`，仓库附 **Apache-2.0**；README 规定文件名前缀为标签。取得 **12 张**作者发布的生成器样图（6 张 128×48、6 张 150×40）。这是有真实已知标签的公开样图，不是目标站验收集。
- 困难合成集：**480 train / 160 dev / 240 test**，四类分别为 plain、pale、through-line、first-symbol-touching。训练字体族 OpenSans/Noto，开发集 Liberation，锁定测试集 DejaVu；字体族、seed 范围、标签和像素 SHA 均不交叉。记录文字/背景灰度、对比度、线宽、曲率、相位、斜率和首位 hook/loop/slash 噪声。噪声符号**不进入标签**。
- 所有公开来源均作 external_test，未用于小头训练。原始模型训练样本、原始 checkpoint、NOTICE 所指训练 manifest 和源码在本机均不存在；无法验证与原训练集的历史重叠。
- `sources-manifest.json` 固定 URL、commit、原始归档 SHA、许可证证据和排除理由。仓库内 `manifests/manifest.jsonl` 与 `manifests/public-apache-manifest.jsonl` 是逐图元数据，不包含像素数据；准备工具将可复现的原始产物写入外部研究目录的 `datasets/`。

## 原始模型基线

环境：Linux x64、Python 3.12.14、ONNX Runtime 1.19.2 CPU、NumPy 2.0.2；3 个可见逻辑 CPU、cgroup 约 2 核配额。ONNX 1.17.0 为实验新增的小型依赖，未升级 ORT/NumPy，未安装 Torch/Paddle。

| 数据 | Exact match | CER |
|---|---:|---:|
| 公开 Fournierp（许可待核实） | 341/1040（32.79%） | 17.69% |
| Apache-2.0 公开小样本 | 7/12 | 20.00% |
| 锁定合成 test：plain | 56/60 | 1.67% |
| 锁定合成 test：pale | 56/60 | 1.67% |
| 锁定合成 test：through-line | 50/60 | 3.67% |
| 锁定合成 test：first-symbol-touching | 6/60 | 24.67% |
| 锁定合成 test：全部 | 168/240（70.00%） | 7.92% |

以上不是目标网站准确率，也不能由 12 张或自生成图推断普遍抗干扰能力。

已导出 `autoSkill-Captcha-Synthetic-20261112.zip`：**1,360 张自行合成图片、480 张相交验证 mask**，3,431,303 bytes，SHA-256 `a48194062f3cfc83351f1bd522734131ddbf358f68d9d69e54d6e99964b54883`。包含原 880 张分组数据与第一轮已经评估的 480 张盲测；后者不能再当作从未见过的测试集。压缩包不含公开数据像素、字体或模型；全部条目已解压比对。导出记录见 `evidence/synthetic-download.json`，文件保存在本次工作区的仓库外交付目录，未写入 Git 或 Release。

## 数据准备提交的范围

首次准备提交只包含公开来源调查、逐图标签/哈希 manifest、困难样本生成器、只读模型检查及原始模型基线，未包含原始/候选模型、图像像素、特征缓存或训练参数。`evidence/baseline-results.json` 保存完整分组 exact-match/CER/时延。

速度任务与 NumPy 小头、色调恢复等准确率路线分开冻结、验证和提交；当前提交不宣称准确率已经改善。

## 已独立完成的速度任务

`rewrite_distance.py` 将两个 RBF 校正头的距离计算改为完整公式 `||x||² + ||c||² - 2 x cᵀ`，保留特征真实范数，未偷换成 `2 - 2 x cᵀ`。原有 backbone、系数、半径和字符集不变；中心矩阵只作无损转置，新增范数常量由同一中心计算。

- 在固定主 manifest **1920 图**上，输出概率最大绝对误差 **0**、帧 argmax 与 CTC 字符串变化均 **0**；额外 **12 张 Apache 样图**的识别结果也相同。
- 同 CPU 2 线程，ABBA/BAAB 独立子进程每模型 4 块×32 个固定 dev 输入：warm 推理中位 **16.35→13.74ms（1.19×）**，p95 **25.62→21.85ms**；进程 peak RSS 中位 **168164→101996KiB**。
- 同 2 线程，process-cold（模块导入/输入准备/初始化/首次推理，不含 Python 启动，OS 缓存未清空）中位 **204.74→196.15ms**；模型初始化 **44.98→44.85ms**，首次推理 **29.49→15.97ms**。
- ORT 默认线程的中位 **18.72→11.39ms**，块间负载波动更大，只作为本机补充结果。
- 模型体积 **13055561→13061505 bytes**，此任务优化计算与中间内存，不承诺模型压缩或准确率提高。
- 内部数值诊断额外探测全部 1,244 个既有中心、邻域及范数极端值，共 5,065 个特征向量，覆盖真实激活的两个校正头；局部/扩展校正的最大绝对浮点差分别为 **0.001139 / 0.000347**。数学恒等式的 FP32 计算顺序不同，不能宣称任意输入都逐位相同；这组内部特征也不计为图片识别准确率。

证据位于 `evidence/distance-evaluation.json`、`evidence/paired-benchmark.json` 和 `evidence/distance-rewrite.json`。纯数学改写不训练、不重新选择字符标签；数据不是目标站验收集，不据此宣称普遍准确率提升。

### 复现速度任务

保留一份 SHA 为 `be9dd…` 的原始模型在仓库外，避免把当前已优化模型当作 baseline。重写工具会核对原始 SHA。

```bash
export CAPTCHA_BASELINE_MODEL=/absolute/path/outside-repository/original-captcha-ctc.onnx
export CAPTCHA_RESEARCH_DIR=/absolute/path/outside-repository/captcha-research
python training/captcha-research/rewrite_distance.py
export CAPTCHA_CANDIDATE_MODEL="$CAPTCHA_RESEARCH_DIR/candidates/rbf-distance-matmul.onnx"
python training/captcha-research/validate_distance.py
OPENBLAS_NUM_THREADS=1 python training/captcha-research/paired_benchmark.py
OPENBLAS_NUM_THREADS=1 python training/captcha-research/probe_distance_heads.py
```

`validate_distance.py` 不依赖小头训练产物；它加载固定 manifest，核对逐图数值/字符串与分组 exact-match/CER。benchmark 在独立进程内冷导入科学计算库，不同时保留两个互相争抢线程的 ORT session。原始测量值是一次固定实验的证据，复测时间会随宿主机负载变化。

## 已否决的准确率候选

色调恢复加上下文门控候选（`beafa0c…`）冻结后，独立生成并冻结了四类各 120 张的 **480 张新盲测**，与现有 1,932 条研究记录的标签、像素、字体文件及 seed 均无交叉。评估通过真实 `autoskill.captcha.solve`，没有替代产品预处理或解码。

| 评估集 | 原模型 / 速度模型 | 色调门控候选 |
|---|---:|---:|
| 新合成盲测 | 317/480 | 320/480 |
| 其中首位符号接触 | 5/120 | 8/120 |
| 外部公开集 | 341/1040 | 185/1040 |
| Apache 作者样图 | 7/12 | 3/12 |

候选在新难例上净增 3 张，但外部兼容集明显退化，**已否决，未替换产品权重**。速度模型在全部 1,532 张上的识别字符串与原模型一致。完整分组、CER、模型/数据 SHA 与局限见 [独立盲测报告](rejected/tone-context-gate/盲测报告.md) 及同目录 JSON；原始图像、预测、生成器和训练候选留存在仓库外冻结研究目录，未随产品分发。该失败结果不用于下一路线的参数选择。

## 复现

从 autoSkill 根目录运行，将工作目录显式指向仓库外。不要向当前 `models/captcha` 写候选模型。

```bash
export CAPTCHA_MODEL_DIR="$PWD/skills/autoskill/models/captcha"
export CAPTCHA_BASELINE_MODEL=/absolute/path/outside-repository/original-captcha-ctc.onnx
export CAPTCHA_RESEARCH_DIR=/absolute/path/outside-repository/captcha-research
mkdir -p "$CAPTCHA_RESEARCH_DIR/sources"
git clone https://github.com/AakashKumarNain/CaptchaCracker.git "$CAPTCHA_RESEARCH_DIR/sources/CaptchaCracker"
git -C "$CAPTCHA_RESEARCH_DIR/sources/CaptchaCracker" checkout 21fe487174290b1616c342184cd3787cbe08c457
git clone https://github.com/PatrickLib/captcha_recognize.git "$CAPTCHA_RESEARCH_DIR/sources/captcha-recognize"
git -C "$CAPTCHA_RESEARCH_DIR/sources/captcha-recognize" checkout e4ed7a1513ddcf3b2aec7620f4d68e7c90ddc5a4
# 准备工具使用现有 ORT/NumPy/Pillow；不需要 Torch/Paddle。
python training/captcha-research/prepare_datasets.py
python training/captcha-research/inspect_baseline.py
```

需要与 manifest 对应的系统字体文件，脚本中固定了字体路径，并记录每个字体文件的 SHA。图像、模型候选、缓存、完整预测和 profiler 原始 JSON 留在仓库外；可提交脚本、来源/标签 manifest、冻结记录和结果摘要。
