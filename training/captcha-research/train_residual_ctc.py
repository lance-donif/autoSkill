"""Train bounded temporal CTC residuals from a frozen train/dev feature cache."""
import argparse
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from residual_ctc import Adam, TemporalResidual, ctc_loss_and_grad, sequence_edit_distance, teacher_kl_and_grad
from residual_data import frozen_metadata


def sha(file):
    """Content address each input/checkpoint before it is used."""
    return hashlib.sha256(file.read_bytes()).hexdigest()


def decode(logits, charset):
    """Greedy CTC decode for the same observable contract as the application."""
    outputs = []
    for frames in logits.argmax(axis=-1):
        previous, text = -1, []
        for value in frames:
            if value and value != previous:
                text.append(charset[int(value)])
            previous = value
        outputs.append(''.join(text))
    return outputs


def measure(predictions, rows):
    """Keep ordinary/noisy cohorts visible instead of only a synthetic mean."""
    groups = defaultdict(lambda: {'exact': 0, 'samples': 0, 'edits': 0, 'characters': 0})
    for prediction, row in zip(predictions, rows):
        for name in (row['category'], 'NORMAL' if row['normal'] else 'NOISY', 'ALL'):
            group = groups[name]
            group['exact'] += prediction == row['label']
            group['samples'] += 1
            group['edits'] += sequence_edit_distance(prediction, row['label'])
            group['characters'] += len(row['label'])
    for group in groups.values():
        group['exact_rate'] = group['exact'] / group['samples']
        group['CER'] = group['edits'] / group['characters']
    return dict(groups)


def compare(predictions, baseline_predictions, rows, baseline, policy):
    """Apply fixed dev preservation gates; never relax them to force a candidate."""
    measured = measure(predictions, rows)
    ordinary_names = {row['category'] for row in rows if row['normal']}
    noisy_names = {row['category'] for row in rows if not row['normal']}
    new_wrong = [row['id'] for prediction, original, row in zip(predictions, baseline_predictions, rows)
                 if row['normal'] and original == row['label'] and prediction != row['label']]
    normal_regressed = [name for name in ordinary_names if measured[name]['exact'] < baseline[name]['exact'] or measured[name]['edits'] > baseline[name]['edits']]
    gains = {name: measured[name]['exact'] - baseline[name]['exact'] for name in noisy_names}
    total_gain = measured['ALL']['exact'] - baseline['ALL']['exact']
    eligible = (not normal_regressed and len(new_wrong) <= policy['normal_baseline_correct_new_wrong_max']
                and sum(gain > 0 for gain in gains.values()) >= policy['minimum_noise_groups_with_exact_gain']
                and total_gain >= policy['minimum_total_exact_gain'])
    first_gain = sum(gain for name, gain in gains.items() if 'first-symbol-touching' in name)
    return {'eligible': eligible, 'groups': measured, 'normal_new_wrong_count': len(new_wrong),
            'normal_new_wrong_ids': new_wrong, 'normal_regressed_groups': sorted(normal_regressed),
            'noise_exact_gains': gains, 'total_exact_gain': total_gain, 'first_symbol_exact_gain': first_gain}


def balanced_batches(rows, random, batch_size):
    """Use half ordinary and half noisy data in each update."""
    ordinary = random.permutation([i for i, row in enumerate(rows) if row['normal']])
    noisy = random.permutation([i for i, row in enumerate(rows) if not row['normal']])
    half = batch_size // 2
    if not half or len(ordinary) != len(noisy):
        raise ValueError('The fixed training protocol requires equally sized normal/noisy cohorts')
    for start in range(0, len(ordinary), half):
        yield random.permutation(np.concatenate((ordinary[start:start + half], noisy[start:start + half])))


def forward_all(head, features, baseline, batch_size=64):
    """Avoid allocating a full-epoch context tensor at once."""
    values = [head.forward(features[start:start + batch_size], baseline[start:start + batch_size])[0]
              for start in range(0, len(features), batch_size)]
    return np.concatenate(values)


def save_checkpoint(file, head, mean, scale, selection):
    """An accepted checkpoint is immutable evidence, not a production replacement."""
    np.savez_compressed(file, **head.parameters(), feature_mean=mean, feature_scale=scale,
                        radius=np.asarray(head.radius), cap=np.asarray(head.cap))
    return {**selection, 'file': str(file), 'sha256': sha(file)}


def prepare_inputs(root, manifest, cache_dir, protocol):
    """Reject any weight/data mismatch or accidental held-out input."""
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    if any(row['source'] != protocol['allowed_source'] or row['split'] not in protocol['allowed_splits'] for row in rows):
        raise ValueError('Only the fixed new train/dev source may enter the trainer')
    cache = np.load(cache_dir / 'features.npz')
    metadata = json.loads((cache_dir / 'metadata.json').read_text())
    if metadata['model_sha256'] != protocol['teacher_model_sha256'] or metadata['manifest_sha256'] != sha(manifest):
        raise ValueError('Cache provenance differs from the fixed protocol')
    if metadata['cache_sha256'] != sha(cache_dir / 'features.npz') or list(cache['ids']) != [row['id'] for row in rows]:
        raise ValueError('Feature bytes or ordering changed')
    meta = frozen_metadata(root)
    charset = meta['charset']
    index = {char: i for i, char in enumerate(charset)}
    labels = [[index[char] for char in row['label']] for row in rows]
    train = np.asarray([i for i, row in enumerate(rows) if row['split'] == 'train'])
    dev = np.asarray([i for i, row in enumerate(rows) if row['split'] == 'dev'])
    raw = cache['features']
    mean = raw[train].mean(axis=(0, 1)).astype(np.float32)
    scale = np.maximum(protocol['feature_scale_floor'], raw[train].std(axis=(0, 1))).astype(np.float32)
    features = ((raw - mean) / scale).astype(np.float32)
    return rows, labels, train, dev, features, cache['logits'], mean, scale, charset


def train_trial(data, config, protocol, output, trial_index, baseline_predictions, baseline_metrics):
    """Fit one predeclared configuration with true marginal CTC supervision."""
    rows, labels, train, dev, features, teacher, mean, scale, charset = data
    training_rows, development_rows = [[rows[i] for i in indices] for indices in (train, dev)]
    xtrain, xdev = features[train], features[dev]
    ytrain, ydev = teacher[train], teacher[dev]
    train_labels, dev_labels = [[labels[i] for i in indices] for indices in (train, dev)]
    original_train = [baseline_predictions[i] for i in train]
    original_dev = [baseline_predictions[i] for i in dev]
    protected = np.asarray([row['normal'] and prediction == row['label'] for row, prediction in zip(training_rows, original_train)], np.float32)
    head = TemporalResidual.initialize(dimensions=features.shape[-1], hidden=config['hidden'], classes=len(charset),
                                       radius=config['radius'], cap=config['cap'], seed=protocol['seed'])
    identity = forward_all(head, xdev, ydev)
    if not np.array_equal(identity, ydev):
        raise ValueError('Zero-residual initialization did not preserve the baseline exactly')
    optimizer = Adam(head.parameters(), learning_rate=config['learning_rate'], clip_norm=protocol['gradient_clip_norm'])
    random = np.random.default_rng(protocol['seed'])
    best, history, minimum_loss, stale = None, [], np.inf, 0
    started = time.monotonic()
    for epoch in range(1, protocol['max_epochs'] + 1):
        losses = []
        for batch in balanced_batches(training_rows, random, protocol['batch_size']):
            corrected, cache = head.forward(xtrain[batch], ytrain[batch])
            loss, gradient, _ = ctc_loss_and_grad(corrected, [train_labels[i] for i in batch])
            consistency, consistency_gradient = teacher_kl_and_grad(corrected, ytrain[batch], protected[batch])
            derivative = gradient + config['teacher_kl'] * consistency_gradient
            gradients = head.backward(derivative, cache, residual_penalty=config['residual_penalty'])
            for name in ('w1', 'w2'):
                gradients[name] += protocol['weight_decay'] * head.parameters()[name]
            optimizer.step(gradients)
            losses.append(loss + config['teacher_kl'] * consistency + config['residual_penalty'] * float(np.mean(cache['residual'] ** 2)))
        corrected_dev = forward_all(head, xdev, ydev)
        predictions = decode(corrected_dev, charset)
        judged = compare(predictions, original_dev, development_rows, baseline_metrics, protocol['selection'])
        dev_loss = ctc_loss_and_grad(corrected_dev, dev_labels)[0]
        row = {'trial': trial_index, 'epoch': epoch, 'train_loss': float(np.mean(losses)), 'dev_CTC': dev_loss,
               'dev_exact': judged['groups']['ALL']['exact'], 'dev_total': len(dev),
               'normal_new_wrong': judged['normal_new_wrong_count'], 'first_symbol_gain': judged['first_symbol_exact_gain'],
               'total_gain': judged['total_exact_gain'], 'eligible': judged['eligible'], 'elapsed_seconds': time.monotonic() - started}
        history.append(row)
        print(json.dumps(row), flush=True)  # noqa: T201  # epoch JSON progress for the research log
        diagnostics = output / 'checkpoints'
        diagnostics.mkdir(exist_ok=True)
        diagnostic = save_checkpoint(diagnostics / f'trial-{trial_index}-epoch-{epoch}.npz', head, mean, scale,
                                     {'trial': trial_index, 'epoch': epoch, 'config': config,
                                      'diagnostic_only': True, 'metrics': row})
        with (output / 'epoch-checkpoints.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(diagnostic) + '\n')
        ranking = (judged['total_exact_gain'], judged['first_symbol_exact_gain'], -judged['groups']['ALL']['edits'])
        if judged['eligible'] and (best is None or ranking > tuple(best['ranking'])):
            file = output / f'trial-{trial_index}-epoch-{epoch}.npz'
            best = save_checkpoint(file, head, mean, scale, {'trial': trial_index, 'epoch': epoch, 'config': config,
                                                           'ranking': list(ranking), 'dev': judged})
        if dev_loss < minimum_loss - 1e-4:
            minimum_loss, stale = dev_loss, 0
        else:
            stale += 1
        if epoch >= protocol['minimum_epochs'] and stale >= protocol['early_stop_no_loss_improvement_epochs']:
            break
    final_train = measure(decode(forward_all(head, xtrain, ytrain), charset), training_rows)
    report = {'trial': trial_index, 'config': config, 'history': history, 'best_eligible': best,
              'final_train_groups': final_train, 'final_dev': judged, 'zero_residual_identity': True}
    (output / f'trial-{trial_index}.json').write_text(json.dumps(report, indent=2))
    return report


def main():
    """Create a new locked run directory; no implicit extraction or model promotion."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, default=Path(__file__).with_name('residual_protocol.json'))
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new run directory; existing research evidence may not be overwritten')
    args.output.mkdir(parents=True)
    protocol = json.loads(args.protocol.read_text())
    frozen = {'protocol': protocol, 'protocol_sha256': sha(args.protocol), 'manifest_sha256': sha(args.manifest),
              'cache_metadata': json.loads((args.cache / 'metadata.json').read_text()),
              'code_sha256': {name: sha(Path(__file__).with_name(name)) for name in ('train_residual_ctc.py', 'residual_ctc.py')},
              'test_images_loaded': 0, 'external_images_loaded': 0}
    (args.output / 'run-lock.json').write_text(json.dumps(frozen, indent=2))
    data = prepare_inputs(args.root, args.manifest, args.cache, protocol)
    rows, _, _, dev, _, teacher, _, _, charset = data
    baseline_predictions = decode(teacher, charset)
    baseline = measure([baseline_predictions[i] for i in dev], [rows[i] for i in dev])
    (args.output / 'baseline-dev.json').write_text(json.dumps(baseline, indent=2))
    reports = [train_trial(data, trial, protocol, args.output, index, baseline_predictions, baseline)
               for index, trial in enumerate(protocol['trials'])]
    eligible = [report['best_eligible'] for report in reports if report['best_eligible']]
    selected = max(eligible, key=lambda item: tuple(item['ranking'])) if eligible else None
    result = {'status': 'FROZEN_FOR_INDEPENDENT_VALIDATION' if selected else 'REJECTED_KEEP_ZERO_RESIDUAL_BASELINE',
              'selected': selected, 'baseline_dev': baseline, 'trials': reports, 'test_images_loaded': 0, 'external_images_loaded': 0,
              'teacher_sha256': protocol['teacher_model_sha256'], 'freeze_unix': time.time()}
    (args.output / 'training-and-freeze.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({'status': result['status'], 'selected': selected}), flush=True)  # noqa: T201  # final research selection JSON


if __name__ == '__main__':
    main()
