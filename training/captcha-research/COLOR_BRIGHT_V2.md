# 亮字限定三层恢复 v2（原 CER 门槛下仍否决）

状态 **REJECTED_COUNTERFACTUAL_BRIGHT_ONLY_V2**。这是使用 train/dev 的新开发迭代，不是独立验证。v1 的失败记录与冻结文件不变；本轮没有改产品或运行新的识别推理。

在重新统计前单独注册 `color_layer_bright_protocol_v2.json`：全部 v1 物理自校验通过之后，额外要求 foreground mode **a1≥128**，否则使用原 baseline。128 复用已有亮暗分界，没有搜索 127/129 等网格。恢复公式、模型、gate、背景 mode 和阈值求解完全不变。

用固定 v1 的逐图 mode、prediction 和 baseline cache 作确定性重算：只保留 a1≥128 的原恢复预测，其余回 baseline。新工具重放与原 counterfactual 的逐图预测字节和全部分组统计一致。三项无模型测试验证实际调色板 127 弃权/128 保留、原自校验先执行、保留分支 PNG 字节与 v1 相同。按进度要求先报告了缓存重算，随后补齐像素边界测试；规则在重算前已注册。

全 1600：**181 保留恢复 / 58 亮度跳过 / 200 原自校验弃权 / 1161 未触发**。

| dev 分组 | baseline exact | v2 exact | baseline edits | v2 edits | 保留恢复 / 亮度跳过 |
|---|---:|---:|---:|---:|---:|
| 普通（200） | 178 | 178 | 22 | 22 | 0 / 0 |
| pale（40） | 33 | 33 | 8 | 8 | 0 / 0 |
| through-line（40） | 29 | 33 | 12 | 7 | 18 / 11 |
| dark-through-line（40） | 26 | 26 | 19 | 19 | 0 / 4 |
| first-symbol-touching（40） | 2 | 18 | 55 | 28 | 23 / 0 |
| dark-first-symbol-touching（40） | 9 | 9 | 45 | 47 | 1 / 2 |
| 全部（400） | 277 | 297 | 161 | 131 | 42 / 17 |

dev 另有原自校验弃权 46、未触发 295；普通与 pale 字符串变化 0，全 1600 新增空输出 0。浅色首位净增 16，但深色首位 CER **45→47/200**，因此仍按预先固定的 CER 门槛拒绝，没有进行后续少量 NN 验证或 runtime 晋级。

## 只读 paired 统计

根据既有 cache、无新推理：

| dev 分组 | 原正确→新错误 | 原错误→新正确 |
|---|---:|---:|
| 普通 / pale | 0 | 0 |
| through-line | 1 | 5 |
| dark-through-line | 0 | 0 |
| first-symbol-touching | 0 | 16 |
| dark-first-symbol-touching | 0 | 0 |
| 全部 | 1 | 21 |

深色首位唯一保留恢复本来就整串错误，恢复后仍错误，编辑错误由 1 变 3；它不是新增整串失败。这是对指标的描述，**没有据此把历史 CER 失败改写成通过**。任何后续改变评价目标的协议必须前瞻注册，保留全部旧失败及 CER 报告，再等待新的独立验证。

`color_layer_bright_v2.py` 保存固定内容条件，`test_color_layer_bright_v2.py` 无模型依赖，`counterfactual_color_bright_v2.py` 复算固定 cache 并输出 paired 计数。`evidence/color-bright-v2/` 保存全部分组统计、paired 计数、重放证明与冻结 SHA，不含样本、标签或像素数据。原逐图 cache 只在仓库外只读研究目录。

```bash
python -m unittest discover -s training/captcha-research -p test_color_layer_bright_v2.py
python training/captcha-research/counterfactual_color_bright_v2.py \
  --protocol /absolute/new-research/protocol.json --output /absolute/new-research/counterfactual
```
