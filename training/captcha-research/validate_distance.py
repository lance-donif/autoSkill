"""Compare a pinned algebraic model rewrite with the original inference outputs."""
import hashlib
import importlib.util
import json
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
import onnxruntime as ort

ROOT = Path(os.environ['CAPTCHA_RESEARCH_DIR'])
BASELINE = Path(os.environ['CAPTCHA_BASELINE_MODEL'])
CANDIDATE = Path(os.environ.get('CAPTCHA_CANDIDATE_MODEL', str(ROOT / 'candidates/rbf-distance-matmul.onnx')))
SCRIPT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('captcha_baseline_tools', SCRIPT / 'inspect_baseline.py')
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


def edit_distance(left, right):
    """Levenshtein distance for case-sensitive character error rate."""
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for other, target in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[other] + 1, previous[other - 1] + (char != target)))
        previous = current
    return previous[-1]


def evaluate(file, rows, charset):
    """Run each corpus image once without changing preprocessing or model weights."""
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = ort.InferenceSession(str(file), options, providers=['CPUExecutionProvider'])
    key = session.get_inputs()[0].name
    groups = defaultdict(lambda: {'exact': 0, 'samples': 0, 'edit_errors': 0, 'characters': 0})
    outputs, predictions = [], []
    for row in rows:
        tensor = tools.prepare((ROOT / row['file']).read_bytes(), 48, 320)
        output = session.run(None, {key: tensor})[0]
        prediction = tools.decode(output, charset)
        outputs.append(output)
        predictions.append(prediction)
        for group in (row['source'] + '/' + row['split'] + '/' + row['category'], row['source'] + '/' + row['split'] + '/ALL'):
            measured = groups[group]
            measured['exact'] += prediction == row['label']
            measured['samples'] += 1
            measured['edit_errors'] += edit_distance(prediction, row['label'])
            measured['characters'] += len(row['label'])
    for measured in groups.values():
        measured['exact_match_rate'] = measured['exact'] / measured['samples']
        measured['character_error_rate'] = measured['edit_errors'] / measured['characters']
    return outputs, predictions, dict(groups)


def main():
    """Write explicit numeric and decoded-output comparisons for a frozen pair."""
    expected = 'be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4'
    baseline_sha = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    if baseline_sha != expected:
        raise ValueError('The comparison baseline is not the pinned original model')
    charset = json.loads((tools.SOURCE / 'captcha-ctc.json').read_text())['charset']
    paths = [ROOT / 'datasets/manifest.jsonl', ROOT / 'datasets/public-apache-manifest.jsonl']
    rows = [json.loads(line) for file in paths if file.is_file() for line in file.read_text().splitlines()]
    pixels = [row['pixel_sha256'] for row in rows]
    if len(pixels) != len(set(pixels)):
        raise ValueError('Duplicate decoded pixels must be removed before evaluation')
    original_outputs, original_predictions, original_metrics = evaluate(BASELINE, rows, charset)
    candidate_outputs, candidate_predictions, candidate_metrics = evaluate(CANDIDATE, rows, charset)
    numeric = {'samples': len(rows),
               'max_absolute_probability_error': max(float(np.abs(left - right).max()) for left, right in zip(original_outputs, candidate_outputs)),
               'frame_argmax_changes': sum(int(np.count_nonzero(left.argmax(-1) != right.argmax(-1))) for left, right in zip(original_outputs, candidate_outputs)),
               'decoded_sequence_changes': sum(left != right for left, right in zip(original_predictions, candidate_predictions))}
    report = {'source_sha256': baseline_sha, 'candidate_sha256': hashlib.sha256(CANDIDATE.read_bytes()).hexdigest(),
              'numeric_equivalence': numeric, 'original': original_metrics, 'candidate': candidate_metrics,
              'limitations': 'Research corpus only; original training overlap unknown; no target-site accuracy or accuracy-improvement claim.'}
    (ROOT / 'distance-validation.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))  # noqa: T201  # the validation command writes its JSON result
    if numeric['decoded_sequence_changes'] or numeric['max_absolute_probability_error'] > 1e-5:
        raise ValueError('The frozen candidate exceeded the declared numeric/recognition equivalence gate')


if __name__ == '__main__':
    main()
