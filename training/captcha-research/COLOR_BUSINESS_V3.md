# 同一亮字算法：仅进入新的整串业务指标验证

`v3-business-candidate.json` 状态为 **ELIGIBLE_FOR_NEW_FULL_STRING_HOLDOUT**。这不是已经通过独立验证，也不是把旧结果改成通过。

v1 在逐组 CER 不退门槛下失败；v2 同样因深色首位 CER 45→47/200 被否决。对应 [v1 记录](COLOR_LAYER_V1.md)、[v2 记录](COLOR_BRIGHT_V2.md) 和全部 CER 报告保持不变。算法、128 分界、B583 权重及冻结研究源均没有改变。

新的评价目标依据验证码的实际结果：必须整串完全正确才成功。只读 paired dev 统计为新增正确 21、损失正确 1，净增 20/400；深色首位那一张恢复前后都整串失败，编辑错误从 1 变 3。普通预测变化 0，浅色首位 2→18/40；overall edits 161→131/2000。以上仍是参与开发迭代的 dev，不可当成独立泛化证据。

## 新前瞻标准

root 与独立评估端将在 fresh blind4 的数据生成/预测前正式固定协议，加入暗线负控。计划要求：所有 blind 类别整串 exact 不低于 baseline，整体 exact 严格提升；普通预测字符串变化 0；浅色首位至少净增 **6/240**；整体 CER 不高；逐类 CER 完整报告作诊断；每个 external 来源 exact 不低、整体 external CER 不高；无新增空输出或异常。

这是新协议，不能用来追认旧 CER 标准已通过。当前未读取任何新 blind 数据，未用旧 blind 逐图信息改算法。runtime 实现和空结果 fallback 由 root 独立冻结：研究函数本身未新增 fallback，后续若仅在恢复后为空时重试原 tensor，须在新 blind 前另行明确。

## 冻结函数与代表

冻结入口为 `/workspace/autoSkill-release/captcha-color-research/color_layer_bright_v2.py` 的 `recover_bright_layers(png)`：

- 函数文件 SHA：`1c4eac5345c5ff4f5c18a1be910404016edd0393cab956832e091264780483f8`。
- 依赖 `color_layer_core.py` SHA：`96b863bf3ba95e7775654a92c9e65db8e9ed1483724b4544c66b053b06cbb8fe`。
- 模型 SHA 保持 B583；没有新的训练权重。

八个预定小量代表位于 `evidence/color-business-v3/small-verification-plan.json`：按 v2 预注册的 route/category，取第一条 dev manifest 记录，不依赖 label 或预测。包含两种恢复、两种亮度弃权、两种原自校验弃权及两个未触发代表。这里只提供 ids/category/route，没有标签或预测。root 负责实际 runtime 与全 400 dev parity，本任务未并行修改 app。

## 衍生像素排除

独立新数据排除清单为 `evidence/color-business-v3/derived-restore-exclusion.jsonl`，SHA **`e01b72ce3f5e5aa480cfb5fd6593b93a6ed98bccb6d2905f5f50c617cbb30e15`**，**239 行 / 239 unique pixel hashes**，覆盖 v1 实际评估的全部恢复图（包含 v2 的 181 张子集）。仅由冻结算法对冻结原图重算像素，并核对原 threshold/mode/列覆盖记录；新增模型推理 0，未保存 PNG。

该清单继承 label、font family/文件 SHA、seed/variant seed 和父来源关系，供独立评估端排除重复。像素 SHA 使用旧 manifest 相同的 `sha256(str(size).encode()+decoded_RGB_bytes)`；另外记录编码 PNG SHA。它不含图像像素或识别预测。原清单和摘要已只读冻结在仓库外 `color-layers-v3-business/`，独立端只接收排除信息，不向开发端返回新 blind 字体、标签或样图。

`build_restore_exclusions.py` 可在新路径重建元信息，不执行 NN、不写 PNG。历史协议中的相对路径按外部冻结研究目录解释；仓库文档链接指向同一决定的归档副本。
