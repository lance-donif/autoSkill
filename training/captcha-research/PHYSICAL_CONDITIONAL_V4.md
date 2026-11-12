# v4 固定物理条件与全残差：开发集选择

本步骤使用先前独立提交的固定贯穿线信号，不再训练，不开新 scale 网格，不改变阈值。只评估原来四组训练的 **160 个** epoch checkpoint；全局 alpha 固定 **1**。在 `cap*tanh` 后将整段残差乘每图固定 flag，再加回原 logits；未触发图保留 baseline 通路。

选择前单独冻结 `physical_conditional_protocol_v4.json`：普通 baseline-correct **0 新错**且普通各类 exact/CER 不退；四类线噪声 exact 都不低于 baseline；浅色 `first-symbol-touching` 至少净增 2/40；至少两类线噪声 exact 净增。无线 pale 组另外检查原样保留。排序依次为浅色首位 exact 增量、全部噪声 exact 增量、更少噪声编辑错误、更少 MLP 参数、更早 epoch；最后 trial 编号只作确定性同分处理。

**35 个** checkpoint 满足上述门槛。唯一选中 **trial 2 / epoch 13 / 20415 个新 MLP 参数**，权重 SHA `b9d07631ac888c89dc8f9cef66050253d9141b458bde18d27bb4e85755fdffb1`。权重没有重新训练或改变；这是不同于已否决无条件 v3 的预注册选择，不使用任何公开/盲测图、标签或逐图预测。

| dev 分组 | baseline exact | 条件残差 exact |
|---|---:|---:|
| 普通（200 张） | 178 | 178 |
| pale（40 张） | 33 | 33 |
| through-line（40 张） | 29 | 29 |
| dark-through-line（40 张） | 26 | 30 |
| first-symbol-touching（40 张） | 2 | 6 |
| dark-first-symbol-touching（40 张） | 9 | 16 |
| 全部（400 张） | 277 | 292 |

普通 logits 逐元素原样保留、baseline-correct 新错 0；全部噪声 CER **139→117/1000**，总体 CER **161→139/2000**。浅色目标仍只识别 6/40，不能称为解决。开发集参与选择，这些结果不等于独立泛化提升，也未替换产品模型。

`test_physical_selection.py` 验证三种有实际意义的门槛：第一目标至少净增 2；普通总体 exact 相同但交换了一个新错仍拒绝；噪声总体改善但单组退化仍拒绝。`evidence/physical-conditional-v4/` 保存选择锁、全部 160 个 checkpoint 的摘要、选中记录及 SHA，不含权重文件或模型。

```bash
OPENBLAS_NUM_THREADS=1 python training/captcha-research/select_physical_residual.py \
  --root "$CAPTCHA_RESEARCH_DIR" --manifest "$CAPTCHA_RESEARCH_DIR/residual-v2/data/manifest.jsonl" \
  --cache "$CAPTCHA_RESEARCH_DIR/residual-v2/cache" --run "$CAPTCHA_RESEARCH_DIR/residual-v2/runs/replay" \
  --physical "$CAPTCHA_RESEARCH_DIR/residual-v4/new-physical-diagnostic" \
  --protocol training/captcha-research/physical_conditional_protocol_v4.json \
  --output "$CAPTCHA_RESEARCH_DIR/residual-v4/new-conditional-selection"
```

历史运行使用冻结外部研究代码与原 legacy cache。仓库工具后续增加 cache contract 校验，拒绝没有 contract 的旧缓存：复现时应在全新目录重新抽取，不能给历史 metadata 补字段。若新路径使 manifest/检查点记录的字节变化，应在运行前为这一复现记录相应 provenance；不能改历史已冻结协议来冒称同一次执行。

本步骤只报告 dev 选择。唯一候选的 ONNX 条件一致性、真实产品 solve 对照与新盲测属于后续独立任务，不能把尚未完成的导出或评估算作通过。
