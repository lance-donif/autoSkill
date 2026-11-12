# 验证码模型实验：数据与原始基线

本目录是独立实验产物。两个 autoSkill 工作树的源码、配置和当前模型均未修改。当前生产模型 SHA-256：`be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4`。

## 已完成的数据准备任务

- 公开来源：固定到 `AakashKumarNain/CaptchaCracker@21fe487174290b1616c342184cd3787cbe08c457`。两份原始归档经编码 SHA-256 和解码像素 SHA-256 去重，得到 **1040 张** 200×50、5 字符验证码；排除 **2080 个**重复副本。标签来自原作者文件名，字符集 `2345678bcdefgmnpwxy`。其 `LICESNE.md` 仅回指原始 Kaggle 数据页，当前网络无法读取原站许可，因此**许可未核实，仅隔离评估，不训练、不随产品分发**，不能宣称 CC0。
- 明确许可的公开小样本：`PatrickLib/captcha_recognize@e4ed7a1513ddcf3b2aec7620f4d68e7c90ddc5a4`，仓库附 **Apache-2.0**；README 规定文件名前缀为标签。取得 **12 张**作者发布的生成器样图（6 张 128×48、6 张 150×40）。这是有真实已知标签的公开样图，不是目标站验收集。
- 困难合成集：**480 train / 160 dev / 240 test**，四类分别为 plain、pale、through-line、first-symbol-touching。训练字体族 OpenSans/Noto，开发集 Liberation，锁定测试集 DejaVu；字体族、seed 范围、标签和像素 SHA 均不交叉。记录文字/背景灰度、对比度、线宽、曲率、相位、斜率和首位 hook/loop/slash 噪声。噪声符号**不进入标签**。
- 所有公开来源均作 external_test，未用于小头训练。原始模型训练样本、原始 checkpoint、NOTICE 所指训练 manifest 和源码在本机均不存在；无法验证与原训练集的历史重叠。
- `sources-manifest.json` 固定 URL、commit、原始归档 SHA、许可证证据和排除理由。`datasets/manifest.jsonl` 与 `datasets/public-apache-manifest.jsonl` 是逐图元数据，不包含像素数据。

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

## 本次准备提交的范围

本次只提交公开来源调查、逐图标签/哈希 manifest、困难样本生成器、只读模型检查及原始模型基线。原始模型、候选模型、图像像素、特征缓存和训练参数都不在本次提交中。`evidence/baseline-results.json` 保存完整分组 exact-match/CER/时延。

后续的距离图优化、NumPy 小头和色调恢复实验各自独立冻结、验证和提交；本次不宣称准确率已经改善。

## 复现

从 autoSkill 根目录运行，将工作目录显式指向仓库外。不要向当前 `models/captcha` 写候选模型。

```bash
export CAPTCHA_MODEL_DIR="$PWD/skills/autoskill/models/captcha"
export CAPTCHA_RESEARCH_DIR=/absolute/path/outside-repository/captcha-research
mkdir -p "$CAPTCHA_RESEARCH_DIR/sources"
git clone https://github.com/AakashKumarNain/CaptchaCracker.git "$CAPTCHA_RESEARCH_DIR/sources/CaptchaCracker"
git -C "$CAPTCHA_RESEARCH_DIR/sources/CaptchaCracker" checkout 21fe487174290b1616c342184cd3787cbe08c457
git clone https://github.com/PatrickLib/captcha_recognize.git "$CAPTCHA_RESEARCH_DIR/sources/captcha-recognize"
git -C "$CAPTCHA_RESEARCH_DIR/sources/captcha-recognize" checkout e4ed7a1513ddcf3b2aec7620f4d68e7c90ddc5a4
# 准备工具使用现有 ORT/NumPy/Pillow；不需要 Torch/Paddle。
python /path/to/experiment-scripts/prepare_datasets.py
python /path/to/experiment-scripts/inspect_baseline.py
```

需要与 manifest 对应的系统字体文件，脚本中固定了字体路径，并记录每个字体文件的 SHA。图像、模型候选、缓存、完整预测和 profiler 原始 JSON 留在仓库外；可提交脚本、来源/标签 manifest、冻结记录和结果摘要。
