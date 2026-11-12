"""Rebuild restored-image exclusion metadata without inference or PNG files."""
import argparse
import hashlib
import io
import json
from pathlib import Path

from color_layer_core import recover_layers
from PIL import Image


def sha(file):
    """Verify the immutable input/cache provenance before deriving any pixels."""
    return hashlib.sha256(file.read_bytes()).hexdigest()


def derive(row, parent, retained, protocol, core_sha):
    """Inherit labels/fonts/seeds and reproduce the already evaluated transform."""
    file = Path(protocol['data_root']) / parent['file']
    if sha(file) != parent['png_sha256']:
        raise ValueError('A source PNG changed')
    restored, details = recover_layers(file.read_bytes())
    keys = ['t1', 't2', 'a0', 'a1', 'a2', 'low_mode_columns', 'middle_mode_columns']
    if restored is None or any(details[key] != row['details'][key] for key in keys):
        raise ValueError('A frozen v1 recovery decision changed')
    image = Image.open(io.BytesIO(restored)).convert('RGB')
    pixel_sha = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
    return {'id': 'derived-color-v1/' + row['id'], 'source': 'derived-three-layer-v1', 'label': parent['label'],
            'pixel_sha256': pixel_sha, 'png_sha256': hashlib.sha256(restored).hexdigest(),
            'width': image.width, 'height': image.height, 'seed': parent.get('seed'), 'variant_seed': parent.get('variant_seed'),
            'font_family': parent['font_family'], 'font_file': parent['font_file'], 'font_sha256': parent['font_sha256'],
            'parent_id': parent['id'], 'root_parent_id': parent.get('parent_id', parent['id']),
            'source_file': parent['file'], 'source_png_sha256': parent['png_sha256'],
            'source_manifest_sha256': protocol['data_manifest_sha256'],
            'derivation': 'Frozen v1 palette recovery; deterministic, no new seed or labels.',
            'derivation_core_sha256': core_sha, 'retained_by_v2': retained}


def main():
    """Only regenerate metadata; never run a model or persist transformed PNGs."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['v1-protocol', 'v2-protocol', 'v2-freeze', 'v2-predictions', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new exclusion metadata file')
    first = json.loads(args.v1_protocol.read_text())
    second = json.loads(args.v2_protocol.read_text())
    freeze = json.loads(args.v2_freeze.read_text())
    source = Path(second['v1_predictions'])
    manifest = Path(first['data_manifest'])
    core_sha = sha(Path(__file__).with_name('color_layer_core.py'))
    pairs = [(source, second['v1_predictions_sha256']), (manifest, first['data_manifest_sha256']),
             (args.v2_predictions, freeze['counterfactual_predictions_sha256'])]
    if core_sha != second['v1_core_sha256'] or any(sha(file) != expected for file, expected in pairs):
        raise ValueError('A fixed source/cache/core fingerprint changed')
    parents = {row['id']: row for row in [json.loads(line) for line in manifest.read_text().splitlines()]}
    original = [json.loads(line) for line in source.read_text().splitlines()]
    final = {row['id']: row for row in [json.loads(line) for line in args.v2_predictions.read_text().splitlines()]}
    if len(original) != 1600 or any(row['split'] not in ['train', 'dev'] for row in original):
        raise ValueError('Only the original train/dev derivations are accepted')
    rows = [derive(row, parents[row['id']], final[row['id']]['restore'], first, core_sha) for row in original if row['restore']]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    report = {'rows': len(rows), 'unique_pixel_hashes': len({row['pixel_sha256'] for row in rows}),
              'manifest_sha256': sha(args.output), 'model_runs': 0, 'persisted_PNGs': 0}
    print(json.dumps(report, indent=2))  # noqa: T201  # exclusion metadata provenance


if __name__ == '__main__':
    main()
