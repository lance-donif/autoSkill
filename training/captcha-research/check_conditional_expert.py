"""Check shape, adapter math, greedy semantics and actual If branch execution."""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from build_conditional_expert import load_helper, mapping_nodes, sha
from PIL import Image


def session(file, *, profile=None):
    """Use the fixed shared two-CPU budget for dummy inference."""
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
    options.log_severity_level = 3
    if profile is not None:
        options.enable_profiling = True
        options.profile_file_prefix = str(profile)
    return ort.InferenceSession(file if isinstance(file, bytes) else str(file), options, providers=['CPUExecutionProvider'])


def product_tensor(rgb):
    """Scale already-height48 dummy byte images exactly like product input."""
    tensor = np.zeros((1, 3, 48, 320), np.float32)
    tensor[0, :, :, :rgb.shape[1]] = (rgb[:, :, ::-1].astype(np.float32) / 127.5 - 1).transpose(2, 0, 1)
    return tensor


def reference_adapter(rgb, *, fp32=True):
    """Independent Pillow integer-gray plus mathematical half-pixel bilinear."""
    gray = np.asarray(Image.fromarray(rgb).convert('L'), np.float32)
    width = max(1, gray.shape[1] * 64 // 48)
    precision = np.float32 if fp32 else np.float64
    ys = np.clip((np.arange(64, dtype=precision) + precision(.5)) / precision(64 / 48) - precision(.5), 0, 47)
    xs = np.clip((np.arange(width, dtype=precision) + precision(.5)) / precision(width / gray.shape[1]) - precision(.5), 0, gray.shape[1] - 1)
    y0, x0 = ys.astype(np.int64), xs.astype(np.int64)
    y1, x1 = np.minimum(y0 + 1, 47), np.minimum(x0 + 1, gray.shape[1] - 1)
    wy, wx = (ys - y0).astype(precision)[:, None], (xs - x0).astype(precision)[None]
    resized = ((1 - wy) * ((1 - wx) * gray[y0[:, None], x0[None]] + wx * gray[y0[:, None], x1[None]])
               + wy * ((1 - wx) * gray[y1[:, None], x0[None]] + wx * gray[y1[:, None], x1[None]]))
    normalized = (resized / 127.5 - 1).astype(np.float32)[None, None]
    return gray[None, None], normalized


def check_adaptation(adapter, wrapper, expert, source_charset, target_charset, gate):
    """Cover tiny/ordinary/full widths and verify true branch source equivalence."""
    generator = np.random.default_rng(2026111205)
    lookup = np.asarray([{char: i for i, char in enumerate(target_charset)}.get(char, 0) for char in source_charset], np.int64)
    cases = []
    maximum, ideal_maximum = 0., 0.
    for width in [1, 2, 7, 100, 147, 320]:
        rgb = generator.integers(0, 256, size=(48, width, 3), dtype=np.uint8)
        tensor = product_tensor(rgb)
        gray, normalized = reference_adapter(rgb)
        _, ideal = reference_adapter(rgb, fp32=False)
        observed, observed_gray, _ = adapter.run(None, {adapter.get_inputs()[0].name: tensor})
        if not np.array_equal(gray, observed_gray):
            raise ValueError('PIL fixed-point grayscale differs at width ' + str(width))
        error = float(np.abs(observed - normalized).max())
        maximum = max(maximum, error)
        ideal_maximum = max(ideal_maximum, float(np.abs(observed - ideal).max()))
        if observed.shape != normalized.shape or error > 1e-5:
            raise ValueError('Fixed bilinear adapter differs at width ' + str(width))
        rgb.fill(255)
        rgb[20] = 30
        tensor = product_tensor(rgb)
        if not gate.physical_signal(tensor)['flag']:
            raise ValueError('The full-width thin dummy line did not trigger')
        adapted = adapter.run(None, {adapter.get_inputs()[0].name: tensor})[0]
        raw = expert.run(None, {expert.get_inputs()[0].name: adapted})[0]
        predicted = lookup[raw.argmax(axis=2)].transpose(1, 0)
        expected = np.eye(63, dtype=np.float32)[predicted]
        actual = wrapper.run(None, {wrapper.get_inputs()[0].name: tensor})[0]
        if not np.array_equal(actual, expected):
            raise ValueError('If expert branch changed the source greedy result')
        cases.append({'valid_width': width, 'expert_input_shape': list(adapted.shape), 'source_output_shape': list(raw.shape), 'target_output_shape': list(actual.shape)})
    return {'width_cases': cases, 'integer_gray_exact': True, 'normalized_fp32_reference_max_abs_error': maximum,
            'normalized_float64_ideal_max_abs_error': ideal_maximum, 'source_greedy_onehot_exact': True,
            'reference_correction': 'The initial float64 oracle exceeded 1e-5 at width320; FP32 scales/coordinates match standard ORT Resize arithmetic. The model/settings were unchanged.'}


def check_unflagged(wrapper, base, gate):
    """Check white, dark-background and colored ordinary inputs stay on B583."""
    maximum = 0.
    for name in ['white', 'dark-background', 'colored-line']:
        rgb = np.full((48, 100, 3), 255 if name != 'dark-background' else 30, np.uint8)
        if name == 'colored-line':
            rgb[20] = (20, 100, 20)
        tensor = product_tensor(rgb)
        if gate.physical_signal(tensor)['flag']:
            raise ValueError('An ordinary dummy image triggered the gate')
        expected = base.run(None, {base.get_inputs()[0].name: tensor})[0]
        observed = wrapper.run(None, {wrapper.get_inputs()[0].name: tensor})[0]
        if observed.shape != (1, 40, 63):
            raise ValueError('Base branch shape changed')
        maximum = max(maximum, float(np.abs(observed - expected).max()))
    if maximum > 1e-5:
        raise ValueError('The base branch numeric contract changed')
    return {'cases': 3, 'base_probability_max_abs_error': maximum}


def branch_profile(candidate, output, flag):
    """Measure a real dummy run and report only actually executed child kernels."""
    rgb = np.full((48, 147, 3), 255, np.uint8)
    if flag:
        rgb[20] = 30
    tensor = product_tensor(rgb)
    start = time.perf_counter()
    current = session(candidate, profile=output / ('true' if flag else 'false'))
    load_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    observed = current.run(None, {current.get_inputs()[0].name: tensor})[0]
    run_ms = (time.perf_counter() - start) * 1000
    profile = Path(current.end_profiling())
    events = [row for row in json.loads(profile.read_text()) if row.get('cat') == 'Node' and row['name'].endswith('_kernel_time')]
    base = [row for row in events if row['name'].startswith('base/')]
    expert = [row for row in events if row['name'].startswith('expert/')]
    if (flag and (not expert or base)) or (not flag and (not base or expert)):
        raise ValueError('Profiler did not prove one-backbone-only If routing')
    return {'flag': flag, 'load_ms': load_ms, 'first_run_ms': run_ms, 'output_shape': list(observed.shape),
            'executed_base_kernels': len(base), 'executed_expert_kernels': len(expert),
            'base_kernel_ms_sum': sum(row['dur'] for row in base) / 1000,
            'expert_kernel_ms_sum': sum(row['dur'] for row in expert) / 1000,
            'profile': str(profile), 'timing_scope': 'Single dummy first run, not a warm or controlled benchmark.'}


def check_mapping(source_charset, target_charset):
    """Use source-logit ties and unsupported separators to prove adapter semantics."""
    from onnx import TensorProto, helper

    nodes, constants, lookup = mapping_nodes('raw', source_charset, target_charset)
    graph = helper.make_graph(nodes, 'mapping-contract', [helper.make_tensor_value_info('raw', TensorProto.FLOAT, [None, 1, 8210])],
                              [helper.make_tensor_value_info('expert_adapter/output', TensorProto.FLOAT, [1, None, 63])], constants)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])
    model.ir_version = 8
    current = session(model.SerializeToString())
    raw = np.zeros((5, 1, 8210), np.float32)
    a, b = [source_charset.index(char) for char in 'Aa']
    raw[1, 0, a] = raw[1, 0, b] = 1
    raw[2, 0, a], raw[3, 0, 1], raw[4, 0, a] = 1, 1, 1
    expected = lookup[raw.argmax(axis=2)].transpose(1, 0)
    observed = current.run(None, {'raw': raw})[0]
    if not np.array_equal(observed, np.eye(63, dtype=np.float32)[expected]):
        raise ValueError('Source first-index ties or unsupported classes changed')
    return {'argmax_tie_first_index': True, 'unsupported_to_blank': True, 'output_onehot_exact': True}


def main():
    """Complete dummy contracts before reading any accuracy evaluation images."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new dummy-contract output directory')
    args.output.mkdir(parents=True)
    protocol = json.loads(args.protocol.read_text())
    source_charset = json.loads(Path(protocol['source_charset']).read_text())
    target_charset = json.loads(Path(protocol['base_metadata']).read_text())['charset']
    gate = load_helper(Path(protocol['gate_numpy_helper']))
    candidate = args.build / 'conditional-expert.onnx'
    wrapper, expert = session(candidate), session(args.build / 'converted-source.onnx')
    adapter, base = session(args.build / 'adapter-only.onnx'), session(protocol['base_model'])
    report = {'status': 'DUMMY_CONTRACTS_PASSED_BEFORE_TRAIN_DEV', 'candidate_sha256': sha(candidate),
              'adapter': check_adaptation(adapter, wrapper, expert, source_charset, target_charset, gate),
              'ordinary': check_unflagged(wrapper, base, gate), 'mapping': check_mapping(source_charset, target_charset),
              'branch_execution': [branch_profile(candidate, args.output, flag) for flag in [False, True]],
              'accuracy_images_read': 0, 'checker_script_sha256': sha(Path(__file__))}
    (args.output / 'dummy-contract-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))  # noqa: T201  # research contract report


if __name__ == '__main__':
    main()
