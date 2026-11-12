"""Reconstruct the one fixed bright-only rule using frozen predictions only."""
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from evaluate_color_layers import edits, summarize


def sha(file):
    """Check cache/protocol identity without loading any recognizer."""
    return hashlib.sha256(file.read_bytes()).hexdigest()


def route(row):
    """Apply exactly the predeclared additional foreground-mode boundary."""
    retain = row['restore'] and row['details']['a1'] >= 128
    kind = 'restored' if retain else 'foreground_abstain' if row['restore'] else 'prior_abstain' if row['flag'] else 'not_gated'
    candidate = row['candidate'] if retain else row['baseline']
    return {**row, 'candidate': candidate, 'restore': retain, 'route': kind,
            'new_empty': row['baseline'] != '' and candidate == ''}


def paired_statistics(rows):
    """Report counts only, without disclosing labels, predictions or sample ids."""
    groups = defaultdict(lambda: {'samples': 0, 'correct_to_wrong': 0, 'wrong_to_correct': 0,
                                  'correct_unchanged': 0, 'both_wrong': 0, 'both_wrong_prediction_changed': 0})
    for row in rows:
        if row['split'] != 'dev':
            continue
        before, after = row['baseline'] == row['label'], row['candidate'] == row['label']
        for category in [row['category'], 'NORMAL' if row['normal'] else 'NOISY', 'ALL']:
            group = groups[category]
            group['samples'] += 1
            group['correct_to_wrong'] += before and not after
            group['wrong_to_correct'] += not before and after
            group['correct_unchanged'] += before and after
            group['both_wrong'] += not before and not after
            group['both_wrong_prediction_changed'] += not before and not after and row['baseline'] != row['candidate']
    dark = [row for row in rows if row['split'] == 'dev' and row['category'] == 'dark-first-symbol-touching' and row['restore']]
    return {'dev_groups': dict(groups), 'dark_first_retained_summary': {
        'retained_recoveries': len(dark), 'original_whole_string_wrong': sum(row['baseline'] != row['label'] for row in dark),
        'candidate_whole_string_wrong': sum(row['candidate'] != row['label'] for row in dark),
        'original_edit_errors': sum(edits(row['baseline'], row['label']) for row in dark),
        'candidate_edit_errors': sum(edits(row['candidate'], row['label']) for row in dark)}, 'new_inference_runs': 0}


def main():
    """Recompute fixed statistics without changing the original strict CER gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new counterfactual output directory')
    protocol = json.loads(args.protocol.read_text())
    cache = Path(protocol['v1_predictions'])
    if sha(cache) != protocol['v1_predictions_sha256'] or protocol['brightness_boundary'] != 128:
        raise ValueError('The predeclared v1 cache or boundary changed')
    original = [json.loads(line) for line in cache.read_text().splitlines()]
    if len(original) != 1600 or any(row['split'] not in ['train', 'dev'] for row in original):
        raise ValueError('Only the original 1600 train/dev cache is accepted')
    rows = [route(row) for row in original]
    measured = summarize(rows)
    noise = [measured['dev/' + name] for name in ['through-line', 'dark-through-line', 'first-symbol-touching', 'dark-first-symbol-touching']]
    passed = (measured['dev/NORMAL']['prediction_changes'] == 0 and not any(row['new_empty'] for row in rows)
              and all(group['candidate_exact'] >= group['baseline_exact'] and group['candidate_edits'] <= group['baseline_edits'] for group in noise)
              and measured['dev/first-symbol-touching']['candidate_exact'] - measured['dev/first-symbol-touching']['baseline_exact'] >= 2)
    report = {'status': 'COUNTERFACTUAL_DEV_PASSED_PENDING_ACTUAL_VERIFICATION' if passed else 'REJECTED_COUNTERFACTUAL_BRIGHT_ONLY_V2',
              'groups': measured, 'new_empty_outputs': sum(row['new_empty'] for row in rows), 'model_inference_runs': 0,
              'all_retained_restores': sum(row['restore'] for row in rows),
              'all_brightness_skips': sum(row['route'] == 'foreground_abstain' for row in rows),
              'routes': dict(Counter(row['route'] for row in rows)), 'parameters_changed': False, 'test_public_or_blind_images_loaded': 0}
    args.output.mkdir(parents=True)
    (args.output / 'counterfactual-predictions.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    (args.output / 'counterfactual-report.json').write_text(json.dumps(report, indent=2) + '\n')
    (args.output / 'paired-dev-statistics.json').write_text(json.dumps(paired_statistics(rows), indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'inference_runs': 0, 'routes': report['routes']}, indent=2))  # noqa: T201  # deterministic statistics JSON


if __name__ == '__main__':
    main()
