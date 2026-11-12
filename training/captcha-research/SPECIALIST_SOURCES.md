# ddddocr 预训练 CAPTCHA 专家模型来源核查

核查日期：2026-11-12。结论是优先把 **13,606,051 B 的 `common_old.onnx`** 留作隔离原型候选。它是已经训练好的文本 CAPTCHA 识别模型，包含大小写字母和数字；本机 ONNX Runtime 1.19.2 CPU 能创建 session。**本研究没有执行任何模型输入，没有准确率或速度结论，也没有替换生产 B583 模型。**

## 固定资产与来源

完整机器可读来源、下载 URL、SHA256、第三方源文件 SHA 见 [sources/ddddocr/provenance.json](sources/ddddocr/provenance.json)。模型和 wheel 只保存在仓库外 `/workspace/autoSkill-release/captcha-specialist-research/`，没有纳入本提交。

| 资产 | 大小（B） | SHA256 |
| --- | ---: | --- |
| `ddddocr-1.4.11-py3-none-any.whl` | 75,865,550 | `e75d369c0d2132352b7d91d1ba968c02a7425d16b004a66616e87da1b5a0556c` |
| `ddddocr-1.5.6-py3-none-any.whl` | 75,868,010 | `f13865b00e42de5c2507c1889ba73c2bacd218a49d15b928c2a5c82667062ac5` |
| `ddddocr-1.6.1-py3-none-any.whl` | 75,983,593 | `c7c70f4ae2d0335440ae8b272eea48c9f6888ecef46785fe2311f0c97a133935` |
| `common_old.onnx`，1.5.6 与 1.6.1 字节相同 | 13,606,051 | `b8f2ad9cbc1f2e3922a6cb9459e30824e7e2467f3fb4fd61420640e34ea0bf68` |
| `common.onnx`，即 `beta=True`，1.5.6 与 1.6.1 字节相同 | 54,088,400 | `33b5cd351ee94e73a6bf8fa18c415ed8b819b3ffd342e267c30d8ad8334e34e8` |
| `common_det.onnx`，三个版本相同，目标检测而非文字识别 | 20,127,694 | `6faa8ea85a8c1a634e5050c4a138fca10f30194e0d7abbe9ade1fcd423af6ed6` |
| 提交的 [old 字符表](sources/ddddocr/charset-old.json) | 49,040 | `2c097552127cb5476189e653c0d2ccfd6b6c87425c57f81a01b5770207c33360` |
| 提交的 [MIT 原文](sources/ddddocr/LICENSE)，固定 Git 版本、LF | 1,091 | `8f9defe3bfe64316168989830d40793a5074f25a8b13f3bb8b093914a7c27f73` |

原始 old 模型路径为 `/workspace/autoSkill-release/captcha-specialist-research/wheel-1.6.1/ddddocr/common_old.onnx`。没有单独的 `beta.onnx` 文件；`beta=True` 选择 `common.onnx`，默认选择 `common_old.onnx`。

固定 Git 来源：

- ddddocr README/LICENSE：[`c40f56f95412e10bcb9bd0bd24411e92f896d238`](https://github.com/sml2h3/ddddocr/tree/c40f56f95412e10bcb9bd0bd24411e92f896d238)。这是核查时固定的主线提交，不能据此把旧 wheel 的 wrapper 当成当前主线代码。
- ONNX Runtime `v1.19.2`：[`ffceed9d44f2f3efb9dd69fa75fea51163c91d91`](https://github.com/microsoft/onnxruntime/tree/ffceed9d44f2f3efb9dd69fa75fea51163c91d91)。注解 tag 对象为 `f5a21c75d374a2e1330bcb74daf3e4a3ab9d9269`，前者是剥离后的代码 commit。
- 官方链接的训练工具 dddd_trainer README/LICENSE：[`5fd0d0b5bb83bf44a5692c9d253b7d928e05e673`](https://github.com/sml2h3/dddd_trainer/tree/5fd0d0b5bb83bf44a5692c9d253b7d928e05e673)。未获取该项目的数据集。

## 代码与权重许可的边界

三个固定 wheel 均把包声明为 MIT，并随包附完整 MIT 文本；三个 LICENSE 字节相同（CRLF，SHA256 `6517f9f8ea3088e78633d768cff8b3a6e1e977365d22a5854f8aadd52d6f2bb4`）。提交的 LF 文件与其文本相同，也等于上述固定 Git commit 的 LICENSE。MIT 明确允许使用、修改、发布、再分发、再许可和销售，要求保留版权与许可通知；原文的版权行就是 `Copyright © 2022 <copyright holders>`，没有擅自补写权利人。

**代码的 MIT 授权有直接证据。权重的再分发依据是发布者将 ONNX 作为该 MIT 项目的包内资产分发，没有找到单独的权重许可或排除条款。** 因此项目级许可支持保留 MIT 通知的再分发，但不能把这句话扩大为已经独立核实训练数据来源、所有权链，或获得逐个 ONNX SHA 的单独权重授权书。原模型没有嵌入额外 license metadata/docstring。

“未发现限制”的搜索边界为：三个 wheel 的完整成员清单、METADATA、附带 LICENSE、对应 PyPI README；固定 ddddocr Git README/LICENSE；模型 metadata/docstring；固定 dddd_trainer README/LICENSE。检索了 MIT/license/copyright、research/non-commercial、研究/商用/商业/许可/授权/禁止等词。没有发现“仅研究”“禁止商业使用”等模型限制。README 中的“商业级识别接口”属于赞助商广告，不是本项目许可。该结论不覆盖所有历史 issue、历史 commit、外链服务条款或不可见的训练数据协议。

dddd_trainer 是另一个项目，附带 **Apache-2.0**；该工具的许可不能反向替代 ddddocr 预训练权重的项目级 MIT 依据。没有在此复制完整训练工具或第三方 Python 源码。

## ONNX 结构、字符表与实际输出契约

完整 operator 计数、输入输出声明、IR/opset 和 initializer dtype 见 [静态结构 JSON](evidence/ddddocr-static-model-structure.json)。两份识别模型均为 IR 6，输入 `input1: FLOAT[1,1,64,image_width]`，动态宽度，完整 8210 类 CTC 字符表，而非原生 63 类模型。

| 模型 | 使用的算子域 | 结构要点 |
| --- | --- | --- |
| old，292 nodes | 标准 ONNX opset 12；实际使用 `com.microsoft::DynamicQuantizeLSTM` v1 | 21×ConvInteger、22×DynamicQuantizeLinear、1×MatMulInteger、1×量化双向 LSTM；标准 Add/Cast/Concat/ConstantOfShape/Div/Floor/Gather/Mul/Reshape/Shape/Sigmoid/Transpose/Unsqueeze |
| beta/common，102 nodes | 仅标准 ONNX opset 12 | 21×Conv、1×LSTM、1×Gemm；标准 Add/Concat/Constant/ConstantOfShape/Gather/Mul/Reshape/Shape/Sigmoid/Transpose/Unsqueeze |
| det，279 nodes | 标准 ONNX opset 11 | 输入 `FLOAT[1,3,416,416]`，输出 `FLOAT[1,3549,6]`；不作为文本 CAPTCHA 候选 |

old 的 opset import 还列有 nchwc、mlfeaturizers、training 等域；图中没有对应节点，不能把未使用的 import 误报为实际依赖。old 不是“纯标准算子”模型，beta 的标准算子结构可以作为需要避开 Microsoft contrib 时的较大候选，尚无本研究的质量比较。

old 与 beta 各有 **8210 个唯一字符串**，空字符串 blank 都在 index 0。两表分别完整覆盖 `0-9`、`a-z`、`A-Z`，62 个 ASCII 每个恰有一个 entry，无重复，也有大量其他字符。两模型的表顺序不同，不可互换。old 的所有 62 个索引已写入 [old 契约 JSON](evidence/ddddocr-common-old-contract.json)；beta 表的 canonical JSON SHA 为 `60534d1fcde1536d0914f5de56b5ae183dcfd437564bc23acb9620e27eb66112`。用 AST 读取字面量证明 1.4.11、1.5.6、1.6.1 的 old 表逐项相同、beta 表也逐项相同，未 import ddddocr。

1.5.6/1.6.1 的 `387` 输出是 **FLOAT raw logits `[T,N,8210]`**，time axis 0、batch axis 1、class axis 2，默认 N=1。末端没有 Softmax 或 LogSoftmax。这是图拓扑的静态推导，不是运行观测：

1. CNN 的 `[N,C,H,T]` 经 `Transpose[3,0,1,2]` 与 `Reshape[0,0,-1]` 得到 LSTM 输入 `[T,N,F]`。
2. 双向 LSTM 的 `[T,2,N,512]` 经 `Transpose[0,2,1,3]` 与 reshape 得到 `[T,N,1024]`。
3. 展平后通过 `[1024,8210]` head 和 `[8210]` bias，再按 `shape366[0],shape366[1],-1` 还原为 `[T,N,8210]`。

**两份模型的输出 shape 注解 `[1,seqlen]` 已过期，ORT 的 `get_outputs()` 也只会报告这个声明。** 清除输出注解后运行 ONNX 静态 shape inference 仍未得到可用 shape；不得把它当成运行时 shape 验证。

来源解释见 [版本比较 JSON](evidence/ddddocr-version-lineage.json)：1.4.11 的 old 为 13,606,198 B/SHA `b1d5e1344954355b10b40632062b8a9def06bcc33229fb667274662bf31dcf3f`，beta 为 54,088,521 B/SHA `90e432635bfb100ac6b856220a10b186053264cc307dd049d58e8ea2643cb6e9`。与新版相比，所有 initializer 的 dtype、shape、数组内容 SHA 均相同；新版图恰好删掉末端 `ArgMax(axis=2,keepdims=0)` 和 `Transpose[1,0]` 两节点，原 `[N,T] INT64` 注解因此不再适用于新的 raw logits。不是仅凭两个相近文件大小推测其关系。

贪心解码应先对完整 8210 类做 argmax，再执行 CTC 连续重复合并与 blank 过滤。若转换到 63 类接口，必须明确字符映射与非 ASCII 的处理策略；先裁掉其他 logits 再 argmax 会改变专家决策，不能声称等价。映射前后可能出现不同类别折叠等额外变化，属于转换验证范围。

## 官方预处理发生了版本漂移

| wrapper 来源 | resize / 灰度顺序 | 数值范围 |
| --- | --- | --- |
| 1.4.11 `ddddocr/__init__.py:2570,2587-2590` | 原 PIL 图像用 `ANTIALIAS`（Lanczos）缩到 `(int(W*(64/H)),64)`，然后 `.convert('L')` | float32、`/255` 后 `(x-0.5)/0.5`，最终 float32 `[-1,1]` |
| 1.5.6 `ddddocr/__init__.py:2613,2630-2642` | 同上；不能先灰度再 resize | 同上 `[-1,1]` |
| 1.6.1 `core/ocr_engine.py:174-210`，`preprocessing/image_processor.py` | 同样先 Lanczos resize 再灰度 | 仅 float32 `/255`，`[0,1]`，没有减 0.5/除 0.5 |

旧版与新版 wrapper 对应的模型 SHA 相同，不能据新版 wrapper 推断旧模型的训练归一化。本轮最小原型应固定 **1.5.6 配套 `[-1,1]`**；如研究 `[0,1]`，须另列实验条件，不能静默替换。1.6.1 的可选 `png_fix` 会在 RGBA 情况下先做背景处理，默认不启用；1.4.11/1.5.6 的内置模型分支没有应用 `png_fix`，其相关代码只在自定义彩色模型分支内，不能套用新版行为。

PIL Lanczos resize 后再做整数灰度，与 ONNX Resize 的 linear/cubic 或先算浮点灰度不保证一致；ONNX Resize 没有 Lanczos 模式。生产接口的 `3×48×320` 图像也不能恢复原始动态宽度 `1×64×W` 图像已经丢掉的信息。这些是适配误差来源，本研究没有用图像衡量它们。

## old 量化 dtype 与 ORT 1.19.2 CPU/ARM64 证据

[量化结构 JSON](evidence/ddddocr-quantized-structure.json) 和 [old 契约](evidence/ddddocr-common-old-contract.json) 记录了以下实际组合：

- 21 个 ConvInteger 均为 **UINT8 activation × UINT8 weights → INT32**，不是常见的 unsupported INT8×INT8 CPU ConvInteger 情形。
- 两份 LSTM 权重 `359_quantized`、`360_quantized` 都是 INT8 `[2,512,2048]`；zero point INT8 为 0。LSTM 收到 float 输入，内部 activation 动态量化为 UINT8，使用 INT8 权重。
- 最后 head 的 `135_quantized` 为 UINT8 `[1024,8210]`，bias 为 FLOAT `[8210]`。

以下引用均固定在 ORT commit `ffceed9d44f2f3efb9dd69fa75fea51163c91d91`，每个文件的原文 SHA 与 URL 位于 provenance JSON：

| 官方源码证据 | 直接说明 |
| --- | --- |
| [`docs/OperatorKernels.md:77,472`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/docs/OperatorKernels.md#L77) | CPUExecutionProvider 列明 ConvInteger uint8/uint8/int32 与 DynamicQuantizeLSTM float、int8/uint8 权重；不要混用后面的 DML 表 |
| [`core/providers/cpu/quantization/conv_integer.cc:25-34,111-168`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/onnxruntime/core/providers/cpu/quantization/conv_integer.cc#L25) | 实际 CPU 注册是上述 UINT8 组合，计算路径调用 MLAS GEMM，没有 x64 专用注册条件 |
| [`contrib_ops/cpu/quantization/dynamic_quantize_lstm.cc:40-82,235-246`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/onnxruntime/contrib_ops/cpu/quantization/dynamic_quantize_lstm.cc#L235) | CPU contrib v1 注册允许 int8/uint8 权重，预打包保留 signed 标志 |
| [`contrib_ops/cpu/cpu_contrib_kernels.cc:201-246,363`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/onnxruntime/contrib_ops/cpu/cpu_contrib_kernels.cc#L201) | 量化注册表包含 DynamicQuantizeLSTM，未套 x64 架构 guard；裁剪掉 contrib 的自定义 ORT 构建另当别论 |
| [`core/providers/cpu/rnn/rnn_helpers.cc:268-312`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/onnxruntime/core/providers/cpu/rnn/rnn_helpers.cc#L268) | LSTM activation 量化到 uint8，并把权重 signed 标志送入 MLAS |
| [`core/mlas/lib/qgemm.h:883-887`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/onnxruntime/core/mlas/lib/qgemm.h#L883) | ARM64 分支明确支持 U8S8、U8U8 分派 |
| [`core/mlas/lib/platform.cpp:497-501,528-546`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/onnxruntime/core/mlas/lib/platform.cpp#L497) | ARM64 默认设置两类 Neon kernel；检测指令集后再使用 dot/UDOT 等优化，无需先假定硬件有这些扩展 |
| [`cmake/onnxruntime_mlas.cmake:295,334-358`](https://github.com/microsoft/onnxruntime/blob/ffceed9d44f2f3efb9dd69fa75fea51163c91d91/cmake/onnxruntime_mlas.cmake#L334) | 识别 aarch64 并纳入 ARM64 量化 GEMM/Neon 汇编实现 |

PyPI 为 ORT 1.19.2 提供以下 CPython 3.12 CPU 包，URL 和大小也已固定到 provenance JSON。这里仅检查发行元数据，没有下载、安装或执行这些平台的 wheel。

| 平台 wheel | SHA256 |
| --- | --- |
| `onnxruntime-1.19.2-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl` | `d2d366fbcc205ce68a8a3bde2185fd15c604d9645888703785b61ef174265168` |
| `onnxruntime-1.19.2-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` | `477b93df4db467e9cbf34051662a4b27c18e131fa1836e05974eae0d6e4cf29b` |
| `onnxruntime-1.19.2-cp312-cp312-win_amd64.whl` | `190103273ea4507638ffc31d66a980594b237874b65379e273125150eb044857` |

结论是 **ARM64 有对应类型组合的直接源码和发行支持证据**，不是 ARM 实机通过。UOS 没有独立 ORT wheel，应按实际 CPU 架构、Python ABI、glibc（上述 manylinux 标签至少 2.27）与动态库环境判断；不能从“国产 Linux”名称推断可用。Windows x64 有 CPU 包，但本研究也未在 Windows 加载。

## 本机仅加载的验证范围

原始 [load-only 日志](evidence/ddddocr-ort-1.19.2-linux-x64-load-only.log) 与 [结构化结果](evidence/ddddocr-ort-1.19.2-linux-x64-load-only.json) 记录：Linux x86_64、现有 `onnxruntime==1.19.2`、显式 `CPUExecutionProvider`、intra/inter 线程各 1、关闭 graph optimization，old 与 beta 均创建 `InferenceSession` 成功。只调用 metadata/provider 查询，随后释放 session，**没有构造模型输入，也没有调用 `session.run()`**。

加载成功证明这套本机 runtime 能解析模型、解析所需 CPU kernel，并完成 session 初始化。它不证明所有动态形状的运行正确性、真实输出 shape、准确率、延迟或跨架构可用性。Windows、ARM64、UOS 均为 **未实测**。

## 复现最小检查

下载到仓库外的新目录，固定版本、禁用依赖下载，不安装 ddddocr：

```bash
python -m pip download --no-deps --only-binary=:all: --index-url https://pypi.org/simple --dest /tmp/ddddocr-source-audit ddddocr==1.5.6 ddddocr==1.6.1
python -m pip download --no-deps --only-binary=:all: --python-version 3.11 --index-url https://pypi.org/simple --dest /tmp/ddddocr-source-audit ddddocr==1.4.11
sha256sum /tmp/ddddocr-source-audit/*.whl
```

1.4.11 的 `Requires-Python` 不包含本机 3.12，所以仅对下载使用目标 3.11；没有创建或运行旧版环境。用标准库 `zipfile` 只读取明确的 `.onnx`、`.py`、METADATA、LICENSE 成员；不要整包提取图片。先对照 provenance 的 wheel SHA，再对成员计算 SHA。使用已有 `onnx==1.17.0` 可读取 protobuf、initializer 和节点，不调用 ORT，不 import ddddocr。

字符表可以用标准库 `ast.parse` 和 `ast.literal_eval` 读取：1.5.6 `self.__charset` 两个字面量起始行分别为 77（old）、1376（beta）；1.6.1 为 `models/charset_manager.py` 中 `_get_old_charset` / `_get_beta_charset` 的返回字面量。仓库内 `charset-old.json` 按 `json.dumps(entries, ensure_ascii=False, separators=(',', ':'))` 编码，没有追加换行，以保留上述固定字节 SHA。

仅加载可在既有 ORT 1.19.2 环境中按以下方式复核；它不会执行输入。这个步骤也不要扩大为模型评测：

```python
import onnxruntime as ort

assert ort.__version__ == '1.19.2'
options = ort.SessionOptions()
options.intra_op_num_threads = 1
options.inter_op_num_threads = 1
options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
session = ort.InferenceSession('/outside/repository/common_old.onnx', options,
                               providers=['CPUExecutionProvider'])
print(session.get_providers())
print([(v.name, v.type, v.shape) for v in session.get_inputs()])
print([(v.name, v.type, v.shape) for v in session.get_outputs()])
del session
```

下一步可验证事项是单独构造转换候选并保持原始资产、预处理和字符映射可追踪。转换是否足够精确、能否保留生产速度与独立盲测质量，需要另一阶段的授权实验；本研究没有读取转换 agent 的预测，也没有把来源兼容性当成模型晋级依据。
