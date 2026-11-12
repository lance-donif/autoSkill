"""Prepare balanced ordinary/dark-noise train/dev pairs without reading tests."""
import argparse
import hashlib
import json
import math
import random
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def pixel_sha(image):
    """Hash dimensions and decoded RGB pixels for split-independent deduplication."""
    return hashlib.sha256(str(image.size).encode() + image.convert('RGB').tobytes()).hexdigest()


def noise_path(row):
    """Reconstruct the recorded interference geometry, never the target label."""
    width = row['width']
    amplitude, phase = row['curve_amplitude'], row['curve_phase']
    slope, centre = row['curve_slope'], row['curve_centre']
    points = [(x, centre + amplitude * math.sin((x - 12) / 42 + phase) + slope * x / width) for x in range(3, width - 3)]
    symbol = row['noise_symbol']
    if symbol == 'hook':
        prefix = [(17, 11), (9, 8), (4, 15), (4, 27), (11, 34), (18, points[15][1])]
    elif symbol == 'loop':
        prefix = [(15 + 11 * math.cos(t / 12 * 2 * math.pi), 24 + 14 * math.sin(t / 12 * 2 * math.pi)) for t in range(13)]
    elif symbol == 'slash':
        prefix = [(3, 46), (22, 8), (12, 26)]
    else:
        return points
    return prefix + points[15:]


def render_variant(row, variant):
    """Generate a labeled clean/dark/ordinary variant solely from its train/dev row."""
    seed = int(hashlib.sha256((row['id'] + '/' + variant + '/residual-v2').encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    font = ImageFont.truetype(row['font_file'], row['font_size'])
    background = 255 if variant == 'paired-clean' else rng.randrange(230, 256)
    foreground = 0 if variant == 'paired-clean' else rng.randrange(0, 75)
    image = Image.new('RGB', (row['width'], row['height']), (background,) * 3)
    draw = ImageDraw.Draw(image)
    draw.text(tuple(row['text_origin']), row['label'], font=font, fill=(foreground,) * 3)
    parameters = {'variant_seed': seed, 'variant_background': background, 'variant_foreground': foreground}
    if variant == 'dark-noise':
        draw.line(noise_path(row), fill=(row['line_gray'],) * 3, width=row['line_width'], joint='curve')
        parameters['variant_noise_gray'] = row['line_gray']
    elif variant == 'ordinary-aug':
        rotation, blur = rng.uniform(-4, 4), rng.uniform(.2, .7)
        image = image.rotate(rotation, resample=Image.Resampling.BILINEAR, fillcolor=(background,) * 3)
        image = image.filter(ImageFilter.GaussianBlur(blur))
        parameters.update(variant_rotation_degrees=rotation, variant_blur_radius=blur)
    return image, parameters


def prepare(root, source_manifest, output):
    """Write only train/dev images and a versioned balanced manifest."""
    if output.exists():
        raise ValueError('Refuse to overwrite an existing residual dataset version')
    output.mkdir(parents=True)
    records, hashes, duplicates = [], {}, []
    for line in source_manifest.read_text().splitlines():
        row = json.loads(line)
        if row['source'] != 'synthetic-hard-v1' or row['split'] not in ('train', 'dev'):
            continue
        variants = ['original']
        if row['category'] != 'plain':
            variants.append('paired-clean')
        if row['category'] in ('through-line', 'first-symbol-touching'):
            variants.append('dark-noise')
        if row['category'] == 'plain':
            variants.append('ordinary-aug')
        for variant in variants:
            if variant == 'original':
                image = Image.open(root / row['file']).convert('RGB')
                parameters = {}
            else:
                image, parameters = render_variant(row, variant)
            digest = pixel_sha(image)
            if digest in hashes:
                previous = hashes[digest]
                if previous['split'] != row['split'] or previous['label'] != row['label']:
                    raise ValueError('Cross-split pixels or conflicting labels detected')
                duplicates.append({'parent': row['id'], 'variant': variant, 'duplicate_of': previous['id']})
                continue
            identity = row['id'] + '/' + variant
            file = output / row['split'] / (digest + '.png')
            file.parent.mkdir(exist_ok=True)
            image.save(file)
            category = row['category'] if variant == 'original' else 'dark-' + row['category'] if variant == 'dark-noise' else variant
            record = {**row, **parameters, 'id': identity, 'parent_id': row['id'], 'variant': variant,
                      'source': 'synthetic-residual-v2', 'category': category, 'file': str(file.relative_to(root)),
                      'normal': category in ('plain', 'paired-clean', 'ordinary-aug'),
                      'pixel_sha256': digest, 'png_sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
            records.append(record)
            hashes[digest] = record
    train_labels = {row['label'] for row in records if row['split'] == 'train'}
    dev_labels = {row['label'] for row in records if row['split'] == 'dev'}
    if train_labels & dev_labels:
        raise ValueError('Labels cross train/dev boundaries')
    train_fonts = {row['font_family'] for row in records if row['split'] == 'train'}
    dev_fonts = {row['font_family'] for row in records if row['split'] == 'dev'}
    if train_fonts & dev_fonts:
        raise ValueError('Font families cross train/dev boundaries')
    manifest = output / 'manifest.jsonl'
    manifest.write_text(''.join(json.dumps(row) + '\n' for row in records))
    summary = {'source_manifest_sha256': hashlib.sha256(source_manifest.read_bytes()).hexdigest(),
               'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
               'groups': {str(key): count for key, count in Counter((row['split'], row['category']) for row in records).items()},
               'ordinary_noisy_counts': {str(key): count for key, count in Counter((row['split'], row['normal']) for row in records).items()},
               'duplicate_images_excluded': len(duplicates), 'test_images_read': 0, 'external_images_read': 0,
               'note': 'No new test generated here. Labels/font families inherited within the original train/dev split.'}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root.resolve(), args.manifest.resolve(), args.output.resolve()), indent=2))  # noqa: T201  # research command JSON result
