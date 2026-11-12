# v4.1 唯一条件残差候选导出（已否决）

本步骤只导出已授权的 **trial 2 / epoch 13 / alpha=1**，权重 SHA `b9d07631…`；不重新训练、不重新选 checkpoint、不改阈值。v4.1 的 1600 train/dev flags 与此前 v4 逐个相同，因此沿用原 dev 选择。候选已由第三轮独立盲测否决，状态 **REJECTED_NOT_PROMOTED**，**未替换产品模型**。

候选 SHA-256：`1e6ac899871225561ff08ee01eed29762266a66034676b0d1bb67dade575e5fe`。实际文件保存在仓库外冻结研究目录，未进入 Git。教师固定为原始 be9，导出基底固定为速度模型 B583；**249 个 B583 initializer 的序列化字节全部保持一致**，输出仍只有原来的 CTC 概率。

ONNX 条件从模型输入 tensor 直接计算：有效列排除零 padding；FP32 `Round(127.5*(x+1))` 恢复整数；有效列近黑/近灰覆盖 ≥90%，同时有效像素中同一 mask 占比 ≤50%。条件在完整 `cap*tanh` 残差之后相乘，再加回原 logits。条件不触发时通过原基底 logits，不改变产品预处理、decoder 或支持的字符范围。

## 已完成的导出一致性验证

- **400 张真实产品输入 tensor**：NumPy 条件、ONNX 条件与冻结 dev flags 完全相同，bool 变化 0。
- 真实 `autoskill.captcha.solve` 对 **400 张 dev**的字符串与冻结 NumPy 选择完全相同，变化 0，空结果错误 0。使用真实产品 loader、预处理和 decoder；session observer 只记录其真实 ORT 输入/输出。
- 每类同时抽取触发/未触发代表，共 **16 图**：候选 ONNX/NumPy 概率最大绝对差 **2.980e-7**；零残差对照同为 **2.980e-7**，对照字符串变化 0。这是分组数值验证，不是任意输入逐位等价保证。
- 小型条件子图的 **15 个边界 tensor**及整批输入验证通过：全幅细线、彩线、局部字形、127/128、RGB 极差 16/17、覆盖 90%/89%、暗像素恰好 50%/超过 50%、全黑、黑底白字、零 tensor。条件 bool 与广播乘数相同。
- 原始产品模型、metadata、NOTICE 均未写入，test/external 图像读取为 0。子图测试在独立盲测开始前完成；盲测计时期间只整理副本、Ruff 和 AST，未重跑 ORT。

开发集结果仍是选择阶段的 277→292/400，浅色首位 2→6/40。开发集用来选择，不能当作独立准确率提升。第三轮独立盲测中首位接触 exact 净增 4，但 through-line 退步，未满足预先固定的各噪声组不退条件；普通与外部兼容保持也不足以通过。仅收到独立评估的汇总结论，未读取逐图反馈、盲测原图或标签。

本路线至此停止：不据盲测调阈值、alpha 或 checkpoint，不再次运行来挑结果。`evidence/physical-export-v41/rejection.json` 是最终状态；历史 `export-and-freeze.json` 保留导出时的状态和数值证据，未回写改动。生产权重保留 B583。

`evidence/physical-export-v41/` 保存导出锁、候选/基底/权重 SHA、原 initializer 核验、400 图产品验证和工具副本 provenance。外部冻结源、模型和报告保持不变。

当前仓库的复现命令使用[冻结的恢复前 solver](HISTORICAL_SOLVER.md)，对照原始教师张量及分支；新输出单独记录实际快照身份。

```bash
OPENBLAS_NUM_THREADS=1 python training/captcha-research/export_physical_residual_v41.py \
  --root "$CAPTCHA_RESEARCH_DIR" --manifest "$CAPTCHA_RESEARCH_DIR/residual-v2/data/manifest.jsonl" \
  --cache "$CAPTCHA_RESEARCH_DIR/residual-v2/cache" --product "$PWD" \
  --selection "$CAPTCHA_RESEARCH_DIR/residual-v4/conditional-select-01/selection-and-freeze.json" \
  --physical "$CAPTCHA_RESEARCH_DIR/residual-v41/physical-diagnostic-01" \
  --protocol training/captcha-research/physical_export_protocol_v41.json \
  --directory "$CAPTCHA_RESEARCH_DIR/residual-v41/new-export"
python -m unittest discover -s training/captcha-research -p test_physical_onnx_v41.py
```

复现副本使用新的 cache contract，只接受在新目录重新抽取且核对映射/预处理协议的 cache。历史运行使用冻结原始研究代码和已验证 legacy cache；不能给历史 metadata 补字段，也不能用未来重跑的源码 SHA 冒充原执行记录。
