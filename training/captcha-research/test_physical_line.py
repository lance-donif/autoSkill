"""Validate fixed physical-signal boundaries without OCR model inference."""
import unittest

import numpy as np
from diagnose_physical_line import physical_signal


def image_tensor(raw, padding=0):
    """Represent uint8 RGB with exactly the product scale and right padding."""
    height, width, _ = raw.shape
    tensor = np.zeros((1, 3, height, width + padding), dtype=np.float32)
    tensor[0, :, :, :width] = (raw.astype(np.float32)[:, :, ::-1] / np.float32(127.5) - np.float32(1)).transpose(2, 0, 1)
    return tensor


class PhysicalSignalTests(unittest.TestCase):
    """Keep coverage, RGB boundaries and valid-column semantics unambiguous."""

    def test_padding_excluded_and_edges_included(self):
        raw = np.full((3, 100, 3), 255, np.uint8)
        raw[1, 5:95] = 40
        observed = physical_signal(image_tensor(raw, padding=220))
        self.assertEqual(observed['valid_columns'], 100)
        self.assertEqual(observed['covered_columns'], 90)
        self.assertTrue(observed['flag'])
        raw[1, 94] = 255
        self.assertFalse(physical_signal(image_tensor(raw, padding=220))['flag'])

    def test_full_line_ordinary_glyphs_and_colored_line(self):
        raw = np.full((16, 100, 3), 255, np.uint8)
        raw[7] = (30, 30, 30)
        self.assertTrue(physical_signal(image_tensor(raw))['flag'])
        raw[7] = (20, 100, 20)
        self.assertFalse(physical_signal(image_tensor(raw))['flag'])
        raw[7] = 255
        raw[3:12, 20:70] = 10
        self.assertFalse(physical_signal(image_tensor(raw))['flag'])

    def test_rgb_threshold_roundtrip(self):
        for color, expected in [((127, 127, 127), True), ((128, 128, 128), False),
                                ((111, 127, 127), True), ((110, 127, 127), False)]:
            with self.subTest(color=color):
                raw = np.full((3, 20, 3), 255, np.uint8)
                raw[1] = color
                self.assertEqual(physical_signal(image_tensor(raw))['flag'], expected)

    def test_plain_white_and_no_valid_columns(self):
        raw = np.full((10, 30, 3), 255, np.uint8)
        self.assertEqual(physical_signal(image_tensor(raw))['coverage'], 0)
        with self.assertRaisesRegex(ValueError, 'no valid image columns'):
            physical_signal(np.zeros((1, 3, 48, 320), np.float32))


if __name__ == '__main__':
    unittest.main()
