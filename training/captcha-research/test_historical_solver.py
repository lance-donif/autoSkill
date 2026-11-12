"""Keep historical evaluations on original tensors after production recovery ships."""
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import diagnose_physical_line
import diagnose_physical_line_v41
import evaluate_color_layers
import evaluate_conditional_expert
import evaluate_png_specialist
import numpy as np
from historical_solver import SOURCE_SHA256, FrozenSourceLoader, load_solver
from PIL import Image
from residual_data import prepare


class Session:
    """Capture tensors without loading a recognition model or using predictions."""

    def __init__(self, frames):
        self.frames = frames
        self.inputs = []

    @staticmethod
    def get_inputs():
        return [SimpleNamespace(name='image')]

    def run(self, outputs, inputs):
        if outputs is not None:
            raise AssertionError('Expected default model outputs')
        self.inputs.append(inputs['image'].copy())
        return [self.frames]


class HistoricalSolverTests(unittest.TestCase):
    """Exercise the exact light-text/black-line input that broke old tools."""

    @classmethod
    def setUpClass(cls):
        cls.product = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(cls.product / 'skills/autoskill/app'))
        cls.current = importlib.import_module('autoskill.captcha')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        pixels = np.full((60, 184, 3), 245, np.uint8)
        pixels[10:30, 20:40] = 150
        pixels[50:52] = 20
        stream = io.BytesIO()
        Image.fromarray(pixels).save(stream, format='PNG')
        self.png = stream.getvalue()
        self.image = self.root / 'input.png'
        self.image.write_bytes(self.png)
        self.metadata = self.root / 'captcha-ctc.json'
        metadata = json.loads((self.product / 'skills/autoskill/models/captcha/captcha-ctc.json').read_text())
        self.metadata.write_text(json.dumps(metadata))
        self.charset = metadata['charset']
        self.frames = np.zeros((1, 1, len(self.charset)), np.float32)
        self.frames[0, 0, self.charset.index('a')] = 1
        self.row = {'id': 'three-layer-input', 'file': self.image.name, 'png_sha256': evaluate_color_layers.sha(self.image),
                    'split': 'dev', 'category': 'through-line', 'normal': False, 'label': 'a'}
        self.protocol = {'base_metadata': str(self.metadata), 'base_model_folder': str(self.root),
                         'data_root': str(self.root), 'base_sha256': 'session-double-no-model-loaded',
                         'gate_numpy_helper': 'unused-frozen-gate'}
        self.gate = SimpleNamespace(physical_signal=diagnose_physical_line_v41.physical_signal)

    def test_historical_tensor_keeps_the_line_that_current_solver_removes(self):
        original, current = Session(self.frames), Session(self.frames)
        frozen = load_solver(self.product)
        with patch.object(frozen, '_model', return_value=(original, self.charset, 48, 320)):
            self.assertEqual(frozen.solve(self.png), 'a')
        with patch.object(self.current, '_model', return_value=(current, self.charset, 48, 320)):
            self.assertEqual(self.current.solve(self.png), 'a')
        np.testing.assert_array_equal(original.inputs[0], prepare(self.png, 48, 320))
        self.assertTrue(self.gate.physical_signal(original.inputs[0])['flag'])
        self.assertFalse(self.gate.physical_signal(current.inputs[0])['flag'])

    def test_loading_isolated_snapshot_preserves_existing_product_state(self):
        loaded, folder = self.current._loaded, self.current.FOLDER
        before = set(sys.modules)
        first, second = load_solver(self.product), load_solver(self.product)
        first._loaded = object()
        first.FOLDER = self.root
        self.assertIsNone(second._loaded)
        self.assertEqual(second.FOLDER, self.product / 'skills/autoskill/models/captcha')
        self.assertIs(self.current._loaded, loaded)
        self.assertEqual(self.current.FOLDER, folder)
        self.assertFalse(any(name.startswith('_autoskill_historical_') for name in set(sys.modules) - before))
        self.assertEqual(second.research_provenance['source_sha256'], SOURCE_SHA256)
        self.assertFalse(second.research_provenance['current_product_solver_imported'])

    def test_changed_snapshot_is_rejected_before_execution(self):
        with patch.object(FrozenSourceLoader, 'get_data', return_value=b'changed historical source'), \
                self.assertRaisesRegex(ImportError, 'frozen pre-recovery solver source changed'):
            load_solver(self.product)

    def test_empty_historical_prediction_still_has_one_session_call(self):
        frames = np.zeros_like(self.frames)
        frames[0, 0, 0] = 1
        session = Session(frames)
        frozen = load_solver(self.product)
        with patch.object(frozen, '_model', return_value=(session, self.charset, 48, 320)), \
                self.assertRaisesRegex(ValueError, '没有从验证码图片里识别出字符'):
            frozen.solve(self.png)
        self.assertEqual(len(session.inputs), 1)

    def test_color_evaluation_finishes_with_the_original_frozen_flag(self):
        session = Session(self.frames)
        with (patch.object(evaluate_color_layers, 'load_gate', return_value=self.gate),
              patch.object(evaluate_color_layers.ort, 'InferenceSession', return_value=session)):
            records, runtime = evaluate_color_layers.run_product(
                self.product, self.protocol, [self.row], self.frames, [{'flag': True}], self.root)
        self.assertTrue(records[0]['flag'])
        self.assertTrue(records[0]['restore'])
        self.assertEqual(records[0]['candidate'], 'a')
        self.assertEqual(len(session.inputs), 1)
        self.assertEqual(runtime['historical_solver']['source_sha256'], SOURCE_SHA256)

    def test_png_specialist_receives_the_frozen_gate_input(self):
        source_charset = [*self.charset, *(chr(0x4E00 + index) for index in range(8147))]
        source_metadata = self.root / 'source-charset.json'
        source_metadata.write_text(json.dumps(source_charset))
        self.protocol['source_charset'] = str(source_metadata)
        self.protocol['source_sha256'] = 'session-double-no-model-loaded'
        raw = np.zeros((1, 1, len(source_charset)), np.float32)
        raw[0, 0, source_charset.index('a')] = 1
        session = Session(raw)
        with (patch.object(evaluate_png_specialist, 'source_session', return_value=session),
              patch.object(evaluate_png_specialist, 'load_helper', return_value=self.gate)):
            records, runtime = evaluate_png_specialist.run(
                self.product, self.protocol, [self.row], self.frames, [{'flag': True}], self.root)
        self.assertTrue(records[0]['flag'])
        self.assertEqual((records[0]['A'], records[0]['B']), ('a', 'a'))
        self.assertEqual(len(session.inputs), 1)
        self.assertEqual(runtime['source_ORT_calls'], 1)
        self.assertEqual(runtime['historical_solver']['source_sha256'], SOURCE_SHA256)

    def test_conditional_specialist_stays_on_the_original_flagged_branch(self):
        candidate = self.root / 'candidate.onnx'
        candidate.write_bytes(b'session double; not an ONNX model')
        session = Session(self.frames)
        with (patch.object(evaluate_conditional_expert, 'load_helper', return_value=self.gate),
              patch.object(evaluate_conditional_expert.ort, 'InferenceSession', return_value=session)):
            records, numeric, runtime = evaluate_conditional_expert.run_product(
                self.product, candidate, self.protocol, [self.row], self.frames, [{'flag': True}], self.root)
        self.assertTrue(records[0]['flag'])
        self.assertEqual(numeric['expert_onehot_images'], 1)
        np.testing.assert_array_equal(session.inputs[0], prepare(self.png, 48, 320))
        self.assertEqual(runtime['historical_solver']['source_sha256'], SOURCE_SHA256)

    def test_both_physical_diagnostics_preserve_the_historical_flags(self):
        manifest = self.root / 'manifest.jsonl'
        manifest.write_text(json.dumps(self.row) + '\n')
        cache = self.root / 'cache'
        cache.mkdir()
        (cache / 'features.npz').write_bytes(b'cached logits supplied by the test')
        metadata = json.loads(self.metadata.read_text())
        for diagnostic in (diagnose_physical_line, diagnose_physical_line_v41):
            with self.subTest(version=diagnostic.__name__):
                signal = {'coverage_minimum': .90, 'max_rgb_exclusive': 128, 'max_rgb_spread_inclusive': 16}
                if diagnostic is diagnose_physical_line_v41:
                    signal['max_dark_gray_pixel_fraction_inclusive'] = .50
                output = self.root / diagnostic.__name__
                with patch.object(diagnostic, 'load_inputs', return_value=([self.row], self.frames, metadata)):
                    diagnostic.diagnostic(self.root, manifest, cache, self.product, {'fixed_signal': signal}, output)
                observation = json.loads((output / 'observations.jsonl').read_text())
                lock = json.loads((output / 'run-lock.json').read_text())
                self.assertTrue(observation['flag'])
                self.assertEqual(lock['product_solver_sha256'], SOURCE_SHA256)
                self.assertEqual(lock['historical_solver']['source_sha256'], SOURCE_SHA256)


if __name__ == '__main__':
    unittest.main()
