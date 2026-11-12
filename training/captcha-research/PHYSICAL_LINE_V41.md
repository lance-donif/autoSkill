# v4.1 固定稀疏线条件修正

此前单用“近黑/近灰像素覆盖 ≥90% 有效列”会把全黑或深灰底的正常反色验证码算成线。这是公式本身可证明的语义缺口，不来自盲测。导出前另行预注册 v4.1：保留全部原阈值，再要求有效图像里同一近黑/近灰 mask 的像素占比 **≤0.50**。不改色调、decoder、权重或 scale；不据公开/盲测数据调参。

仍由真实产品 `captcha.solve` 捕获预处理 tensor，并只使用原固定 train/dev、缓存 be9 logits。两版 **1600 张逐图 flag 变化为 0**；普通 train 600 / dev 200 张触发仍为 0，普通 baseline-correct 554 / 178 张均无误触发。因此保留已按 v4 门槛选中的唯一 `b9d07631…` 权重，不重训、不重选。这个一致性只说明当前 train/dev 行为未变，不证明外部泛化。

有效图像左右边缘仍计入分母，padding 不计入像素面积；原始整数由 FP32 `rint(127.5*(tensor+1))` 还原。无有效列的全零非图像 tensor 返回 false 和零覆盖/密度，避免除零。正常真实像素不可能全通道恰好变成产品 padding 的零。

新增四项测试覆盖全黑/深灰底、黑底白字、密度恰好 50% 与超过 50%、padding、127/128、RGB 极差 16/17、全零 tensor。诊断和协议见 `evidence/physical-line-v41/` 及 `physical_line_protocol_v41.json`。没有创建 ORT session、应用残差、训练或读取任何 test/external 图片。ONNX 条件与完整候选的验证另行记录。

当前仓库复现时使用[冻结的恢复前 solver](HISTORICAL_SOLVER.md)，保留当时观察的原始 PP48 张量；新输出单独记录实际快照身份，原冻结结果不变。
