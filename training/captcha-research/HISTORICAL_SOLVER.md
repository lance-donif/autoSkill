# 历史实验的识别链路复现

历史物理诊断、专家评估和残差导出需要颜色恢复集成之前的预处理。当前产品 `captcha.solve` 会先恢复亮字并移除黑线；直接使用它复现历史实验，会改变冻结的物理条件和原始教师张量。

这些工具现统一通过 `historical_solver.py` 加载 `frozen_captcha.py`。快照逐字节来自提交 `336480a09753b43521b1f0fcd000014bc715e04d` 的 `skills/autoskill/app/autoskill/captcha.py`，SHA-256 为 `18b899e023f29f85da0ab07dd3d10e389f38b85e6fc8ebe9233ecae0f59db6ea`，与历史报告中的旧识别源码一致。装载时核对源码字节并直接编译，避免读取旧 pyc。

快照保留旧 loader、白底 alpha 合成、BGR 归一化、48 高缩放、右侧补零、CTC 解码及空结果错误。每次加载使用独立模块和模型会话；仅为旧源码的 `paths.SKILL` 提供目录值，不导入或修改当前产品 `autoskill.captcha`。原有 `--product "$PWD"` 命令可以继续在当前仓库执行；该参数提供默认模型目录，各实验仍按原协议选择和校验模型、metadata、数据与缓存。

适用入口：`diagnose_physical_line.py`、`diagnose_physical_line_v41.py`、`evaluate_color_layers.py`、`evaluate_png_specialist.py`、`evaluate_conditional_expert.py`、`export_residual.py`、`export_physical_residual_v41.py`。新生成的记录包含 `historical_solver`，明确注明快照提交、源码 SHA、装载 helper SHA 和未导入当前产品 solver；原有 `product_solver_sha256` 字段记录实际执行的历史快照。已有冻结结果和报告不回写。

这项修复没有更改生产模型或生产颜色恢复，也没有重跑训练、盲测或性能。验证使用合成三灰层图片与 mock session，覆盖历史黑线条件、各评估路由、诊断结果、空输出及模块状态隔离。现有完整链路性能工具仍比较各自明确指定的真实源码，不使用此历史适配器。

```bash
python -m unittest discover -s training/captcha-research -p 'test_historical_solver.py'
python -m unittest discover -s training/captcha-research -p 'test_*.py'
```
