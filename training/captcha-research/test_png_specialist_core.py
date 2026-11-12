"""Check original-PNG alpha semantics, legacy norm and distinct decoder priors."""
import io
import string
import unittest

import numpy as np
from PIL import Image
from png_specialist_core import class_mappings, ctc_indices, decode_both, original_png_tensor


def png(image):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


class PngSpecialistTests(unittest.TestCase):
    """Test contracts with synthetic pixels/logits, never study images or a model."""

    def test_transparent_source_composites_white(self):
        observed = original_png_tensor(png(Image.new('RGBA', (100, 50), (0, 0, 0, 0))))
        np.testing.assert_array_equal(observed, np.ones((1, 1, 64, 128), np.float32))
        black = original_png_tensor(png(Image.new('RGB', (1, 100), 'black')))
        np.testing.assert_array_equal(black, -np.ones((1, 1, 64, 1), np.float32))

    def test_exact_official_resize_norm_operation_order(self):
        image = Image.new('RGB', (103, 59), (23, 154, 201))
        image.putpixel((0, 0), (251, 1, 88))
        expected = np.asarray(image.resize((int(103 * (64 / 59)), 64), Image.Resampling.LANCZOS).convert('L'), np.float32) / 255
        expected = (expected - .5) / .5
        np.testing.assert_array_equal(original_png_tensor(png(image)), expected[None, None])

    def test_domain_candidate_is_fixed_and_does_not_aggregate_probability(self):
        target = ['', *(string.digits + string.ascii_lowercase + string.ascii_uppercase)]
        source = ['', *reversed(target[1:]), *(chr(0x4E00 + i) for i in range(8147))]
        lookup, indices = class_mappings(source, target)
        raw = np.full((4, 1, 8210), -100, np.float32)
        a, unknown = source.index('A'), 63
        raw[0, 0, a], raw[1, 0, a], raw[2, 0, 0], raw[3, 0, a] = 2, 2, 2, 2
        raw[0, 0, unknown] = 3
        predictions, _ = decode_both(raw, lookup, indices, target)
        self.assertEqual(predictions, {'A': 'AA', 'B': 'AA'})
        raw[1, 0, unknown] = 3
        predictions, diagnostic = decode_both(raw, lookup, indices, target)
        self.assertEqual(predictions['A'], 'A')
        self.assertEqual(predictions['B'], 'AA')
        self.assertEqual(diagnostic['source_nonblank_unsupported_frames'], 2)
        self.assertEqual(ctc_indices([1, 1, 0, 1], ['', 'A']), 'AA')


if __name__ == '__main__':
    unittest.main()
