"""Measure the fixed v4.1 sparse through-line signal on train/dev product tensors."""
import argparse
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from historical_solver import load_solver

COVERAGE_THRESHOLD = .90
MAX_RGB_EXCLUSIVE = 128
MAX_RGB_SPREAD = 16
MAX_DARK_PIXEL_FRACTION = .50


def sha(file):
    """Record exact provenance without following a moving product model."""
    import hashlib

    return hashlib.sha256(file.read_bytes()).hexdigest()


def physical_signal(tensor):
    """Count all valid columns with at least one near-black near-gray pixel."""
    if tensor.ndim != 4 or tensor.shape[0] != 1 or tensor.shape[1] != 3:
        raise ValueError('Expected one product-preprocessed NCHW image')
    # Real resized uint8 RGB cannot map to exactly zero in every channel;
    # product right-padding is exactly zero. Include both image edge columns.
    valid = np.any(tensor[0] != 0, axis=(0, 1))
    count = int(valid.sum())
    if not count:
        return {'flag': False, 'coverage': 0., 'covered_columns': 0, 'valid_columns': 0, 'dark_pixel_fraction': 0.}
    raw = np.rint(np.float32(127.5) * (tensor[0] + np.float32(1)))
    maximum, minimum = raw.max(axis=0), raw.min(axis=0)
    dark_gray = (maximum < MAX_RGB_EXCLUSIVE) & ((maximum - minimum) <= MAX_RGB_SPREAD)
    covered = np.any(dark_gray, axis=0) & valid
    covered_count = int(covered.sum())
    coverage = covered_count / count
    dark_count = int(np.count_nonzero(dark_gray & valid[None]))
    fraction = dark_count / (count * tensor.shape[2])
    return {'flag': coverage >= COVERAGE_THRESHOLD and fraction <= MAX_DARK_PIXEL_FRACTION, 'coverage': coverage,
            'covered_columns': covered_count, 'valid_columns': count, 'dark_pixel_fraction': fraction}


class CapturingSession:
    """Expose cached teacher logits while capturing the historical input tensor."""

    def __init__(self):
        self.tensor = None
        self.frames = None

    @staticmethod
    def get_inputs():
        return [SimpleNamespace(name='image')]

    def run(self, output_names, inputs):
        if output_names is not None:
            raise ValueError('Expected the product default output request')
        self.tensor = inputs['image']
        return [self.frames]


def load_inputs(root, manifest, cache, protocol):
    """Restrict observations to the fixed train/dev manifest and teacher cache."""
    if sha(manifest) != protocol['manifest_sha256']:
        raise ValueError('Diagnostic manifest differs from the predeclared train/dev input')
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    if any(row['source'] != 'synthetic-residual-v2' or row['split'] not in ('train', 'dev') for row in rows):
        raise ValueError('Diagnostic may only read the fixed synthetic train/dev data')
    metadata = json.loads((cache / 'metadata.json').read_text())
    if metadata['manifest_sha256'] != sha(manifest) or metadata['model_sha256'] != protocol['teacher_model_sha256']:
        raise ValueError('Teacher feature cache provenance changed')
    features_file = cache / 'features.npz'
    if sha(features_file) != metadata['cache_sha256']:
        raise ValueError('Teacher feature cache bytes changed')
    arrays = np.load(features_file)
    if list(arrays['ids']) != [row['id'] for row in rows]:
        raise ValueError('Cached teacher ordering differs from the diagnostic manifest')
    meta_file = root / 'baselines/original-be9dd198/captcha-ctc.json'
    if sha(meta_file) != protocol['teacher_metadata_sha256']:
        raise ValueError('Original product metadata changed')
    return rows, arrays['logits'], json.loads(meta_file.read_text())


def diagnostic(root, manifest, cache, product, protocol, output):
    """Report flags without modifying predictions or evaluating any new model."""
    if output.exists():
        raise ValueError('Use a new physical-diagnostic output directory')
    expected = {'coverage_minimum': COVERAGE_THRESHOLD, 'max_rgb_exclusive': MAX_RGB_EXCLUSIVE, 'max_rgb_spread_inclusive': MAX_RGB_SPREAD,
                'max_dark_gray_pixel_fraction_inclusive': MAX_DARK_PIXEL_FRACTION}
    if protocol['fixed_signal'] != expected:
        raise ValueError('The predeclared physical thresholds do not match this implementation')
    rows, logits, meta = load_inputs(root, manifest, cache, protocol)
    captcha = load_solver(product)
    output.mkdir(parents=True)
    (output / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n')
    lock = {'code_sha256': sha(Path(__file__)), 'manifest_sha256': sha(manifest), 'protocol': protocol,
            'product_solver_sha256': sha(Path(captcha.__file__)), 'historical_solver': captcha.research_provenance,
            'teacher_cache_sha256': sha(cache / 'features.npz'), 'model_inference_runs': 0,
            'training_runs': 0, 'test_images_loaded': 0, 'external_images_loaded': 0}
    (output / 'run-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    session = CapturingSession()
    charset, (_, height, width) = meta['charset'], meta['imageShape']
    groups = defaultdict(lambda: {'samples': 0, 'flags': 0, 'baseline_correct': 0, 'baseline_correct_flags': 0})
    observations = []
    with patch.object(captcha, '_model', return_value=(session, charset, height, width)):
        for row, frames in zip(rows, logits):
            image = root / row['file']
            if sha(image) != row['png_sha256']:
                raise ValueError('Input image changed from its frozen manifest bytes')
            session.frames = frames[None]
            try:
                prediction = captcha.solve(image.read_bytes())
            except ValueError as error:
                if '没有从验证码图片里识别出字符' not in str(error):
                    raise
                prediction = ''
            signal = physical_signal(session.tensor)
            correct = prediction == row['label']
            for category in (row['category'], 'NORMAL' if row['normal'] else 'NOISY', 'ALL'):
                group = groups[row['split'] + '/' + category]
                group['samples'] += 1
                group['flags'] += signal['flag']
                group['baseline_correct'] += correct
                group['baseline_correct_flags'] += correct and signal['flag']
            observations.append({'id': row['id'], 'split': row['split'], 'category': row['category'],
                                 'normal': row['normal'], 'baseline_correct': correct, **signal})
    for group in groups.values():
        group['flag_rate'] = group['flags'] / group['samples']
    report = {'status': 'DIAGNOSTIC_ONLY_NO_CONDITIONAL_MODEL_EVALUATED', 'groups': dict(groups),
              'note': 'Normal baseline-correct flags are physical false flags, not measured new recognition errors. No residual is applied.',
              'model_inference_runs': 0, 'training_runs': 0, 'test_images_loaded': 0, 'external_images_loaded': 0}
    (output / 'observations.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in observations))
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    """Run only the fixed physical observation protocol, never model selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--product', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous', type=Path, required=True)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    report = diagnostic(args.root, args.manifest, args.cache, args.product, protocol, args.output)
    previous_file = args.previous / 'observations.jsonl'
    if sha(previous_file) != protocol['previous_v4_observations_sha256']:
        raise ValueError('Previous v4 observations differ from the predeclared comparison')
    before = [json.loads(line) for line in previous_file.read_text().splitlines()]
    after = [json.loads(line) for line in (args.output / 'observations.jsonl').read_text().splitlines()]
    if [row['id'] for row in before] != [row['id'] for row in after]:
        raise ValueError('v4 and v4.1 diagnostic observations have different ordering')
    changed = [new['id'] for old, new in zip(before, after) if old['flag'] != new['flag']]
    report['previous_v4_comparison'] = {'samples': len(after), 'flag_changes': len(changed), 'changed_ids': changed}
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))  # noqa: T201  # research command JSON result


if __name__ == '__main__':
    main()
