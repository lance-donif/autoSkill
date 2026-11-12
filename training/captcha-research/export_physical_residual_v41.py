"""Export and validate one frozen sparse-line-conditioned temporal CTC residual."""
import argparse
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import numpy as np
import onnx
import onnxruntime as ort
from diagnose_physical_line_v41 import physical_signal
from export_residual import build_model
from historical_solver import load_solver
from physical_onnx_v41 import attach_condition, condition_examples, condition_model
from residual_ctc import TemporalResidual, log_softmax
from scale_residual import residual_all
from train_residual_ctc import decode, prepare_inputs, sha


def export_model(root, weights, destination, *, zero_output=False):
    """Add the condition after the bounded adapter and verify all base weights."""
    temporary = destination.with_suffix('.unconditional.onnx')
    base_sha = build_model(root, weights, 1., temporary, zero_output=zero_output)
    model = attach_condition(onnx.load(temporary))
    base = onnx.load(root / 'baselines/speed-b58376af/captcha-ctc.onnx')
    tensors = {tensor.name: tensor.SerializeToString() for tensor in model.graph.initializer}
    if len(tensors) != len(model.graph.initializer):
        raise ValueError('The exported graph contains duplicate initializer names')
    if any(tensors.get(tensor.name) != tensor.SerializeToString() for tensor in base.graph.initializer):
        raise ValueError('An existing speed-model initializer changed')
    onnx.save(model, destination)
    temporary.unlink()
    return {'base_sha256': base_sha, 'unchanged_original_initializers': len(base.graph.initializer),
            'output_names': [item.name for item in model.graph.output]}


class ObservingSession:
    """Observe a real ORT session's historical inputs and probabilities."""

    def __init__(self, session, observe):
        self.session, self.observe = session, observe

    def get_inputs(self):
        return self.session.get_inputs()

    def run(self, outputs, inputs):
        result = self.session.run(outputs, inputs)
        self.observe(next(iter(inputs.values())), result[0][0])
        return result


def validate_boundaries(condition):
    """Check all semantic negatives and exact thresholds in one batched graph."""
    examples = condition_examples()
    tensors = np.concatenate([tensor for _, tensor, _ in examples])
    flags, masks = condition.run(None, {'image': tensors})
    for index, (name, tensor, expected) in enumerate(examples):
        if physical_signal(tensor)['flag'] != expected or bool(flags[index]) != expected:
            raise ValueError('Boundary flag mismatch: ' + name)
    if not np.array_equal(masks[:, 0, 0], flags.astype(np.float32)):
        raise ValueError('Condition bool and residual multiplier differ')
    return {'examples': len(examples), 'bool_changes': 0, 'cases': [name for name, _, _ in examples]}


def validate_product(product, candidate, root, rows, dev, expected, corrected, teacher, flags, zero_candidate):
    """Run the frozen historical solver and inspect every dev input tensor."""
    captcha = load_solver(product)
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
    options.log_severity_level = 3
    condition = ort.InferenceSession(condition_model().SerializeToString(), options, providers=['CPUExecutionProvider'])
    boundaries = validate_boundaries(condition)
    checked = set()
    for category in sorted({rows[i]['category'] for i in dev}):
        indices = [j for j, i in enumerate(dev) if rows[i]['category'] == category]
        true = [j for j in indices if flags[j]]
        false = [j for j in indices if not flags[j]]
        checked.update((true[:1] + false[:1]) if true and false else indices[:2])
    numeric = {'samples': len(checked), 'candidate_probability_max_abs': 0., 'zero_output_probability_max_abs': 0., 'zero_output_sequence_changes': 0}
    captured, flag_changes, sequence_changes, counter = {}, [], [], [0]

    def observe(tensor, probability):
        index = counter[0]
        actual = bool(condition.run(None, {'image': tensor})[0][0])
        reference = physical_signal(tensor)['flag']
        if actual != reference or actual != bool(flags[index]):
            flag_changes.append(rows[dev[index]]['id'])
        if index in checked:
            expected_probability = np.exp(log_softmax(corrected[index].astype(np.float64))).astype(np.float32)
            numeric['candidate_probability_max_abs'] = max(numeric['candidate_probability_max_abs'], float(np.abs(probability - expected_probability).max()))
            captured[index] = tensor.copy()
        counter[0] += 1

    original_constructor = ort.InferenceSession

    def observed_constructor(*args, **kwargs):
        return ObservingSession(original_constructor(*args, **kwargs), observe)

    def capped_options():
        return options

    folder = candidate.parent / 'product-solve-validation'
    folder.mkdir()
    shutil.copyfile(candidate, folder / 'captcha-ctc.onnx')
    shutil.copyfile(root / 'baselines/original-be9dd198/captcha-ctc.json', folder / 'captcha-ctc.json')
    empty_errors = 0
    with (patch.object(captcha, 'FOLDER', folder), patch.object(captcha, '_loaded', None),
          patch.object(ort, 'SessionOptions', capped_options), patch.object(ort, 'InferenceSession', observed_constructor)):
        for index, row_index in enumerate(dev):
            try:
                observed = captcha.solve((root / rows[row_index]['file']).read_bytes())
            except ValueError as error:
                if expected[index] or '没有从验证码图片里识别出字符' not in str(error):
                    raise
                observed, empty_errors = '', empty_errors + 1
            if observed != expected[index]:
                sequence_changes.append(rows[row_index]['id'])
    zero = original_constructor(str(zero_candidate), options, providers=['CPUExecutionProvider'])
    meta = json.loads((root / 'baselines/original-be9dd198/captcha-ctc.json').read_text())
    for index, tensor in captured.items():
        probability = zero.run(None, {zero.get_inputs()[0].name: tensor})[0][0]
        reference = np.exp(log_softmax(teacher[index].astype(np.float64))).astype(np.float32)
        numeric['zero_output_probability_max_abs'] = max(numeric['zero_output_probability_max_abs'], float(np.abs(probability - reference).max()))
        numeric['zero_output_sequence_changes'] += decode(probability[None], meta['charset'])[0] != decode(teacher[index][None], meta['charset'])[0]
    report = {'product_module': str(captcha.__file__), 'product_solver_sha256': sha(Path(captcha.__file__)),
              'historical_solver': captcha.research_provenance,
              'dev_images': len(dev), 'observed_product_tensors': counter[0], 'condition_bool_changes': len(flag_changes),
              'condition_changed_ids': flag_changes, 'product_solve_sequence_changes': len(sequence_changes), 'changed_dev_ids': sequence_changes,
              'empty_output_contract_errors': empty_errors, 'boundaries': boundaries, 'numeric': numeric,
              'session_threads': {'intra_op': 2, 'inter_op': 1}, 'production_model_files_written': False}
    if (counter[0] != len(dev) or flag_changes or sequence_changes or numeric['zero_output_sequence_changes']
            or max(numeric['candidate_probability_max_abs'], numeric['zero_output_probability_max_abs']) > 1e-5):
        raise ValueError('Conditional export parity failed: ' + json.dumps(report))
    return report


def main():
    """Export only the frozen b9 checkpoint under the unchanged v4.1 condition."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['root', 'manifest', 'cache', 'product', 'selection', 'physical', 'protocol', 'directory']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.directory.exists():
        raise ValueError('Use a new export directory; never overwrite a frozen candidate')
    protocol = json.loads(args.protocol.read_text())
    frozen = json.loads(args.selection.read_text())
    selected = frozen['selected']
    if (frozen['status'] != 'DEV_SELECTED_PENDING_EXPORT_AUTHORIZATION' or not selected
            or selected['weights_sha256'] != protocol['weights_sha256'] or selected['global_scale'] != 1
            or selected['checkpoint']['trial'] != 2 or selected['checkpoint']['epoch'] != 13):
        raise ValueError('The one authorized conditional checkpoint differs')
    inputs = {args.selection: protocol['selection_sha256'], args.manifest: protocol['manifest_sha256'],
              args.physical / 'observations.jsonl': protocol['physical_observations_sha256'],
              args.physical / 'protocol.json': protocol['physical_protocol_sha256']}
    if any(sha(file) != expected for file, expected in inputs.items()):
        raise ValueError('The frozen export selection, physical condition or dataset changed')
    weights = np.load(selected['weights'])
    if sha(Path(selected['weights'])) != protocol['weights_sha256']:
        raise ValueError('The selected weights changed')
    source_protocol = json.loads(Path(__file__).with_name('residual_protocol.json').read_text())
    rows, _, _, dev, features, teacher, mean, scale, charset = prepare_inputs(args.root, args.manifest, args.cache, source_protocol)
    if not np.array_equal(weights['feature_mean'], mean) or not np.array_equal(weights['feature_scale'], scale):
        raise ValueError('Selected feature normalization changed')
    observations = [json.loads(line) for line in (args.physical / 'observations.jsonl').read_text().splitlines()]
    if [row['id'] for row in observations] != [row['id'] for row in rows]:
        raise ValueError('The physical flags and frozen teacher ordering differ')
    flags = np.asarray([observations[i]['flag'] for i in dev], bool)
    head = TemporalResidual(weights['w1'], weights['b1'], weights['w2'], weights['b2'], int(weights['radius']), float(weights['cap']))
    corrected = teacher[dev] + flags.astype(np.float32)[:, None, None] * residual_all(head, features[dev], teacher[dev])
    expected = decode(corrected, charset)
    args.directory.mkdir(parents=True)
    lock = {'protocol': protocol, 'code_sha256': sha(Path(__file__)),
            'dependencies_sha256': {name: sha(Path(__file__).with_name(name))
                                    for name in ['physical_onnx_v41.py', 'diagnose_physical_line_v41.py', 'export_residual.py', 'residual_ctc.py', 'train_residual_ctc.py']},
            'selection_sha256': sha(args.selection), 'retraining_runs': 0, 'reselection_runs': 0}
    (args.directory / 'export-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    candidate, zero = args.directory / 'ctc-physical-residual.onnx', args.directory / 'zero-residual-control.onnx'
    base = export_model(args.root, weights, candidate)
    export_model(args.root, weights, zero, zero_output=True)
    parity = validate_product(args.product, candidate, args.root, rows, dev, expected, corrected, teacher[dev], flags, zero)
    report = {'status': 'FROZEN_EXPORT_PARITY_PASSED_PENDING_INDEPENDENT_VALIDATION',
              'candidate': str(candidate), 'candidate_sha256': sha(candidate), 'weights_sha256': protocol['weights_sha256'],
              'teacher_sha256': source_protocol['teacher_model_sha256'], 'export_base_sha256': base['base_sha256'],
              'original_initializers_unchanged': base['unchanged_original_initializers'], 'output_names': base['output_names'],
              'global_alpha': 1., 'condition_version': 'physical-sparse-through-line-v4.1', 'selected_dev': selected['dev'],
              'parity': parity, 'test_images_loaded': 0, 'external_images_loaded': 0, 'production_model_replaced': False}
    (args.directory / 'export-and-freeze.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))  # noqa: T201  # frozen export and parity evidence


if __name__ == '__main__':
    main()
