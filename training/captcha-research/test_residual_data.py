"""Check copied residual preprocessing against the product without running ORT."""
import importlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image
from residual_data import decode_one, frozen_metadata, prepare


class CapturingSession:
    """Capture product inputs while returning deterministic CTC frames."""

    def __init__(self):
        self.tensor = None
        self.frames = np.asarray([[[0, 1, 0], [0, 1, 0], [1, 0, 0], [0, 1, 0]]], dtype=np.float32)

    @staticmethod
    def get_inputs():
        return [SimpleNamespace(name='image')]

    def run(self, output_names, inputs):
        self.tensor = inputs['image'].copy()
        if output_names is not None:
            raise AssertionError('The product should request its default outputs')
        return [self.frames]


class ResidualDataTests(unittest.TestCase):
    """Use the real product solve function with a non-inference session double."""

    @classmethod
    def setUpClass(cls):
        product = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(product / 'skills/autoskill/app'))
        cls.captcha = importlib.import_module('autoskill.captcha')
        if not Path(cls.captcha.__file__).resolve().is_relative_to(product):
            raise AssertionError('The product solver was imported from another worktree')

    def test_product_preprocessing_and_decoding(self):
        for mode, size, color in [('RGB', (17, 31), (23, 154, 201)),
                                  ('RGBA', (103, 41), (99, 40, 188, 87)),
                                  ('RGBA', (900, 13), (210, 10, 70, 0))]:
            with self.subTest(mode=mode, size=size):
                image = Image.new(mode, size, color)
                image.putpixel((0, 0), (245, 17, 66) if mode == 'RGB' else (245, 17, 66, 201))
                stream = io.BytesIO()
                image.save(stream, format='PNG')
                png = stream.getvalue()
                session = CapturingSession()
                charset = ['', 'A', 'B']
                with patch.object(self.captcha, '_model', return_value=(session, charset, 48, 320)):
                    self.assertEqual(self.captcha.solve(png), 'AA')
                np.testing.assert_array_equal(prepare(png, 48, 320), session.tensor)
                self.assertEqual(decode_one(session.frames, charset), 'AA')

    def test_metadata_mismatch_rejected(self):
        with (patch.object(Path, 'read_bytes', return_value=b'{"charset": ["", "wrong"]}'),
              self.assertRaisesRegex(ValueError, 'metadata snapshot changed')):
            frozen_metadata(Path('/unused'))


if __name__ == '__main__':
    unittest.main()
