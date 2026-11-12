"""Select a full residual checkpoint under one fixed physical through-line signal."""
import argparse
import json
import shutil
from pathlib import Path

import numpy as np
from residual_ctc import TemporalResidual
from scale_residual import residual_all
from train_residual_ctc import decode, measure, prepare_inputs, sha


def assess(predictions, original, rows, baseline, policy):
    """Apply the separately predeclared conditional dev preservation requirements."""
    groups = measure(predictions, rows)
    normal_names = {row['category'] for row in rows if row['normal']}
    normal_regressed = [name for name in normal_names if groups[name]['exact'] < baseline[name]['exact'] or groups[name]['edits'] > baseline[name]['edits']]
    new_wrong = [row['id'] for prediction, before, row in zip(predictions, original, rows)
                 if row['normal'] and before == row['label'] and prediction != row['label']]
    gains = {name: groups[name]['exact'] - baseline[name]['exact'] for name in policy['line_noise_groups']}
    first_gain = gains['first-symbol-touching']
    # The no-line pale cohort has a zero mask and must also retain its baseline.
    pale_preserved = groups['pale']['exact'] == baseline['pale']['exact'] and groups['pale']['edits'] == baseline['pale']['edits']
    eligible = (not normal_regressed and not new_wrong and min(gains.values()) >= 0
                and first_gain >= policy['minimum_pale_first_symbol_exact_gain']
                and sum(gain > 0 for gain in gains.values()) >= policy['minimum_line_noise_groups_with_exact_gain']
                and pale_preserved)
    return {'eligible': eligible, 'groups': groups, 'normal_new_wrong_ids': new_wrong,
            'normal_new_wrong_count': len(new_wrong), 'normal_regressed_groups': sorted(normal_regressed),
            'line_noise_exact_gains': gains, 'pale_first_symbol_exact_gain': first_gain,
            'total_noise_exact_gain': groups['NOISY']['exact'] - baseline['NOISY']['exact'],
            'no_line_pale_preserved': pale_preserved}


def selection(root, manifest, cache, run, physical, protocol, output):
    """Evaluate exactly the old checkpoints, with alpha one and no new training."""
    if output.exists():
        raise ValueError('Use a new conditional-selection directory')
    if protocol['global_alpha'] != 1 or protocol['checkpoint_count'] != 160:
        raise ValueError('This selection has exactly 160 old checkpoints and alpha one')
    inputs = {manifest: protocol['manifest_sha256'],
              run / 'epoch-checkpoints.jsonl': protocol['source_epoch_checkpoints_sha256'],
              physical / 'observations.jsonl': protocol['physical_observations_sha256'],
              physical / 'report.json': protocol['physical_report_sha256'],
              physical / 'protocol.json': protocol['physical_protocol_sha256']}
    if any(sha(file) != digest for file, digest in inputs.items()):
        raise ValueError('The predeclared selection data or physical flags changed')
    checkpoints = [json.loads(line) for line in (run / 'epoch-checkpoints.jsonl').read_text().splitlines()]
    if len(checkpoints) != protocol['checkpoint_count']:
        raise ValueError('Unexpected number of old checkpoints')
    source_protocol = json.loads((run / 'run-lock.json').read_text())['protocol']
    rows, _, _, dev, features, teacher, mean, scale, charset = prepare_inputs(root, manifest, cache, source_protocol)
    observations = [json.loads(line) for line in (physical / 'observations.jsonl').read_text().splitlines()]
    if [row['id'] for row in observations] != [row['id'] for row in rows]:
        raise ValueError('Physical observations differ from the cache order')
    development = [rows[i] for i in dev]
    mask = np.asarray([observations[i]['flag'] for i in dev], np.float32)[:, None, None]
    xdev, ydev = features[dev], teacher[dev]
    original = decode(ydev, charset)
    baseline = measure(original, development)
    output.mkdir(parents=True)
    lock = {'protocol': protocol, 'selector_sha256': sha(Path(__file__)),
            'dependencies_sha256': {name: sha(Path(__file__).with_name(name)) for name in ['residual_ctc.py', 'train_residual_ctc.py', 'scale_residual.py']},
            'training_runs': 0, 'test_images_loaded': 0, 'external_images_loaded': 0}
    (output / 'run-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    best, trials = None, []
    for index, checkpoint in enumerate(checkpoints):
        file = Path(checkpoint['file'])
        if sha(file) != checkpoint['sha256']:
            raise ValueError('A source checkpoint changed after the original run')
        weights = np.load(file)
        if not np.array_equal(weights['feature_mean'], mean) or not np.array_equal(weights['feature_scale'], scale):
            raise ValueError('Checkpoint normalization differs from the frozen training data')
        head = TemporalResidual(weights['w1'], weights['b1'], weights['w2'], weights['b2'], int(weights['radius']), float(weights['cap']))
        bounded = residual_all(head, xdev, ydev)
        corrected = ydev + mask * bounded
        if not np.array_equal(corrected[mask[:, 0, 0] == 0], ydev[mask[:, 0, 0] == 0]):
            raise ValueError('The unflagged baseline path changed')
        judged = assess(decode(corrected, charset), original, development, baseline, protocol['selection'])
        parameters = sum(weights[name].size for name in ['w1', 'b1', 'w2', 'b2'])
        ranking = (judged['pale_first_symbol_exact_gain'], judged['total_noise_exact_gain'],
                   -judged['groups']['NOISY']['edits'], -parameters, -checkpoint['epoch'], -checkpoint['trial'])
        trial = {'trial': checkpoint['trial'], 'epoch': checkpoint['epoch'], 'eligible': judged['eligible'],
                 'pale_first_symbol_exact_gain': judged['pale_first_symbol_exact_gain'],
                 'total_noise_exact_gain': judged['total_noise_exact_gain'], 'noise_edits': judged['groups']['NOISY']['edits'],
                 'normal_new_wrong': judged['normal_new_wrong_count'], 'line_noise_exact_gains': judged['line_noise_exact_gains'],
                 'parameter_count': parameters, 'ranking': list(ranking)}
        trials.append(trial)
        if judged['eligible'] and (best is None or ranking > tuple(best['ranking'])):
            best = {'checkpoint': checkpoint, 'global_scale': 1., 'ranking': list(ranking), 'parameter_count': parameters, 'dev': judged}
        if index % 40 == 0:
            print(json.dumps({'checkpoints': index, 'total': len(checkpoints), 'eligible_found': best is not None}), flush=True)  # noqa: T201  # fixed selection progress
    selected = None
    if best:
        destination = output / 'selected-weights.npz'
        shutil.copyfile(best['checkpoint']['file'], destination)
        selected = {**best, 'weights': str(destination), 'weights_sha256': sha(destination)}
    report = {'status': 'DEV_SELECTED_PENDING_EXPORT_AUTHORIZATION' if selected else 'REJECTED_NO_ELIGIBLE_CONDITIONAL_CHECKPOINT',
              'selected': selected, 'baseline_dev': baseline, 'trials': trials,
              'evaluated_checkpoints': len(checkpoints), 'eligible_checkpoints': sum(item['eligible'] for item in trials),
              'global_alpha': 1., 'training_runs': 0, 'test_images_loaded': 0, 'external_images_loaded': 0,
              'unflagged_logits_unchanged': True, 'model_exported': False}
    (output / 'selection-and-freeze.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    """Use the new frozen selection protocol without modifying prior experiments."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['root', 'manifest', 'cache', 'run', 'physical', 'protocol', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    report = selection(args.root, args.manifest, args.cache, args.run, args.physical, protocol, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != 'trials'}, indent=2))  # noqa: T201  # final dev-only selection JSON


if __name__ == '__main__':
    main()
