"""Check the fixed foreground boundary without inference or study data."""
import io
import unittest

import numpy as np
from color_layer_bright_v2 import recover_bright_layers
from color_layer_core import recover_layers
from PIL import Image


def png(gray):
    stream = io.BytesIO()
    Image.fromarray(gray).save(stream, format='PNG')
    return stream.getvalue()


class BrightLayerTests(unittest.TestCase):
    """Keep self-validation order and retained recovery pixels unchanged."""

    def test_actual_foreground_modes_127_and_128(self):
        for foreground, reason in [(127, 'foreground_mode_below_128'), (128, 'restored')]:
            gray = np.full((60, 100), 245, np.uint8)
            gray[50:52] = 20
            gray[10:30, 20:40] = foreground
            source = png(gray)
            original, before = recover_layers(source)
            self.assertEqual(before['reason'], 'restored')
            after, details = recover_bright_layers(source)
            self.assertEqual(details['a1'], foreground)
            self.assertEqual(details['reason'], reason)
            if foreground == 128:
                self.assertEqual(after, original)
            else:
                self.assertIsNone(after)

    def test_prior_checks_precede_the_brightness_check(self):
        gray = np.full((60, 200), 245, np.uint8)
        gray[50:52] = 30
        gray[10:25, 10:30] = 20
        gray[30:38, 40:50] = 150
        _, details = recover_bright_layers(png(gray))
        self.assertEqual(details['reason'], 'remaining_peak_outside_middle')
        self.assertGreaterEqual(details['a1'], 128)

    def test_retained_same_pixels_and_existing_abstention(self):
        gray = np.full((60, 100), 245, np.uint8)
        gray[50:52] = 20
        gray[10:30, 20:40] = 180
        self.assertEqual(recover_bright_layers(png(gray))[0], recover_layers(png(gray))[0])
        gray[20] = 180
        self.assertEqual(recover_bright_layers(png(gray))[1]['reason'], 'middle_mode_fullwidth')


if __name__ == '__main__':
    unittest.main()
