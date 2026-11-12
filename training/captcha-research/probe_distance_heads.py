"""Probe floating-point RBF rewrite error where compact heads are active."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import helper, numpy_helper

FEATURE = 'recognition/p2o.pd_op.transpose.8.0'
HEADS = ('local', 'expanded')


def isolated_heads(model):
    """Keep only the ancestors of the two correction tensors."""
    outputs = [head + '/correction' for head in HEADS]
    required = set(outputs)
    selected = []
    for node in reversed(model.graph.node):
        if required.intersection(node.output) and FEATURE not in node.output:
            selected.append(node)
            required.update(node.input)
    tensors = [item for item in model.graph.initializer if item.name in required]
    graph = helper.make_graph(list(reversed(selected)), 'rbf-numeric-probe',
                              [helper.make_tensor_value_info(FEATURE, onnx.TensorProto.FLOAT, [1, 40, 120])],
                              [helper.make_tensor_value_info(name, onnx.TensorProto.FLOAT, [1, 40, 63]) for name in outputs],
                              tensors)
    probe = helper.make_model(graph, opset_imports=list(model.opset_import))
    probe.ir_version = model.ir_version
    onnx.checker.check_model(probe)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(probe.SerializeToString(), options, providers=['CPUExecutionProvider'])


def compare(original, candidate, vectors):
    """Compare active corrections in fixed-size frame batches."""
    stats = {head: {'max_abs_error': 0.0, 'squared_error_sum': 0.0,
                    'elements': 0, 'active_frames': 0} for head in HEADS}
    for start in range(0, len(vectors), 40):
        count = min(40, len(vectors) - start)
        features = np.pad(vectors[start:start + count], ((0, 40 - count), (0, 0)))[None]
        left, right = (session.run(None, {FEATURE: features}) for session in (original, candidate))
        for head, before_batch, after_batch in zip(HEADS, left, right, strict=True):
            before, after = before_batch[0, :count], after_batch[0, :count]
            delta = np.abs(before.astype(np.float64) - after)
            group = stats[head]
            group['max_abs_error'] = max(group['max_abs_error'], float(delta.max(initial=0)))
            group['squared_error_sum'] += float(np.square(delta).sum())
            group['elements'] += delta.size
            group['active_frames'] += int(np.count_nonzero(np.any(before != 0, axis=-1)))
    for group in stats.values():
        group['rms_error'] = (group.pop('squared_error_sum') / group['elements']) ** .5
    return stats


def main():
    root = Path(os.environ['CAPTCHA_RESEARCH_DIR'])
    source = Path(os.environ['CAPTCHA_BASELINE_MODEL'])
    target = Path(os.environ['CAPTCHA_CANDIDATE_MODEL'])
    baseline_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    if baseline_sha != 'be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4':
        raise ValueError('The numeric probe requires the pinned original baseline')
    original, candidate = onnx.load(source), onnx.load(target)
    tensors = {item.name: numpy_helper.to_array(item) for item in original.graph.initializer}
    centres = np.concatenate([tensors[head + '/centres'] for head in HEADS])
    random = np.random.default_rng(2026111227)
    perturbation = random.normal(0, 1, centres.shape).astype(np.float32)
    perturbation /= np.linalg.norm(perturbation, axis=-1, keepdims=True)
    # Include each learned centre and nearby active supports, plus norm extremes.
    groups = {'all_1244_centres': centres,
              'near_centres_0.05': centres + .05 * perturbation,
              'near_centres_0.15': centres + .15 * perturbation,
              'near_centres_0.3': centres + .3 * perturbation,
              'zero_and_norm_extremes': np.concatenate([np.zeros((1, 120), np.float32),
                                                       centres[:44] * .01, centres[:44] * 100])}
    sessions = isolated_heads(original), isolated_heads(candidate)
    report = {'baseline_sha256': baseline_sha, 'candidate_sha256': candidate_sha,
              'kind': 'internal-feature numeric diagnostic, not image recognition accuracy',
              'vectors': sum(len(value) for value in groups.values()),
              'groups': {name: compare(*sessions, value) for name, value in groups.items()},
              'note': 'The algebra changes FP32 operation order; do not claim bitwise equality for arbitrary features.'}
    (root / 'distance-active-head-probe.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))  # noqa: T201  # The research command emits its diagnostic JSON.


if __name__ == '__main__':
    main()
