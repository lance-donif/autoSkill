"""Provide fixed original-PNG preprocessing and the two predeclared decoders."""
import io

import numpy as np
from PIL import Image


def original_png_tensor(png):
    """Composite white, RGB Lanczos64, then L and exact legacy operation order."""
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    flat = Image.new('RGBA', image.size, 'white')
    flat.alpha_composite(image)
    rgb = flat.convert('RGB')
    width = max(1, int(rgb.width * (64 / rgb.height)))
    gray = rgb.resize((width, 64), Image.Resampling.LANCZOS).convert('L')
    values = np.asarray(gray, dtype=np.float32) / 255
    normalized = (values - .5) / .5
    return normalized[None, None]


def ctc_indices(indices, charset):
    """Drop blanks and consecutive identical frame classes, preserving repeats."""
    text, previous = [], 0
    for value in indices:
        index = int(value)
        if index and index != previous:
            text.append(charset[index])
        previous = index
    return ''.join(text)


def class_mappings(source_charset, target_charset):
    """Keep source and target index spaces explicit, including both tie rules."""
    if len(source_charset) != 8210 or len(set(source_charset)) != 8210 or len(target_charset) != 63:
        raise ValueError('Unexpected source or target charset contract')
    if source_charset[0] != '' or target_charset[0] != '':
        raise ValueError('Both CTC blank indices must be zero')
    target = {char: index for index, char in enumerate(target_charset)}
    source_to_target = np.asarray([target.get(char, 0) for char in source_charset], np.int64)
    target_in_source = np.asarray([source_charset.index(char) for char in target_charset], np.int64)
    return source_to_target, target_in_source


def decode_both(raw, source_to_target, target_in_source, target_charset):
    """Decode A/B from exactly one raw old-model output, without a softmax."""
    if raw.ndim != 3 or raw.shape[1:] != (1, 8210):
        raise ValueError('Expected old expert raw[T,1,8210] logits')
    source_indices = raw[:, 0].argmax(axis=1)
    full_indices = source_to_target[source_indices]
    domain_indices = np.take(raw[:, 0], target_in_source, axis=1).argmax(axis=1)
    return {'A': ctc_indices(full_indices, target_charset), 'B': ctc_indices(domain_indices, target_charset)}, {
        'source_nonblank_unsupported_frames': int(np.count_nonzero((source_indices != 0) & (full_indices == 0))),
        'source_blank_frames': int(np.count_nonzero(source_indices == 0)), 'source_total_frames': len(source_indices)}
