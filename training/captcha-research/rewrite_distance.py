"""Equivalent RBF distance graph experiment; no source model is overwritten."""
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import onnx
from onnx import helper, numpy_helper

ROOT = Path(os.environ.get('CAPTCHA_RESEARCH_DIR', str(Path(__file__).resolve().parent)))
SOURCE = Path(os.environ['CAPTCHA_BASELINE_MODEL'])
EXPECTED_BASELINE_SHA256 = 'be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4'
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != EXPECTED_BASELINE_SHA256:
    raise ValueError('CAPTCHA_BASELINE_MODEL must be the pinned original model, not an already optimized candidate')
model = onnx.load(SOURCE)
onnx.checker.check_model(model)
initializers = {item.name: numpy_helper.to_array(item).copy() for item in model.graph.initializer}
generated = []
changes = []
for head in ('local', 'expanded'):
    centre_name = head + '/centres'
    centres = initializers[centre_name]
    transposed = head + '/centres_transposed'
    centre_norm = head + '/centre_squared_norm'
    generated += [numpy_helper.from_array(centres.T.copy(), transposed),
                  numpy_helper.from_array(np.sum(centres * centres, axis=-1, dtype=np.float32), centre_norm)]
    changes.append({'head': head, 'centres': list(centres.shape),
                    'original_intermediate_elements_per_image': 40 * int(np.prod(centres.shape)),
                    'new_distance_elements_per_image': 40 * len(centres)})
generated.append(numpy_helper.from_array(np.array(2, dtype=np.float32), 'distance_rewrite/two'))
replacement = []
for node in model.graph.node:
    head = next((prefix for prefix in ('local', 'expanded') if list(node.output) == [prefix + '/distance']), None)
    if head:
        prefix = head + '/distance_rewrite/'
        normalized = head + '/normalized'
        replacement.extend([
            helper.make_node('Mul', [normalized, normalized], [prefix + 'square'], name=prefix + 'square'),
            helper.make_node('ReduceSum', [prefix + 'square', head + '/last_axis'], [prefix + 'feature_norm'],
                             name=prefix + 'feature_norm', keepdims=1),
            helper.make_node('MatMul', [normalized, head + '/centres_transposed'], [prefix + 'dot'], name=prefix + 'dot'),
            helper.make_node('Add', [prefix + 'feature_norm', head + '/centre_squared_norm'], [prefix + 'norms'], name=prefix + 'norms'),
            helper.make_node('Mul', [prefix + 'dot', 'distance_rewrite/two'], [prefix + 'twice_dot'], name=prefix + 'twice_dot'),
            helper.make_node('Sub', [prefix + 'norms', prefix + 'twice_dot'], [head + '/distance'], name=head + '/distance'),
        ])
    elif any(list(node.output) == [head + '/' + suffix]
             for head in ('local', 'expanded') for suffix in ('expanded', 'difference', 'distance_square')):
        continue
    else:
        replacement.append(node)
del model.graph.node[:]
model.graph.node.extend(replacement)
old_initializers = [item for item in model.graph.initializer if item.name not in ('local/centres', 'expanded/centres', 'local/centre_axis', 'expanded/centre_axis')]
del model.graph.initializer[:]
model.graph.initializer.extend([*old_initializers, *generated])
model.doc_string += '\nResearch-only equivalent squared-distance rewrite: ||x||^2+||c||^2-2*x*c.T. Learned coefficients/backbone/radii unchanged.'
onnx.checker.check_model(model)
folder = ROOT / 'candidates'
folder.mkdir(exist_ok=True)
target = folder / 'rbf-distance-matmul.onnx'
onnx.save(model, target)
report = {'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
          'candidate_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
          'candidate': str(target), 'file_bytes': target.stat().st_size,
          'learned_weights_changed': False,
          'note': ('Centre matrices are transposed without loss; only derived squared norms and graph algebra added. '
                   'Floating-point operation order changes require numerical/recognition validation.'),
          'heads': changes}
(ROOT / 'distance-rewrite.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))  # noqa: T201  # the experiment command writes its JSON result
