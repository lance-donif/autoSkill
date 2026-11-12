"""Evaluate one fixed palette-recovery chain on the original train/dev only."""
import argparse
import hashlib
import importlib.util
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import onnxruntime as ort
from color_layer_core import recover_layers
from historical_solver import load_solver


def sha(file):
    """Bind model, data and helper provenance before reading image pixels."""
    return hashlib.sha256(file.read_bytes()).hexdigest()


def load_gate(protocol):
    """Use the unchanged previously frozen physical gate implementation."""
    file = Path(protocol['gate_numpy_helper'])
    if sha(file) != protocol['gate_numpy_helper_sha256']:
        raise ValueError('The fixed v4.1 gate source changed')
    spec = importlib.util.spec_from_file_location('fixed_physical_gate', file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_data(protocol):
    """Accept exactly the known 1600 original train/dev records and teacher."""
    pairs = [('data_manifest', 'data_manifest_sha256'), ('teacher_cache', 'teacher_cache_sha256'),
             ('frozen_physical_observations', 'frozen_physical_observations_sha256'),
             ('base_model', 'base_sha256'), ('base_metadata', 'base_metadata_sha256')]
    if any(sha(Path(protocol[file])) != protocol[expected] for file, expected in pairs):
        raise ValueError('A fixed color-recovery input asset changed')
    rows = [json.loads(line) for line in Path(protocol['data_manifest']).read_text().splitlines()]
    if len(rows) != 1600 or any(row['source'] != 'synthetic-residual-v2' or row['split'] not in ['train', 'dev'] for row in rows):
        raise ValueError('Only the original fixed synthetic train/dev source is allowed')
    cache = np.load(protocol['teacher_cache'])
    physical = [json.loads(line) for line in Path(protocol['frozen_physical_observations']).read_text().splitlines()]
    identities = [row['id'] for row in rows]
    if list(cache['ids']) != identities or [row['id'] for row in physical] != identities:
        raise ValueError('Teacher, physical flags and images differ in ordering')
    return rows, cache['logits'], physical


class CaptureBaseline:
    """Capture frozen pre-recovery preprocessing while exposing teacher logits."""

    def __init__(self):
        self.tensor, self.frames = None, None

    @staticmethod
    def get_inputs():
        return [SimpleNamespace(name='image')]

    def run(self, outputs, inputs):
        if outputs is not None:
            raise ValueError('Unexpected product output request')
        self.tensor = inputs['image']
        return [self.frames]


def solve_text(solver, png):
    """Count the existing empty-output contract as an empty recognition result."""
    try:
        return solver(png)
    except ValueError as error:
        if '没有从验证码图片里识别出字符' not in str(error):
            raise
        return ''


def run_product(product, protocol, rows, teacher, physical, output):
    """Run B583 once per restored image through the frozen historical solver."""
    captcha = load_solver(product)
    gate = load_gate(protocol)
    meta = json.loads(Path(protocol['base_metadata']).read_text())
    capture, records = CaptureBaseline(), []
    original_options = ort.SessionOptions

    def options():
        chosen = original_options()
        chosen.intra_op_num_threads, chosen.inter_op_num_threads = 2, 1
        chosen.log_severity_level = 3
        return chosen

    with (patch.object(captcha, 'FOLDER', Path(protocol['base_model_folder'])), patch.object(captcha, '_loaded', None),
          patch.object(ort, 'SessionOptions', options), (output / 'predictions.jsonl').open('w', encoding='utf-8') as stream):
        actual = captcha._model()
        if actual[1:] != (meta['charset'], 48, 320):
            raise ValueError('The actual B583 loader metadata changed')
        for index, row in enumerate(rows):
            file = Path(protocol['data_root']) / row['file']
            if sha(file) != row['png_sha256']:
                raise ValueError('An original train/dev image changed')
            image = file.read_bytes()
            capture.frames = teacher[index][None]
            with patch.object(captcha, '_model', return_value=(capture, meta['charset'], 48, 320)):
                baseline = solve_text(captcha.solve, image)
            flag = gate.physical_signal(capture.tensor)['flag']
            if flag != physical[index]['flag']:
                raise ValueError('The actual PP48 input changed a fixed physical flag')
            prediction, details, solve_ms = baseline, {'reason': 'not_gated'}, None
            if flag:
                restored, details = recover_layers(image)
                if restored is not None:
                    start = time.perf_counter()
                    prediction = solve_text(captcha.solve, restored)
                    solve_ms = (time.perf_counter() - start) * 1000
            record = {'id': row['id'], 'split': row['split'], 'category': row['category'], 'normal': row['normal'],
                      'label': row['label'], 'baseline': baseline, 'candidate': prediction, 'flag': flag,
                      'restore': details['reason'] == 'restored', 'details': details, 'restored_product_solve_ms': solve_ms,
                      'new_empty': baseline != '' and prediction == ''}
            records.append(record)
            stream.write(json.dumps(record) + '\n')
            if index % 200 == 0:
                stream.flush()
                print(json.dumps({'phase': 'fixed-color-layers-once', 'completed': index, 'total': len(rows)}), flush=True)  # noqa: T201  # research progress
    return records, {'product_solver_sha256': sha(Path(captcha.__file__)), 'historical_solver': captcha.research_provenance,
                     'restored_recognizer_calls': sum(row['restore'] for row in records),
                     'base_model_sha256': protocol['base_sha256'], 'threads': {'intra': 2, 'inter': 1}, 'unexpected_exceptions': 0}


def edits(left, right):
    """Measure character errors independently of recognition implementation."""
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for column, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (char != other)))
        previous = current
    return previous[-1]


def summarize(records):
    """Expose every group's restoration/abstention and exact/CER tradeoffs."""
    groups = defaultdict(lambda: {'samples': 0, 'baseline_exact': 0, 'candidate_exact': 0, 'baseline_edits': 0, 'candidate_edits': 0,
                                  'characters': 0, 'prediction_changes': 0, 'new_empty': 0, 'restored': 0, 'abstained': 0, 'not_gated': 0})
    for row in records:
        for category in [row['category'], 'NORMAL' if row['normal'] else 'NOISY', 'ALL']:
            group = groups[row['split'] + '/' + category]
            group['samples'] += 1
            group['baseline_exact'] += row['baseline'] == row['label']
            group['candidate_exact'] += row['candidate'] == row['label']
            group['baseline_edits'] += edits(row['baseline'], row['label'])
            group['candidate_edits'] += edits(row['candidate'], row['label'])
            group['characters'] += len(row['label'])
            group['prediction_changes'] += row['baseline'] != row['candidate']
            group['new_empty'] += row['new_empty']
            group['restored'] += row['restore']
            group['abstained'] += row['flag'] and not row['restore']
            group['not_gated'] += not row['flag']
    for group in groups.values():
        group['baseline_CER'] = group['baseline_edits'] / group['characters']
        group['candidate_CER'] = group['candidate_edits'] / group['characters']
    return dict(groups)


def main():
    """Lock and evaluate the only fixed algorithm without a parameter grid."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['protocol', 'product', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new color-layer evaluation output')
    protocol = json.loads(args.protocol.read_text())
    rows, teacher, physical = load_data(protocol)
    args.output.mkdir(parents=True)
    lock = {'protocol_sha256': sha(args.protocol), 'script_sha256': sha(Path(__file__)),
            'core_sha256': sha(Path(__file__).with_name('color_layer_core.py')), 'algorithm_trials': 1,
            'training_runs': 0, 'test_images_loaded': 0, 'external_images_loaded': 0}
    (args.output / 'run-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    records, runtime = run_product(args.product, protocol, rows, teacher, physical, args.output)
    measured = summarize(records)
    noise = [measured['dev/' + name] for name in protocol['evaluation']['line_noise_groups']]
    first = measured['dev/first-symbol-touching']
    new_empty = sum(row['new_empty'] for row in records)
    accepted = (measured['dev/NORMAL']['prediction_changes'] == 0 and new_empty == 0
                and all(group['candidate_exact'] >= group['baseline_exact'] and group['candidate_edits'] <= group['baseline_edits'] for group in noise)
                and first['candidate_exact'] - first['baseline_exact'] >= 2)
    active = [row for row in records if row['flag']]
    timing = {key: {'median': statistics.median(row['details'][key] for row in active),
                    'p95': float(np.percentile([row['details'][key] for row in active], 95))}
              for key in ['total_pixel_ms', 'total_pixel_cpu_ms', 'histogram_otsu_ms']}
    report = {'status': 'FIXED_DEV_GATE_PASSED_PENDING_ROOT_DECISION' if accepted else 'REJECTED_FIXED_COLOR_LAYER_GATE',
              'groups': measured, 'abstain_reasons': dict(Counter(row['details']['reason'] for row in active if not row['restore'])),
              'restore_count': sum(row['restore'] for row in records), 'abstain_count': sum(not row['restore'] for row in active),
              'not_gated_count': len(records) - len(active), 'new_empty_outputs': new_empty, 'runtime': runtime,
              'pixel_timing_ms': timing, 'timing_scope': 'One fixed dataset pass; thread CPU excludes idle ORT worker spinning, not a controlled ABBA benchmark.',
              'learned_weights_modified': False, 'product_modified': False, 'training_runs': 0,
              'test_images_loaded': 0, 'external_images_loaded': 0, 'parameters_tuned': False}
    (args.output / 'evaluation-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))  # noqa: T201  # fixed research report


if __name__ == '__main__':
    main()
