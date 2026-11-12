"""Pinned public labeled corpus plus reproducible, font-separated hard examples."""
import hashlib
import io
import json
import math
import os
import random
import re
import subprocess
import tarfile
import zipfile
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(os.environ.get('CAPTCHA_RESEARCH_DIR', str(Path(__file__).resolve().parent)))
DATA = ROOT / 'datasets'
DATA.mkdir(exist_ok=True)
ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
records, seen_pixels, seen_labels = [], {}, set()
duplicates, invalid = [], []


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(data, label, source, split, category, extra):
    image = Image.open(io.BytesIO(data)).convert('RGB')
    pixels = sha(str(image.size).encode() + image.tobytes())
    encoded = sha(data)
    if pixels in seen_pixels:
        duplicate = {'source': source, 'label': label, 'duplicate_of': seen_pixels[pixels], 'pixel_sha256': pixels}
        duplicates.append(duplicate)
        original = next(row for row in records if row['id'] == seen_pixels[pixels])
        if original['label'] != label:
            raise ValueError('Conflicting labels for identical pixels')
        return
    identity = source + '-' + pixels[:16]
    folder = DATA / source / split
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (identity + '.png')
    image.save(destination)
    records.append({'id': identity, 'file': str(destination.relative_to(ROOT)), 'label': label,
                    'source': source, 'split': split, 'category': category, 'width': image.width, 'height': image.height,
                    'source_bytes_sha256': encoded, 'pixel_sha256': pixels,
                    'png_sha256': sha(destination.read_bytes()), **extra})
    seen_pixels[pixels] = identity


public = ROOT / 'sources/CaptchaCracker'
for archive in (public / 'captcha_images_v2.zip', public / 'captcha_images.tar.gz'):
    if archive.suffix == '.zip':
        with zipfile.ZipFile(archive) as bundle:
            items = [(name, bundle.read(name)) for name in bundle.namelist() if name.lower().endswith('.png') and not Path(name).name.startswith('._')]
    else:
        with tarfile.open(archive) as bundle:
            items = [(entry.name, bundle.extractfile(entry).read()) for entry in bundle.getmembers()
                     if entry.isfile() and entry.name.lower().endswith('.png') and not Path(entry.name).name.startswith('._')]
    for name, data in sorted(items):
        label = Path(name).stem
        if not re.fullmatch(r'[0-9A-Za-z]{4,8}', label):
            invalid.append(name)
            continue
        save(data, label, 'public-fournierp', 'external_test', 'public-noisy',
             {'archive': archive.name, 'archive_member': name, 'label_source': 'original filename',
              'license_status': 'unverified: mirror explicitly refers to original Kaggle description, inaccessible via available HTTPS route',
              'training_allowed': False})

FONTS = {
    'train': [('OpenSans', '/usr/share/fonts/truetype/open-sans/OpenSans-Regular.ttf'),
              ('Noto', '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf')],
    'dev': [('Liberation', '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'),
            ('Liberation', '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf')],
    'test': [('DejaVu', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
             ('DejaVu', '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'),
             ('DejaVu', '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf')],
}
categories = ('plain', 'pale', 'through-line', 'first-symbol-touching')
for split, per_category, seed_base in (('train', 120, 202611120000), ('dev', 40, 202611130000), ('test', 60, 202611140000)):
    for kind_index, category in enumerate(categories):
        for index in range(per_category):
            seed = seed_base + kind_index * 10000 + index
            rng = random.Random(seed)
            family, font_file = FONTS[split][index % len(FONTS[split])]
            length = 4 if index % 2 == 0 else 6
            label = ''.join(rng.choice(ALPHABET) for _ in range(length))
            while label in seen_labels:
                label = ''.join(rng.choice(ALPHABET) for _ in range(length))
            seen_labels.add(label)
            background = rng.randrange(247, 256)
            foreground_ranges = {'plain': (5, 85), 'pale': (185, 225), 'through-line': (100, 185), 'first-symbol-touching': (150, 215)}
            foreground = rng.randrange(*foreground_ranges[category])
            size = rng.randrange(28, 35)
            font = ImageFont.truetype(font_file, size)
            width, height = 184, 60
            image = Image.new('RGB', (width, height), (background,) * 3)
            draw = ImageDraw.Draw(image)
            text_x, text_y = 12, rng.randrange(7, 13)
            draw.text((text_x, text_y), label, font=font, fill=(foreground,) * 3)
            parameters = {'seed': seed, 'font_family': family, 'font_file': font_file,
                          'font_sha256': sha(Path(font_file).read_bytes()), 'font_size': size,
                          'text_origin': [text_x, text_y], 'background_gray': background,
                          'foreground_gray': foreground, 'contrast_gray': background - foreground,
                          'line_gray': None, 'line_width': 0, 'curve_amplitude': 0, 'curve_phase': 0,
                          'noise_symbol': None, 'noise_is_not_part_of_label': True,
                          'training_allowed': split == 'train', 'label_source': 'deterministic generator ground truth'}
            if category in ('through-line', 'first-symbol-touching'):
                line_gray, line_width = rng.randrange(0, 45), rng.randrange(1, 4)
                amplitude, phase = rng.uniform(3, 10), rng.uniform(0, 2 * math.pi)
                slope, centre = rng.uniform(-5, 5), rng.uniform(25, 37)
                points = [(x, centre + amplitude * math.sin((x - 12) / 42 + phase) + slope * x / width) for x in range(3, width - 3)]
                if category == 'first-symbol-touching':
                    symbol = ('hook', 'loop', 'slash')[index % 3]
                    if symbol == 'hook':
                        prefix = [(17, 11), (9, 8), (4, 15), (4, 27), (11, 34), (18, points[15][1])]
                    elif symbol == 'loop':
                        prefix = [(15 + 11 * math.cos(t / 12 * 2 * math.pi), 24 + 14 * math.sin(t / 12 * 2 * math.pi)) for t in range(13)]
                    else:
                        prefix = [(3, 46), (22, 8), (12, 26)]
                    draw.line(prefix + points[15:], fill=(line_gray,) * 3, width=line_width, joint='curve')
                    parameters['noise_symbol'] = symbol
                else:
                    draw.line(points, fill=(line_gray,) * 3, width=line_width, joint='curve')
                parameters.update(line_gray=line_gray, line_width=line_width, curve_amplitude=amplitude,
                                  curve_phase=phase, curve_slope=slope, curve_centre=centre)
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            save(buffer.getvalue(), label, 'synthetic-hard-v1', split, category, parameters)

families = {split: {family for family, _ in values} for split, values in FONTS.items()}
assert not (families['train'] & families['dev'] or families['train'] & families['test'] or families['dev'] & families['test'])
manifest = ROOT / 'datasets/manifest.jsonl'
manifest.write_text(''.join(json.dumps(row) + '\n' for row in records))
summary = {'manifest_sha256': sha(manifest.read_bytes()), 'total_unique': len(records),
           'groups': {str(key): count for key, count in Counter((row['source'], row['split'], row['category']) for row in records).items()},
           'duplicate_pixel_images_excluded': len(duplicates), 'invalid_members_excluded': invalid,
           'public_charset': ''.join(sorted({char for row in records if row['source'] == 'public-fournierp' for char in row['label']})),
           'public_dimensions': dict(Counter(str((row['width'], row['height'])) for row in records if row['source'] == 'public-fournierp')),
           'font_families': {split: sorted(values) for split, values in families.items()},
           'split_protocol': ('Public source is external-test-only, never training. Synthetic train/dev/test use disjoint font families, '
                              'seed ranges, labels and pixel hashes. Test metrics are evaluated only after candidate freeze.'),
           'public_license': 'NOT VERIFIED: LICEN(S)E mirror note refers to original Kaggle description; HTTPS metadata access failed. No public data used for training.'}
(ROOT / 'datasets/summary.json').write_text(json.dumps(summary, indent=2))
(ROOT / 'datasets/duplicates.json').write_text(json.dumps(duplicates, indent=2))
print(json.dumps(summary, indent=2))  # noqa: T201  # the dataset preparation command writes its JSON result


def prepare_apache_examples():
    """Keep the clearly licensed author examples separate from the larger corpus."""
    repo = ROOT / 'sources/captcha-recognize'
    commit = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True, text=True, capture_output=True).stdout.strip()
    examples = []
    for file in sorted((repo / 'data/test_data').glob('*')):
        if file.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
            continue
        label = file.stem.split('_')[0]
        image = Image.open(file).convert('RGB')
        pixels = sha(str(image.size).encode() + image.tobytes())
        if pixels in seen_pixels:
            raise ValueError('Licensed author example duplicates an existing source image')
        examples.append({
            'id': 'patrick-apache-' + pixels[:16], 'file': str(file.relative_to(ROOT)), 'label': label,
            'source': 'patrick-public-apache2', 'split': 'external_test',
            'category': 'upstream-lepture' if file.suffix.lower() == '.png' else 'upstream-gregwar',
            'width': image.width, 'height': image.height, 'source_bytes_sha256': sha(file.read_bytes()), 'pixel_sha256': pixels,
            'license': 'Apache-2.0', 'license_file': 'sources/captcha-recognize/LICENSE',
            'repository': 'https://github.com/PatrickLib/captcha_recognize', 'commit': commit,
            'label_source': 'author filename prefix before underscore, documented in README', 'training_allowed': False,
        })
    (ROOT / 'datasets/public-apache-manifest.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in examples))
    return len(examples)


prepare_apache_examples()
