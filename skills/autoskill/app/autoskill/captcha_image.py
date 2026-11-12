"""Recover light CAPTCHA ink when sparse dark lines form a separate color layer."""
import numpy as np
from PIL import Image


def multi_otsu_three(histogram):
    """Find the global nonempty-class optimum, with exact near-tie comparisons."""
    histogram = np.asarray(histogram)
    levels = np.flatnonzero(histogram)
    if len(levels) < 3:
        return None
    counts, sums = [], []
    count, total = 0, 0
    for gray in levels:
        value = int(histogram[gray])
        count += value
        total += int(gray) * value
        counts.append(count)
        sums.append(total)
    first, second = np.triu_indices(len(levels) - 1, k=1)
    cw, cs = np.asarray(counts, np.float64), np.asarray(sums, np.float64)
    w0, s0 = cw[first], cs[first]
    w1, s1 = cw[second] - w0, cs[second] - s0
    w2, s2 = count - cw[second], total - cs[second]
    scores = s0 * s0 / w0 + s1 * s1 / w1 + s2 * s2 / w2
    tolerance = 16 * np.finfo(np.float64).eps * count * 255 * 255
    candidates = np.flatnonzero(scores >= scores.max() - tolerance)
    best = None
    for index in candidates:
        low, middle = int(first[index]), int(second[index])
        a, b, c = counts[low], counts[middle] - counts[low], count - counts[middle]
        x, y, z = sums[low], sums[middle] - sums[low], total - sums[middle]
        numerator = x * x * b * c + y * y * a * c + z * z * a * b
        denominator = a * b * c
        if best is None or numerator * best[1] > best[0] * denominator:
            best = (numerator, denominator, int(levels[low]), int(levels[middle]))
    return {'t1': best[2], 't2': best[3], 'nonzero_levels': len(levels),
            'exact_candidates': len(candidates), 'score_tolerance': float(tolerance)}


def palette_validation(rgb, gray, histogram, thresholds):
    """Reject palettes that cannot identify background, line and text modes."""
    t1, t2 = thresholds['t1'], thresholds['t2']
    a0 = int(np.argmax(histogram[:t1 + 1]))
    a1 = int(np.argmax(histogram[t1 + 1:t2 + 1])) + t1 + 1
    a2 = int(np.argmax(histogram[t2 + 1:])) + t2 + 1
    details = {**thresholds, 'a0': a0, 'a1': a1, 'a2': a2}
    if not a0 < a1 < a2:
        return 'modes_not_ordered', details
    global_mode = int(np.argmax(histogram))
    details['global_mode'] = global_mode
    if a2 != global_mode:
        return 'background_not_global_mode', details
    remaining = histogram.copy()
    remaining[a0] = remaining[a2] = 0
    peak = int(np.argmax(remaining))
    details['remaining_peak'] = peak
    if not t1 < peak <= t2:
        return 'remaining_peak_outside_middle', details
    near_gray = rgb.max(axis=2) - rgb.min(axis=2) <= 16
    low_columns = int(np.any((gray == a0) & near_gray, axis=0).sum())
    middle_columns = int(np.any((gray == a1) & near_gray, axis=0).sum())
    width = gray.shape[1]
    details.update(low_mode_columns=low_columns, middle_mode_columns=middle_columns, width=width,
                   low_mode_coverage=low_columns / width, middle_mode_coverage=middle_columns / width)
    if low_columns * 10 < width * 9:
        return 'low_mode_not_fullwidth', details
    if middle_columns * 10 >= width * 9:
        return 'middle_mode_fullwidth', details
    return None, details


def restore_pixels(gray, thresholds, a1, a2):
    """Restore only observed antialias coverage; erase low pixels without inpainting."""
    low = gray <= thresholds['t1']
    recovered = np.rint(np.clip(255 * (gray.astype(np.float64) - a1) / (a2 - a1), 0, 255)).astype(np.uint8)
    recovered[low] = 255
    return recovered


def sparse_gray_line(tensor):
    """Use the fixed physical signal, excluding zero padding and dark backgrounds."""
    valid = np.any(tensor[0] != 0, axis=(0, 1))
    columns = int(valid.sum())
    if not columns:
        return False
    raw = np.rint(np.float32(127.5) * (tensor[0] + np.float32(1)))
    maximum, minimum = raw.max(axis=0), raw.min(axis=0)
    dark = (maximum < 128) & ((maximum - minimum) <= 16) & valid[None, :]
    covered = int(np.any(dark, axis=0).sum())
    pixels = int(dark.sum())
    return covered * 10 >= columns * 9 and pixels * 2 <= columns * tensor.shape[2]


def recover_light_text(image, tensor):
    """Return restored RGB ink only when the foreground is a validated light layer."""
    if not sparse_gray_line(tensor):
        return None
    rgb = np.asarray(image)
    gray = np.asarray(image.convert('L'))
    histogram = np.bincount(gray.ravel(), minlength=256)
    thresholds = multi_otsu_three(histogram)
    if thresholds is None:
        return None
    reason, details = palette_validation(rgb, gray, histogram, thresholds)
    if reason or details['a1'] < 128:
        return None
    return Image.fromarray(restore_pixels(gray, thresholds, details['a1'], details['a2'])).convert('RGB')
