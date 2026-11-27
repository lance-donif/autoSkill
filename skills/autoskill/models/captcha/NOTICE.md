# CAPTCHA CTC model notice

`public/models/captcha-ctc.onnx` was trained by this project from the Apache-2.0
PP-OCRv6 tiny recognition checkpoint at PaddleOCR `v3.7.0`, commit
`b03f46425e8ff4442b268ce449e3eef758146cd4`.

Training data included project-generated samples plus these pinned public datasets:

- MathCaptcha10k v6, CC BY 4.0
- parsasam/CAPTCHA Dataset v1, CC0 1.0
- huthayfahodeb/Captcha Dataset v2, CC0 1.0
- daniilnxy/Math problem CAPTCHA images v1, Apache 2.0

Exact source URLs, archive hashes, limitations, sample provenance, and split groups were
recorded in the historical `training/ppocrv6-captcha/public-datasets.ts` manifest in repository git history. The model must not be presented as supporting CAPTCHA types outside
the documented project scope.


## autoSkill inference-graph optimization (2026-11-12)

The learned OCR weights and both RBF correction heads are unchanged. The squared
distance calculation uses `||x||^2 + ||c||^2 - 2*x*c.T` instead of materializing
large broadcast difference/square tensors. Centre matrices are transposed exactly;
additional squared-norm constants are derived from the same centres.

Original model SHA-256: `be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4`.
Optimized model SHA-256: `b58376af1642a97b0df1be84642170c1f9a88203b86b062c3fe3d272e1759662`.

On the fixed 1920-image research corpus, output probabilities and CTC strings
were identical. The corpus is not a target-site acceptance set; this rewrite
does not claim improved recognition accuracy. Reproduction and CPU latency/memory
evidence were recorded under `training/captcha-research` (archived in repository git history). Upstream licenses remain unchanged.
