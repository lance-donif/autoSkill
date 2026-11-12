"""Verify ONNX condition bools exactly match fixed NumPy physical semantics."""
import unittest

import numpy as np
import onnxruntime as ort
from diagnose_physical_line_v41 import physical_signal
from physical_onnx_v41 import condition_examples, condition_model


class PhysicalOnnxTests(unittest.TestCase):
    """Use only the small condition graph, never the OCR model or training."""

    @classmethod
    def setUpClass(cls):
        options = ort.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
        cls.session = ort.InferenceSession(condition_model().SerializeToString(), options, providers=['CPUExecutionProvider'])

    def test_boundaries_numpy_and_onnx(self):
        for name, tensor, expected in condition_examples():
            with self.subTest(name=name):
                self.assertEqual(physical_signal(tensor)['flag'], expected)
                flags, mask = self.session.run(None, {'image': tensor})
                self.assertEqual(bool(flags[0]), expected)
                np.testing.assert_array_equal(mask, np.full((1, 1, 1), float(expected), np.float32))

    def test_batch_flags_match_individual_inputs(self):
        examples = condition_examples()
        batch = np.concatenate([tensor for _, tensor, _ in examples])
        flags, mask = self.session.run(None, {'image': batch})
        np.testing.assert_array_equal(flags, [expected for _, _, expected in examples])
        np.testing.assert_array_equal(mask[:, 0, 0], flags.astype(np.float32))


if __name__ == '__main__':
    unittest.main()
