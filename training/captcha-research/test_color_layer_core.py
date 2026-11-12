"""Verify exact multi-Otsu choices and physically valid palette restoration."""
import io
import unittest
from fractions import Fraction

import numpy as np
from color_layer_core import multi_otsu_three, recover_layers, restore_pixels
from PIL import Image, ImageDraw, ImageFont


def png(gray):
    """Encode test pixels without involving a recognizer."""
    stream = io.BytesIO()
    Image.fromarray(gray).save(stream, format='PNG')
    return stream.getvalue()


def exhaustive_fraction_oracle(histogram):
    """Use slow independent rational scores only for tiny unit-test histograms."""
    levels = np.flatnonzero(histogram)
    best, thresholds = None, None
    for left in range(len(levels) - 2):
        for right in range(left + 1, len(levels) - 1):
            score = Fraction(0)
            for group in [levels[:left + 1], levels[left + 1:right + 1], levels[right + 1:]]:
                weight = sum(int(histogram[gray]) for gray in group)
                total = sum(int(gray) * int(histogram[gray]) for gray in group)
                score += Fraction(total * total, weight)
            if best is None or score > best:
                best, thresholds = score, (int(levels[left]), int(levels[right]))
    return thresholds


class ColorLayerTests(unittest.TestCase):
    """Exercise abstention conditions separately from exact palette arithmetic."""

    def test_vectorized_candidates_match_exhaustive_rationals(self):
        generator = np.random.default_rng(2026111207)
        for count in range(3, 13):
            for _ in range(8):
                histogram = np.zeros(256, np.int64)
                occupied = generator.choice(256, count, replace=False)
                histogram[occupied] = generator.integers(1, 501, count)
                result = multi_otsu_three(histogram)
                self.assertEqual((result['t1'], result['t2']), exhaustive_fraction_oracle(histogram))

    def test_earliest_threshold_ties_and_large_counts(self):
        for scale in [1, 10**12]:
            histogram = np.zeros(256, np.int64)
            histogram[[0, 85, 170, 255]] = scale
            result = multi_otsu_three(histogram)
            self.assertEqual((result['t1'], result['t2']), (0, 85))
            self.assertGreater(result['exact_candidates'], 1)
        result = multi_otsu_three(np.ones(256, np.int64))
        self.assertEqual((result['t1'], result['t2']), (84, 169))

    def test_known_font_alpha_is_recovered_with_quantization_bound(self):
        mask = Image.new('L', (184, 60), 0)
        ImageDraw.Draw(mask).text((12, 6), 'Ab3', font=ImageFont.load_default(size=28), fill=255)
        alpha = np.asarray(mask)
        gray = np.rint(245 - (245 - 150) * alpha.astype(np.float64) / 255).astype(np.uint8)
        gray[50:52] = 20
        recovered, details = recover_layers(png(gray))
        self.assertEqual(details['reason'], 'restored')
        self.assertEqual((details['a0'], details['a1'], details['a2']), (20, 150, 245))
        output = np.asarray(Image.open(io.BytesIO(recovered)))
        self.assertEqual(output.shape, alpha.shape)
        self.assertTrue(np.all(output[50:52] == 255))
        self.assertLessEqual(int(np.abs(output.astype(np.int16) - (255 - alpha).astype(np.int16)).max()), 2)

    def test_low_pixels_deleted_and_remaining_formula_clips(self):
        gray = np.asarray([[0, 20, 21, 149, 150, 245, 255]], np.uint8)
        np.testing.assert_array_equal(restore_pixels(gray, {'t1': 20}, 150, 245), [[255, 255, 0, 0, 0, 255, 255]])

    def test_dark_text_in_low_class_abstains(self):
        gray = np.full((60, 200), 245, np.uint8)
        gray[50:52] = 30
        gray[10:25, 10:30] = 20
        gray[30:38, 40:50] = 150
        recovered, details = recover_layers(png(gray))
        self.assertIsNone(recovered)
        self.assertEqual(details['reason'], 'remaining_peak_outside_middle')

    def test_missing_classes_and_nonbackground_global_mode_abstain(self):
        for values in [[255], [20, 245]]:
            recovered, details = recover_layers(png(np.resize(np.asarray(values, np.uint8), (30, 100))))
            self.assertIsNone(recovered)
            self.assertEqual(details['reason'], 'fewer_than_three_nonempty_classes')
        gray = np.full((60, 100), 20, np.uint8)
        gray[10:12], gray[20:22] = 150, 245
        self.assertEqual(recover_layers(png(gray))[1]['reason'], 'background_not_global_mode')

    def test_global_mode_tie_chooses_smallest_gray(self):
        gray = np.asarray([[20, 150, 245]] * 40, np.uint8)
        recovered, details = recover_layers(png(gray))
        self.assertIsNone(recovered)
        self.assertEqual(details['global_mode'], 20)
        self.assertEqual(details['reason'], 'background_not_global_mode')

    def test_original_column_coverage_boundaries(self):
        for columns, expected in [(90, 'restored'), (89, 'low_mode_not_fullwidth')]:
            gray = np.full((60, 100), 245, np.uint8)
            gray[50:52, :columns] = 20
            gray[10:30, 20:40] = 150
            self.assertEqual(recover_layers(png(gray))[1]['reason'], expected)
        for columns, expected in [(90, 'middle_mode_fullwidth'), (89, 'restored')]:
            gray = np.full((60, 100), 245, np.uint8)
            gray[50:52] = 20
            gray[20, :columns] = 150
            self.assertEqual(recover_layers(png(gray))[1]['reason'], expected)

    def test_colored_low_mode_is_not_a_gray_line(self):
        rgb = np.full((60, 100, 3), 245, np.uint8)
        rgb[50:52] = (0, 50, 0)
        rgb[10:30, 20:40] = 150
        self.assertEqual(recover_layers(png(rgb))[1]['reason'], 'low_mode_not_fullwidth')


if __name__ == '__main__':
    unittest.main()
