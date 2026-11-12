"""Check CAPTCHA recovery routing, original pixel semantics and empty-result fallback."""
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/autoskill/app'))

from autoskill import captcha, captcha_image


def picture(foreground=150, line=True):
    """Provide a known three-layer palette independently of any OCR predictions."""
    pixels = np.full((60, 184, 3), 245, np.uint8)
    pixels[10:30, 20:40] = foreground
    if line:
        pixels[50:52] = 20
    return Image.fromarray(pixels)


def encoded(image):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def frames(indices):
    values = np.zeros((1, len(indices), 2), np.float32)
    for row, label in enumerate(indices):
        values[0, row, label] = 1
    return values


class Session:
    """Capture real product tensors while providing a prescribed CTC response."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.inputs = []

    @staticmethod
    def get_inputs():
        return [SimpleNamespace(name='image')]

    def run(self, names, inputs):
        if names is not None:
            raise AssertionError('Expected default model outputs')
        self.inputs.append(inputs['image'].copy())
        return [self.outputs.pop(0)]


class CaptchaRecoveryTests(unittest.TestCase):
    def read(self, image, session):
        with patch.object(captcha, '_model', return_value=(session, ['', 'a'], 48, 320)):
            return captcha.solve(encoded(image))

    def test_original_color_order_scale_padding_and_ctc_repeats(self):
        image = Image.new('RGB', (2, 1), (0, 127, 255))
        session = Session(frames([1, 1, 0, 1]))
        self.assertEqual(self.read(image, session), 'aa')
        tensor = session.inputs[0]
        self.assertEqual(tensor.shape, (1, 3, 48, 320))
        self.assertEqual(float(tensor[0, 0, 0, 0]), 1)
        self.assertEqual(float(tensor[0, 2, 0, 0]), -1)
        self.assertAlmostEqual(float(tensor[0, 1, 0, 0]), 127 / 127.5 - 1, places=6)
        self.assertTrue(np.all(tensor[:, :, :, 96:] == 0))
        self.assertEqual(len(session.inputs), 1)

    def test_validated_light_layer_recovers_ink_and_removes_line(self):
        session = Session(frames([1]))
        self.assertEqual(self.read(picture(), session), 'a')
        tensor = session.inputs[0]
        self.assertEqual(float(tensor[0, 0, 16, 24]), -1)
        self.assertEqual(float(tensor[0, 0, 40, 100]), 1)
        self.assertTrue(np.all(tensor[:, :, :, 148:] == 0))

    def test_dark_foreground_boundary_keeps_original_pixels(self):
        for foreground, recovered in ((127, False), (128, True)):
            with self.subTest(foreground=foreground):
                session = Session(frames([1]))
                self.assertEqual(self.read(picture(foreground), session), 'a')
                expected = -1 if recovered else foreground / 127.5 - 1
                self.assertAlmostEqual(float(session.inputs[0][0, 0, 16, 24]), expected, places=6)

    def test_ordinary_light_text_uses_original_tensor(self):
        session = Session(frames([1]))
        self.assertEqual(self.read(picture(line=False), session), 'a')
        self.assertAlmostEqual(float(session.inputs[0][0, 0, 16, 24]), 150 / 127.5 - 1, places=6)

    def test_dark_background_and_padding_do_not_look_like_sparse_lines(self):
        with patch.object(captcha_image, 'multi_otsu_three', side_effect=AssertionError('Unexpected recovery')):
            self.assertIsNone(captcha_image.recover_light_text(picture(), np.full((1, 3, 48, 320), -1, np.float32)))
            self.assertIsNone(captcha_image.recover_light_text(picture(), np.zeros((1, 3, 48, 320), np.float32)))

    def test_empty_recovery_retries_the_original_tensor(self):
        session = Session(frames([0]), frames([1]))
        self.assertEqual(self.read(picture(), session), 'a')
        self.assertEqual(len(session.inputs), 2)
        self.assertEqual(float(session.inputs[0][0, 0, 16, 24]), -1)
        self.assertAlmostEqual(float(session.inputs[1][0, 0, 16, 24]), 150 / 127.5 - 1, places=6)

    def test_no_character_error_keeps_the_original_message(self):
        message = '没有从验证码图片里识别出字符；这类验证码（滑块、点选、算式）不在支持范围内'
        for image, outputs in ((picture(), (frames([0]), frames([0]))),
                               (picture(line=False), (frames([0]),))):
            with self.subTest(recovery=len(outputs) == 2):
                session = Session(*outputs)
                with self.assertRaises(ValueError) as raised:
                    self.read(image, session)
                self.assertEqual(str(raised.exception), message)
                self.assertEqual(len(session.inputs), len(outputs))


if __name__ == '__main__':
    unittest.main()
