"""Evaluate the single fixed specialist wrapper on original train/dev only."""
import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
import onnxruntime as ort
from build_conditional_expert import load_helper, sha
from historical_solver import load_solver


def decode(logits, charset):
    """Use the same greedy blank/repeat contract as captcha.solve."""
    predictions = []
    for sample in logits:
        text, previous = [], 0
        for index in sample.argmax(axis=1):
            if index and index != previous:
                text.append(charset[int(index)])
            previous = int(index)
        predictions.append(''.join(text))
    return predictions


def edits(left, right):
    """Count Levenshtein errors without another inference or training library."""
    previous = list(range(len(right) + 1))
    for row, char in enumerate(left, 1):
        current = [row]
        for column, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (char != other)))
        previous = current
    return previous[-1]


def metrics(records):
    """Keep split/category error rates and all ordinary prediction changes visible."""
    groups = defaultdict(lambda: {'samples': 0, 'baseline_exact': 0, 'candidate_exact': 0, 'baseline_edits': 0,
                                  'candidate_edits': 0, 'characters': 0, 'prediction_changes': 0, 'flags': 0})
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
            group['flags'] += row['flag']
    for group in groups.values():
        group['baseline_CER'] = group['baseline_edits'] / group['characters']
        group['candidate_CER'] = group['candidate_edits'] / group['characters']
    return dict(groups)


def load_data(protocol):
    """Reject every source/split outside the already fixed original train/dev."""
    manifest = Path(protocol['data_manifest'])
    if sha(manifest) != protocol['data_manifest_sha256'] or sha(Path(protocol['teacher_cache'])) != protocol['teacher_cache_sha256']:
        raise ValueError('The frozen train/dev data or teacher cache changed')
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    if len(rows) != 1600 or any(row['source'] != 'synthetic-residual-v2' or row['split'] not in ['train', 'dev'] for row in rows):
        raise ValueError('This evaluation only accepts the fixed original 1600 train/dev images')
    arrays = np.load(protocol['teacher_cache'])
    if list(arrays['ids']) != [row['id'] for row in rows]:
        raise ValueError('The teacher ordering changed')
    observations_file = Path(protocol['frozen_physical_observations'])
    if sha(observations_file) != protocol['frozen_physical_observations_sha256']:
        raise ValueError('The previously frozen physical flags changed')
    observations = [json.loads(line) for line in observations_file.read_text().splitlines()]
    if [row['id'] for row in observations] != [row['id'] for row in rows]:
        raise ValueError('The physical flag ordering changed')
    return rows, arrays['logits'], observations


class ObservedSession:
    """Observe historical solver inference without changing its input or decoder."""

    def __init__(self, session, observe):
        self.session, self.observe = session, observe

    def get_inputs(self):
        return self.session.get_inputs()

    def run(self, outputs, inputs):
        start = time.perf_counter()
        result = self.session.run(outputs, inputs)
        milliseconds = (time.perf_counter() - start) * 1000
        self.observe(next(iter(inputs.values())), result[0], milliseconds)
        return result


def validate_probability(probability, flag, teacher, numeric):
    """Check either exact expert onehot or unchanged base probabilities."""
    if probability.ndim != 3 or probability.shape[0] != 1 or probability.shape[2] != 63 or probability.dtype != np.float32:
        raise ValueError('The single C63 output contract changed')
    if flag:
        if not np.all((probability == 0) | (probability == 1)) or not np.all(probability.sum(axis=2) == 1):
            raise ValueError('Expert decoder output is not exact onehot63')
        numeric['expert_onehot_images'] += 1
        return
    logits = teacher.astype(np.float64)
    reference = np.exp(logits - logits.max(axis=1, keepdims=True))
    reference /= reference.sum(axis=1, keepdims=True)
    if probability.shape != (1, 40, 63):
        raise ValueError('The B583 output shape changed')
    error = float(np.abs(probability[0] - reference.astype(np.float32)).max())
    numeric['unflagged_probability_max_abs_error'] = max(numeric['unflagged_probability_max_abs_error'], error)
    numeric['unflagged_images'] += 1
    if error > 1e-5:
        raise ValueError('The unflagged B583 numeric contract changed')


def run_product(product, candidate, protocol, rows, teacher, physical, output):
    """Use the frozen solve path once per allowed image, with fixed options."""
    captcha = load_solver(product)
    gate = load_helper(Path(protocol['gate_numpy_helper']))
    charset = json.loads(Path(protocol['base_metadata']).read_text())['charset']
    baseline = decode(teacher, charset)
    original_constructor, original_options = ort.InferenceSession, ort.SessionOptions
    records, observed = [], []
    numeric = {'unflagged_images': 0, 'unflagged_probability_max_abs_error': 0., 'expert_onehot_images': 0}

    def observe(tensor, probability, milliseconds):
        index = len(observed)
        flag = gate.physical_signal(tensor)['flag']
        if flag != physical[index]['flag']:
            raise ValueError('A real product input changed the frozen physical gate')
        validate_probability(probability, flag, teacher[index], numeric)
        observed.append({'flag': flag, 'output_frames': probability.shape[1], 'ORT_inference_ms': milliseconds})

    def constructor(*args, **kwargs):
        return ObservedSession(original_constructor(*args, **kwargs), observe)

    def capped_options():
        options = original_options()
        options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
        options.log_severity_level = 3
        return options

    folder = output / 'product-model'
    folder.mkdir()
    shutil_copy(candidate, folder / 'captcha-ctc.onnx')
    shutil_copy(Path(protocol['base_metadata']), folder / 'captcha-ctc.json')
    with (patch.object(captcha, 'FOLDER', folder), patch.object(captcha, '_loaded', None),
          patch.object(ort, 'InferenceSession', constructor), patch.object(ort, 'SessionOptions', capped_options),
          (output / 'predictions.jsonl').open('w', encoding='utf-8') as stream):
        for index, row in enumerate(rows):
            file = Path(protocol['data_root']) / row['file']
            if sha(file) != row['png_sha256']:
                raise ValueError('An allowed image changed its bytes')
            empty = False
            try:
                prediction = captcha.solve(file.read_bytes())
            except ValueError as error:
                if '没有从验证码图片里识别出字符' not in str(error):
                    raise
                prediction, empty = '', True
            record = {'id': row['id'], 'split': row['split'], 'category': row['category'], 'normal': row['normal'],
                      'label': row['label'], 'baseline': baseline[index], 'candidate': prediction, 'empty_output': empty, **observed[index]}
            records.append(record)
            stream.write(json.dumps(record) + '\n')
            if index % 200 == 0:
                stream.flush()
                print(json.dumps({'phase': 'fixed-train-dev-once', 'completed': index, 'total': len(rows)}), flush=True)  # noqa: T201  # research progress
    return records, numeric, {'product_solver': str(captcha.__file__), 'product_solver_sha256': sha(Path(captcha.__file__)),
                              'historical_solver': captcha.research_provenance, 'threads': {'intra': 2, 'inter': 1}}


def shutil_copy(source, destination):
    """Keep all temporary model loading paths outside the product worktree."""
    import shutil

    shutil.copyfile(source, destination)


def main():
    """Apply the fixed dev acceptance gate; never select alternate settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['protocol', 'candidate', 'product', 'dummy', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new fixed-evaluation directory')
    dummy = json.loads((args.dummy / 'dummy-contract-report.json').read_text())
    if dummy['status'] != 'DUMMY_CONTRACTS_PASSED_BEFORE_TRAIN_DEV' or sha(args.candidate) != dummy['candidate_sha256']:
        raise ValueError('The candidate has not passed its dummy contracts')
    protocol = json.loads(args.protocol.read_text())
    rows, teacher, physical = load_data(protocol)
    args.output.mkdir(parents=True)
    lock = {'protocol_sha256': sha(args.protocol), 'candidate_sha256': sha(args.candidate), 'script_sha256': sha(Path(__file__)),
            'dummy_report_sha256': sha(args.dummy / 'dummy-contract-report.json'), 'settings_changed': False,
            'test_images_loaded': 0, 'external_images_loaded': 0, 'training_runs': 0}
    (args.output / 'run-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    records, numeric, runtime = run_product(args.product, args.candidate, protocol, rows, teacher, physical, args.output)
    measured = metrics(records)
    normal = measured['dev/NORMAL']
    noise_gains = {name: measured['dev/' + name]['candidate_exact'] - measured['dev/' + name]['baseline_exact']
                   for name in protocol['evaluation']['line_noise_groups']}
    first_gain = noise_gains['first-symbol-touching']
    accepted = (normal['prediction_changes'] == 0 and min(noise_gains.values()) >= 0 and first_gain >= 2
                and measured['dev/pale']['prediction_changes'] == 0)
    timing = {}
    for flag in [False, True]:
        values = [row['ORT_inference_ms'] for row in records if row['flag'] == flag]
        timing['expert' if flag else 'base'] = {'images': len(values), 'ORT_inference_median_ms': statistics.median(values),
                                              'scope': 'One fixed dataset pass; not a controlled benchmark.'}
    report = {'status': 'FIXED_DEV_GATE_PASSED_PENDING_ROOT_DECISION' if accepted else 'REJECTED_FIXED_WRAPPER_DEV_GATE',
              'candidate_sha256': sha(args.candidate), 'groups': measured, 'dev_line_noise_exact_gains': noise_gains,
              'dev_pale_first_symbol_gain': first_gain, 'dev_ordinary_prediction_changes': normal['prediction_changes'],
              'numeric': numeric, 'runtime': runtime, 'observed_branch_timing': timing,
              'empty_outputs': sum(row['empty_output'] for row in records), 'evaluated_images': len(records),
              'settings_changed': False, 'training_runs': 0, 'test_images_loaded': 0, 'external_images_loaded': 0, 'production_model_replaced': False}
    (args.output / 'evaluation-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))  # noqa: T201  # fixed evaluation result


if __name__ == '__main__':
    main()
