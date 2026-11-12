"""Select one global residual scale on train/dev checkpoints, with unchanged gates."""
import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
from residual_ctc import TemporalResidual
from train_residual_ctc import compare, decode, measure, prepare_inputs, sha

SCALES = (0., .03125, .0625, .125, .25, .5, .75, 1.)


def residual_all(head, features, teacher):
    """Forward once per checkpoint, then reuse the bounded residual for every scale."""
    residuals = []
    for start in range(0, len(features), 64):
        _, cache = head.forward(features[start:start + 64], teacher[start:start + 64])
        residuals.append(cache['residual'])
    return np.concatenate(residuals)


def main():
    """Freeze the dev utility and inputs before evaluating the fixed scale grid."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new scale-selection directory')
    training_result = args.run / 'training-and-freeze.json'
    if not training_result.is_file():
        raise ValueError('Checkpoint source run must be finished before scale selection')
    args.output.mkdir(parents=True)
    source_lock = json.loads((args.run / 'run-lock.json').read_text())
    protocol = source_lock['protocol']
    checkpoints_file = args.run / 'epoch-checkpoints.jsonl'
    checkpoints = [json.loads(line) for line in checkpoints_file.read_text().splitlines()]
    lock = {'version': 'global-residual-scale-v3', 'scales': list(SCALES), 'source_run_lock_sha256': sha(args.run / 'run-lock.json'),
            'checkpoints_manifest_sha256': sha(checkpoints_file), 'manifest_sha256': sha(args.manifest),
            'selection_policy_unchanged': protocol['selection'],
            'utility_predeclared': ['total_exact_gain', 'first_symbol_exact_gain', 'negative_total_edits',
                                    'negative_global_scale', 'negative_trial', 'negative_epoch'],
            'code_sha256': sha(Path(__file__)), 'test_images_loaded': 0, 'external_images_loaded': 0,
            'note': 'This is additional dev tuning; the second independent blind set is never loaded or used to choose a model.'}
    (args.output / 'scale-lock.json').write_text(json.dumps(lock, indent=2))
    rows, _, _, dev, features, teacher, mean, scale, charset = prepare_inputs(args.root, args.manifest, args.cache, protocol)
    development = [rows[i] for i in dev]
    xdev, ydev = features[dev], teacher[dev]
    original = decode(ydev, charset)
    baseline = measure(original, development)
    best, trials = None, []
    started = time.monotonic()
    for index, checkpoint in enumerate(checkpoints):
        file = Path(checkpoint['file'])
        if sha(file) != checkpoint['sha256']:
            raise ValueError('Diagnostic checkpoint changed after recording')
        weights = np.load(file)
        if not np.array_equal(weights['feature_mean'], mean) or not np.array_equal(weights['feature_scale'], scale):
            raise ValueError('Checkpoint feature normalization differs')
        head = TemporalResidual(weights['w1'], weights['b1'], weights['w2'], weights['b2'],
                                int(weights['radius']), float(weights['cap']))
        residual = residual_all(head, xdev, ydev)
        for alpha in SCALES:
            predictions = original if alpha == 0 else decode(ydev + np.float32(alpha) * residual, charset)
            judged = compare(predictions, original, development, baseline, protocol['selection'])
            ranking = (judged['total_exact_gain'], judged['first_symbol_exact_gain'], -judged['groups']['ALL']['edits'],
                       -alpha, -checkpoint['trial'], -checkpoint['epoch'])
            trial = {'trial': checkpoint['trial'], 'epoch': checkpoint['epoch'], 'scale': alpha,
                     'dev_exact': judged['groups']['ALL']['exact'], 'normal_new_wrong': judged['normal_new_wrong_count'],
                     'first_symbol_gain': judged['first_symbol_exact_gain'], 'total_gain': judged['total_exact_gain'],
                     'noise_gains': judged['noise_exact_gains'], 'eligible': judged['eligible']}
            trials.append(trial)
            if judged['eligible'] and (best is None or ranking > tuple(best['ranking'])):
                best = {'checkpoint': checkpoint, 'global_scale': alpha, 'ranking': list(ranking), 'dev': judged}
        if index % 20 == 0:
            progress = {'checkpoints': index, 'total': len(checkpoints), 'eligible_found': best is not None,
                        'elapsed_seconds': time.monotonic() - started}
            print(json.dumps(progress), flush=True)  # noqa: T201  # research progress JSON
    selected = None
    if best:
        destination = args.output / 'selected-weights.npz'
        shutil.copyfile(best['checkpoint']['file'], destination)
        selected = {**best, 'weights': str(destination), 'weights_sha256': sha(destination)}
    report = {'status': 'FROZEN_FOR_INDEPENDENT_VALIDATION' if selected else 'REJECTED_KEEP_ZERO_RESIDUAL_BASELINE',
              'selected': selected, 'baseline_dev': baseline, 'evaluated_checkpoints': len(checkpoints),
              'scale_trials': len(trials), 'trials': trials, 'freeze_unix': time.time(),
              'test_images_loaded': 0, 'external_images_loaded': 0}
    (args.output / 'scale-training-and-freeze.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'status': report['status'], 'selected': selected}), flush=True)  # noqa: T201  # final research selection result


if __name__ == '__main__':
    main()
