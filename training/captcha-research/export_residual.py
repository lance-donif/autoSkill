"""Export one frozen global-scale residual, then validate NumPy and product solve."""
import argparse
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import numpy as np
import onnx
import onnxruntime as ort
from historical_solver import load_solver
from onnx import helper, numpy_helper
from residual_ctc import TemporalResidual, log_softmax
from residual_data import frozen_metadata, prepare
from scale_residual import residual_all
from train_residual_ctc import decode, prepare_inputs, sha

SCRIPT = Path(__file__).resolve().parent


def build_model(root, weights, alpha, destination, *, zero_output=False):
    """Append an adapter to the immutable speed graph, never rewriting its weights."""
    base = root / 'baselines/speed-b58376af/captcha-ctc.onnx'
    expected = 'b58376af1642a97b0df1be84642170c1f9a88203b86b062c3fe3d272e1759662'
    if sha(base) != expected:
        raise ValueError('The immutable speed export base changed')
    if destination.exists():
        raise ValueError('Refuse to overwrite an existing exported candidate')
    model = onnx.load(base)
    prefix = 'temporal_residual/'
    initializers, additions = [], []

    def constant(name, value, dtype=np.float32):
        full = prefix + name
        initializers.append(numpy_helper.from_array(np.asarray(value, dtype=dtype), full))
        return full

    def node(op, inputs, name, **attributes):
        full = prefix + name
        additions.append(helper.make_node(op, inputs, [full], name=full, **attributes))
        return full

    raw_features = 'recognition/p2o.pd_op.transpose.8.0'
    standardized = node('Div', [node('Sub', [raw_features, constant('feature_mean', weights['feature_mean'])], 'centered'),
                                constant('feature_scale', weights['feature_scale'])], 'standardized')
    radius, cap = int(weights['radius']), float(weights['cap'])
    padded = node('Pad', [standardized, constant('pads', [0, radius, 0, 0, radius, 0], np.int64)], 'padded', mode='edge')
    contexts = [node('Slice', [padded, constant(f'start_{offset}', [offset], np.int64),
                               constant(f'end_{offset}', [offset + 40], np.int64),
                               constant(f'axis_{offset}', [1], np.int64)], f'context_{offset}')
                for offset in range(2 * radius + 1)]
    context = node('Concat', contexts, 'context', axis=2)
    hidden = node('Tanh', [node('Add', [node('MatMul', [context, constant('w1', weights['w1'])], 'hidden_linear'),
                                      constant('b1', weights['b1'])], 'hidden_affine')], 'hidden')
    w2 = np.zeros_like(weights['w2']) if zero_output else weights['w2']
    b2 = np.zeros_like(weights['b2']) if zero_output else weights['b2']
    raw = node('Add', [node('MatMul', [hidden, constant('w2', w2)], 'output_linear'), constant('b2', b2)], 'output_affine')
    bounded = node('Mul', [node('Tanh', [node('Div', [raw, constant('cap', cap)], 'cap_input')], 'bounded'), prefix + 'cap'], 'residual')
    scaled = node('Mul', [bounded, constant('global_scale', alpha)], 'scaled')
    corrected = node('Add', ['expanded/logits', scaled], 'logits')
    original = list(model.graph.node)
    final_index = next(i for i, item in enumerate(original) if list(item.output) == ['recognition/trained_probabilities'])
    final = original.pop(final_index)
    if final.op_type != 'Softmax':
        raise ValueError('The base output contract is not the expected CTC probabilities')
    final.input[0] = corrected
    del model.graph.node[:]
    model.graph.node.extend([*original, *additions, final])
    model.graph.initializer.extend(initializers)
    model.doc_string += '\nResearch-only frozen temporal residual; true marginal CTC, train/dev only; all original OCR weights unchanged.'
    onnx.checker.check_model(model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, destination)
    return sha(base)


def validate(root, selection, candidate, zero_candidate, product, manifest, cache):
    """Require the frozen historical solve path to match all selected dev strings."""
    frozen_metadata(root)
    protocol = json.loads((SCRIPT / 'residual_protocol.json').read_text())
    rows, _, _, dev, features, teacher, mean, scale, charset = prepare_inputs(root, manifest, cache, protocol)
    weights = np.load(selection['weights'])
    if sha(Path(selection['weights'])) != selection['weights_sha256']:
        raise ValueError('Frozen selected weights changed')
    if not np.array_equal(weights['feature_mean'], mean) or not np.array_equal(weights['feature_scale'], scale):
        raise ValueError('Feature normalization differs from the selected checkpoint')
    head = TemporalResidual(weights['w1'], weights['b1'], weights['w2'], weights['b2'], int(weights['radius']), float(weights['cap']))
    alpha = np.float32(selection['global_scale'])
    corrected = teacher[dev] + alpha * residual_all(head, features[dev], teacher[dev])
    expected = decode(corrected, charset)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    checked = []
    for category in sorted({rows[i]['category'] for i in dev}):
        checked.extend([j for j, i in enumerate(dev) if rows[i]['category'] == category][:2])
    numeric = {'samples': len(checked), 'candidate_probability_max_abs': 0., 'zero_output_probability_max_abs': 0., 'zero_output_sequence_changes': 0}
    for path, zero in ((candidate, False), (zero_candidate, True)):
        session = ort.InferenceSession(str(path), options, providers=['CPUExecutionProvider'])
        key = session.get_inputs()[0].name
        for index in checked:
            data = prepare((root / rows[dev[index]]['file']).read_bytes(), 48, 320)
            observed = session.run(None, {key: data})[0][0]
            logits = teacher[dev[index]] if zero else corrected[index]
            probability = np.exp(log_softmax(logits.astype(np.float64))).astype(np.float32)
            name = 'zero_output_probability_max_abs' if zero else 'candidate_probability_max_abs'
            numeric[name] = max(numeric[name], float(np.abs(observed - probability).max()))
            if zero:
                numeric['zero_output_sequence_changes'] += decode(observed[None], charset)[0] != decode(logits[None], charset)[0]
        del session
    captcha = load_solver(product)
    folder = candidate.parent / 'product-solve-validation'
    folder.mkdir()
    shutil.copyfile(candidate, folder / 'captcha-ctc.onnx')
    shutil.copyfile(root / 'baselines/original-be9dd198/captcha-ctc.json', folder / 'captcha-ctc.json')
    captcha.FOLDER, captcha._loaded = folder, None
    changes, empty_errors = [], 0
    original_options = ort.SessionOptions

    def capped_options():
        chosen = original_options()
        chosen.intra_op_num_threads, chosen.inter_op_num_threads = 2, 1
        return chosen

    # The frozen product loader/preprocessing/decoder run unchanged; only the
    # validation process's thread budget is capped to the shared two CPUs.
    with patch.object(ort, 'SessionOptions', capped_options):
        for index, row_index in enumerate(dev):
            image = (root / rows[row_index]['file']).read_bytes()
            try:
                observed = captcha.solve(image)
            except ValueError as error:
                if expected[index] or '没有从验证码图片里识别出字符' not in str(error):
                    raise
                observed, empty_errors = '', empty_errors + 1
            if observed != expected[index]:
                changes.append(rows[row_index]['id'])
    report = {'numeric': numeric, 'product_module': str(captcha.__file__), 'historical_solver': captcha.research_provenance,
              'product_solve_dev_images': len(dev),
              'product_solve_sequence_changes': len(changes), 'changed_dev_ids': changes, 'empty_output_contract_errors': empty_errors,
              'test_images_loaded': 0, 'external_images_loaded': 0, 'production_model_files_written': False}
    report['validation_session_threads'] = {'intra_op': 2, 'inter_op': 1}
    if changes or numeric['zero_output_sequence_changes'] or max(numeric['candidate_probability_max_abs'], numeric['zero_output_probability_max_abs']) > 1e-5:
        raise ValueError('Export parity check failed: ' + json.dumps(report))
    return report


def main():
    """Export only the already frozen dev-selected checkpoint; perform no reselection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--product', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    if hasattr(os, 'sched_setaffinity'):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    frozen = json.loads(args.selection.read_text())
    if frozen['status'] != 'FROZEN_FOR_INDEPENDENT_VALIDATION' or not frozen['selected']:
        raise ValueError('There is no eligible frozen candidate to export')
    selected = frozen['selected']
    weights = np.load(selected['weights'])
    if sha(Path(selected['weights'])) != selected['weights_sha256']:
        raise ValueError('Selected weights do not match their frozen SHA')
    if args.directory.exists():
        raise ValueError('Use a new export directory; existing frozen candidates may not be overwritten')
    candidate, zero = args.directory / 'ctc-residual.onnx', args.directory / 'zero-residual-control.onnx'
    base_sha = build_model(args.root, weights, selected['global_scale'], candidate)
    build_model(args.root, weights, selected['global_scale'], zero, zero_output=True)
    checked = validate(args.root, selected, candidate, zero, args.product, args.manifest, args.cache)
    report = {'status': 'FROZEN_EXPORT_PARITY_PASSED_PENDING_INDEPENDENT_VALIDATION', 'candidate': str(candidate),
              'candidate_sha256': sha(candidate), 'export_base_sha256': base_sha,
              'teacher_sha256': 'be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4',
              'weights_sha256': selected['weights_sha256'], 'selection_sha256': sha(args.selection),
              'export_script_sha256': sha(Path(__file__)), 'global_scale': selected['global_scale'],
              'selected_dev': selected['dev'], 'parity': checked, 'promoted': False}
    (args.directory / 'export-and-freeze.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))  # noqa: T201  # final export evidence JSON


if __name__ == '__main__':
    main()
