"""Check sparse-line semantics, including ordinary dark-background captchas."""
import unittest

import numpy as np
from diagnose_physical_line_v41 import physical_signal
from test_physical_line import image_tensor


class SparsePhysicalSignalTests(unittest.TestCase):
    """Reject dark image fields while retaining the same fixed thin-line limits."""

    def test_dark_background_is_not_a_line(self):
        for background in [0, 70, 127]:
            raw = np.full((48, 100, 3), background, np.uint8)
            self.assertFalse(physical_signal(image_tensor(raw, padding=220))['flag'])
            raw[10:36, 20:70] = 255
            observed = physical_signal(image_tensor(raw, padding=220))
            self.assertEqual(observed['coverage'], 1)
            self.assertGreater(observed['dark_pixel_fraction'], .5)
            self.assertFalse(observed['flag'])

    def test_half_pixel_fraction_inclusive_boundary(self):
        raw = np.full((48, 100, 3), 255, np.uint8)
        raw[:24] = 30
        self.assertTrue(physical_signal(image_tensor(raw))['flag'])
        raw[24] = 30
        self.assertFalse(physical_signal(image_tensor(raw))['flag'])

    def test_fixed_coverage_and_color_thresholds(self):
        for color, expected in [((127, 127, 127), True), ((128, 128, 128), False),
                                ((111, 127, 127), True), ((110, 127, 127), False), ((20, 100, 20), False)]:
            raw = np.full((48, 100, 3), 255, np.uint8)
            raw[20, 5:95] = color
            self.assertEqual(physical_signal(image_tensor(raw, padding=220))['flag'], expected)
        raw = np.full((48, 100, 3), 255, np.uint8)
        raw[20, 5:94] = 30
        self.assertFalse(physical_signal(image_tensor(raw))['flag'])

    def test_all_zero_tensor_returns_false(self):
        observed = physical_signal(np.zeros((1, 3, 48, 320), np.float32))
        self.assertFalse(observed['flag'])
        self.assertEqual(observed['valid_columns'], 0)
        self.assertEqual(observed['coverage'], 0)


if __name__ == '__main__':
    unittest.main()
