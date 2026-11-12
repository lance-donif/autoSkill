# 完整 CAPTCHA solve 链路基准

`benchmark_solver_pipeline.py` 测量真实 `captcha.solve(png)`：PNG 解码、合成背景、缩放/张量、候选亮字恢复、所有 ORT 调用与 CTC 解码。它不使用标签、不输出识别字符串，也不报告准确率。工具编写阶段仅做 Ruff/语法检查，未运行推理或 benchmark。

从仓库根目录使用现有环境执行。`--output` 必须是不存在的新目录；以下命令会开始真实 CPU 推理，应在独立验收与候选冻结后运行一次，不作为参数搜索：

```bash
.venv/bin/python training/captcha-research/benchmark_solver_pipeline.py \
  --research-dir /workspace/autoSkill-release/captcha-research \
  --manifest /workspace/autoSkill-release/captcha-research/datasets/manifest.jsonl \
  --input-root /workspace/autoSkill-release/captcha-research \
  --original-source /workspace/autoSkill-static-checks/skills/autoskill/app/autoskill/captcha.py \
  --original-model /workspace/autoSkill-release/captcha-research/baselines/original-be9dd198/captcha-ctc.onnx \
  --original-meta /workspace/autoSkill-release/captcha-research/baselines/original-be9dd198/captcha-ctc.json \
  --candidate-source "$PWD/skills/autoskill/app/autoskill/captcha.py" \
  --helper-dir "$PWD/skills/autoskill/app/autoskill" \
  --candidate-model "$PWD/skills/autoskill/models/captcha/captcha-ctc.onnx" \
  --candidate-meta "$PWD/skills/autoskill/models/captcha/captcha-ctc.json" \
  --output /workspace/autoSkill-release/captcha-research/solver-pipeline-new-run
```

默认固定资产：旧 source `18b899e0…`、旧模型 `be9dd198…`；新 source `ae7c69e1…`、helper `202432c2…`、模型 `b58376af…`；原 manifest `c2c755ea…`。完整 SHA 常量位于工具的 `EXPECTED`，所有默认哈希都必须匹配。`--*-sha256` 仅供显式迁移固定实验身份，不会自动接受路径中的新版本。old/new source SHA 必须不同。两份 `captcha.py` 按已验证的源字节分别编译，绕过旧 pyc；相对 `.paths`、`.captcha_image` 从指定候选包目录按各自 SHA 加载，保留真实包上下文。未依赖或导入 DDD。

协议固定为两种配置：ORT default（intra/inter 均保留 0）和 intra=2/inter=1；每种依次启动 original/candidate/candidate/original/candidate/original/original/candidate，共 16 个顺序执行的独立子进程，每种配置每个模型 4 blocks。每 worker 只有一个 CPU ORT session，单独量 init 后通过 `_loaded` 注入计时代理。worker 环境固定 `OMP_NUM_THREADS=1`、`OPENBLAS_NUM_THREADS=1`。

每 block 严格使用原 manifest 中 `plain`、`pale`、`through-line`、`first-symbol-touching` 的前 8 个 dev 条目，按上述类别和 manifest 次序拼成 32 图，并核对 PNG 文件 SHA。只加载这 32 个输入文件，不加载 train/test/public/blind 图像。先进行 1 次 cold first solve，再对同一首图额外 warmup 5 次，最后逐图各测一次。PNG 文件读取在 warm solve 计时外；PNG 解码和一切图片处理仍在 solve 内。

输出包含 `run-metadata.json`、每 worker 的 request/stdout/stderr，以及完成后唯一的 `solver-pipeline-benchmark.json`。记录工具/source/helper/model/meta/manifest/32 个输入的 SHA、包版本、CPU provider、平台、线程环境和每次 solve 的 `solve_ms`、全部 `session.run` 累加的 `inference_ms`、`run_count`。候选恢复后为空再 fallback 会计入第 2 次 run，不会只量最后一次。ValueError 失败尝试保留在统计并单列 errors；其他 worker 错误中止，不写完整 summary。

warm 统计按整体/逐类别报告 median 与 nearest-rank p95，同时保留逐 block、逐图原始值。cold 的 import/input-byte-prepare/session-init/first-solve 与 warm 分开；process-cold 从 worker 内首次科学库导入前计到首次完整 solve 后，不含 Python 启动与工具标准库导入。小源码 SHA 检查包含在 import 内；首次 lazy helper/Pillow 导入包含在 first solve 内。峰值 RSS 使用 Unix `resource` 的进程 lifetime peak（Linux KiB，macOS bytes 转 KiB），在测量结束、批量资产复核前采样。

**不清空 OS 页缓存。** 父进程预检的流式哈希也可能预热文件缓存，因此 process-cold 只表示新进程冷导入/初始化，不表示磁盘冷缓存。模型 SHA 一律用 `hashlib.file_digest` 流式计算，避免整模型 `read_bytes()` 人为抬高 RSS；大文件哈希不计入 cold/warm 时间。测量后再次校验资产和实际 PNG buffers，发现变化即拒绝完成结果。

参数均有显式 flags；可选环境变量包括 `CAPTCHA_RESEARCH_DIR`、`CAPTCHA_SOLVER_MANIFEST`、`CAPTCHA_SOLVER_INPUT_ROOT`、`CAPTCHA_ORIGINAL_SOURCE`、`CAPTCHA_CANDIDATE_SOURCE`、`CAPTCHA_SOLVER_HELPER_DIR`、`CAPTCHA_BASELINE_MODEL/META`、`CAPTCHA_CANDIDATE_MODEL/META`，以及 `CAPTCHA_SOLVER_<ASSET>_SHA256`（例如 `CAPTCHA_SOLVER_ORIGINAL_SOURCE_SHA256`）。flags 优先，不改变固定分组、输入数量、warmup 或两种线程配置。
