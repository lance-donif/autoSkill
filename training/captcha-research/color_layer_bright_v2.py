"""Add the one fixed light-foreground rule after all original palette checks."""
import io

import numpy as np
from color_layer_core import multi_otsu_three, palette_validation, restore_pixels
from PIL import Image

FOREGROUND_MINIMUM = 128


def recover_bright_layers(png):
    """Preserve v1 checks/pixels and abstain before encoding on dark foreground."""
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    flat = Image.new('RGBA', image.size, 'white')
    flat.alpha_composite(image)
    image = flat.convert('RGB')
    rgb, gray = np.asarray(image), np.asarray(image.convert('L'))
    histogram = np.bincount(gray.ravel(), minlength=256)
    thresholds = multi_otsu_three(histogram)
    if thresholds is None:
        return None, {'reason': 'fewer_than_three_nonempty_classes'}
    reason, details = palette_validation(rgb, gray, histogram, thresholds)
    if reason:
        return None, {'reason': reason, **details}
    if details['a1'] < FOREGROUND_MINIMUM:
        return None, {'reason': 'foreground_mode_below_128', **details}
    pixels = restore_pixels(gray, thresholds, details['a1'], details['a2'])
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, format='PNG')
    return stream.getvalue(), {'reason': 'restored', **details}
