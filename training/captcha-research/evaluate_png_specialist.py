"""Evaluate two predeclared decoders from one original-PNG expert pass."""
import argparse
import json
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import onnxruntime as ort
from build_conditional_expert import load_helper, sha
from evaluate_conditional_expert import load_data, metrics
from historical_solver import load_solver
from png_specialist_core import class_mappings, decode_both, original_png_tensor


class CaptureBaseline:
    """Capture frozen pre-recovery preprocessing while returning cached logits."""

    def __init__(self):
        self.tensor, self.frames = None, None

    @staticmethod
    def get_inputs():
        return [SimpleNamespace(name='image')]

    def run(self, outputs, inputs):
        if outputs is not None:
            raise ValueError('Unexpected product inference output request')
        self.tensor = inputs['image']
        return [self.frames]


def source_session(protocol):
    """Load exactly the fixed old source weights, without an If or conversion."""
    for field, key in [('source_model', 'source_sha256'), ('source_charset', 'source_charset_sha256'),
                       ('base_metadata', 'base_metadata_sha256'), ('gate_numpy_helper', 'gate_numpy_helper_sha256')]:
        if sha(Path(protocol[field])) != protocol[key]:
            raise ValueError('A fixed PNG-expert source asset changed: ' + field)
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
    options.log_severity_level = 3
    return ort.InferenceSession(protocol['source_model'], options, providers=['CPUExecutionProvider'])


def run(product, protocol, rows, teacher, physical, output):
    """Use frozen PP48 gate inputs and one original-PNG source run when true."""
    captcha = load_solver(product)
    meta = json.loads(Path(protocol['base_metadata']).read_text())
    charset = meta['charset']
    source_charset = json.loads(Path(protocol['source_charset']).read_text())
    lookup, indices = class_mappings(source_charset, charset)
    expert = source_session(protocol)
    gate = load_helper(Path(protocol['gate_numpy_helper']))
    capture = CaptureBaseline()
    records = []
    with (patch.object(captcha, '_model', return_value=(capture, charset, 48, 320)),
          (output / 'predictions.jsonl').open('w', encoding='utf-8') as stream):
        for index, row in enumerate(rows):
            file = Path(protocol['data_root']) / row['file']
            if sha(file) != row['png_sha256']:
                raise ValueError('A fixed train/dev PNG changed')
            image = file.read_bytes()
            capture.frames = teacher[index][None]
            try:
                baseline = captcha.solve(image)
            except ValueError as error:
                if '没有从验证码图片里识别出字符' not in str(error):
                    raise
                baseline = ''
            flag = gate.physical_signal(capture.tensor)['flag']
            if flag != physical[index]['flag']:
                raise ValueError('The actual PP48 gate changed a previously frozen flag')
            predictions, diagnostics, timing = {'A': baseline, 'B': baseline}, {}, {}
            if flag:
                start = time.perf_counter()
                tensor = original_png_tensor(image)
                timing['original_png_preprocess_ms'] = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                raw = expert.run(None, {expert.get_inputs()[0].name: tensor})[0]
                timing['expert_ORT_ms'] = (time.perf_counter() - start) * 1000
                predictions, diagnostics = decode_both(raw, lookup, indices, charset)
                diagnostics.update(expert_input_shape=list(tensor.shape), raw_output_shape=list(raw.shape))
            record = {'id': row['id'], 'split': row['split'], 'category': row['category'], 'normal': row['normal'],
                      'label': row['label'], 'baseline': baseline, 'A': predictions['A'], 'B': predictions['B'],
                      'flag': flag, **diagnostics, **timing}
            records.append(record)
            stream.write(json.dumps(record) + '\n')
            if index % 200 == 0:
                stream.flush()
                print(json.dumps({'phase': 'original-png-fixed-A-B-once', 'completed': index, 'total': len(rows)}), flush=True)  # noqa: T201  # fixed research progress
    return records, {'product_solver': str(captcha.__file__), 'product_solver_sha256': sha(Path(captcha.__file__)),
                     'historical_solver': captcha.research_provenance,
                     'baseline': 'Verified frozen B583 teacher text; historical preprocessing captured without a B583 network run.',
                     'source_ORT_calls': sum(row['flag'] for row in records), 'source_sha256': protocol['source_sha256'],
                     'threads': {'intra': 2, 'inter': 1}}


def judge(records, variant, protocol):
    """Keep all hard gates fixed while selecting only between A and B on dev."""
    measured = metrics([{**row, 'candidate': row[variant]} for row in records])
    groups = protocol['evaluation']['line_noise_groups']
    exact_gains = {name: measured['dev/' + name]['candidate_exact'] - measured['dev/' + name]['baseline_exact'] for name in groups}
    edit_gains = {name: measured['dev/' + name]['candidate_edits'] - measured['dev/' + name]['baseline_edits'] for name in groups}
    normal_changes = measured['dev/NORMAL']['prediction_changes']
    pale_changes = measured['dev/pale']['prediction_changes']
    first_gain = exact_gains['first-symbol-touching']
    eligible = normal_changes == 0 and pale_changes == 0 and min(exact_gains.values()) >= 0 and max(edit_gains.values()) <= 0 and first_gain >= 2
    noise = measured['dev/NOISY']
    rank = [noise['candidate_exact'], first_gain, -noise['candidate_edits'], int(variant == 'B')]
    return {'variant': variant, 'eligible': eligible, 'groups': measured, 'dev_line_exact_gains': exact_gains,
            'dev_line_edit_changes': edit_gains, 'dev_normal_prediction_changes': normal_changes,
            'dev_pale_prediction_changes': pale_changes, 'pale_first_symbol_exact_gain': first_gain, 'ranking': rank,
            'empty_outputs': sum(row[variant] == '' for row in records)}


def main():
    """Write one study lock before the fixed original-PNG evaluation starts."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['protocol', 'product', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new original-PNG evaluation output')
    protocol = json.loads(args.protocol.read_text())
    rows, teacher, physical = load_data(protocol)
    args.output.mkdir(parents=True)
    lock = {'protocol_sha256': sha(args.protocol), 'script_sha256': sha(Path(__file__)),
            'core_sha256': sha(Path(__file__).with_name('png_specialist_core.py')),
            'decoders': ['A', 'B'], 'source_runs_per_flagged_image': 1, 'normalization_or_resize_trials': 1,
            'test_images_loaded': 0, 'external_images_loaded': 0, 'training_runs': 0}
    (args.output / 'run-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    records, runtime = run(args.product, protocol, rows, teacher, physical, args.output)
    trials = [judge(records, variant, protocol) for variant in ['A', 'B']]
    eligible = [trial for trial in trials if trial['eligible']]
    selected = max(eligible, key=lambda trial: trial['ranking'])['variant'] if eligible else None
    timing = {key: statistics.median(row[key] for row in records if row['flag'])
              for key in ['original_png_preprocess_ms', 'expert_ORT_ms']}
    report = {'status': 'DEV_DECODER_SELECTED_PENDING_ROOT_DECISION' if selected else 'REJECTED_BOTH_FIXED_PNG_DECODERS',
              'selected_decoder': selected, 'trials': trials, 'runtime': runtime, 'observed_median_ms': timing,
              'timing_scope': 'One fixed pass, not a controlled benchmark.',
              'evaluated_images': len(records), 'physical_flag_changes': 0, 'source_model_modified': False,
              'projection_pruned': False, 'production_code_or_model_changed': False,
              'test_images_loaded': 0, 'external_images_loaded': 0, 'training_runs': 0, 'parameters_tuned': False}
    (args.output / 'evaluation-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))  # noqa: T201  # fixed research report


if __name__ == '__main__':
    main()
