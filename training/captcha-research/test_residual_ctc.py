"""Tiny exhaustive-path and finite-difference checks; no OCR model or corpus."""
import unittest
from itertools import product

import numpy as np
from residual_ctc import Adam, TemporalResidual, context_window, ctc_loss_and_grad, log_softmax, teacher_kl_and_grad


def collapse(path):
    """CTC collapse drops blank and adjacent repeats, not separated repeats."""
    output, previous = [], -1
    for value in path:
        if value and value != previous:
            output.append(value)
        previous = value
    return output


def exhaustive_loss(logits, target):
    """Reference marginal likelihood over every class path in a tiny example."""
    probabilities = np.exp(log_softmax(logits))
    total = 0.
    for path in product(range(logits.shape[1]), repeat=logits.shape[0]):
        if collapse(path) == list(target):
            total += np.prod([probabilities[frame, value] for frame, value in enumerate(path)])
    return -np.log(total) / max(1, len(target))


class CTCMarginalTests(unittest.TestCase):
    def test_all_paths_include_repeated_characters_and_blank_separation(self):
        random = np.random.default_rng(101)
        logits = random.normal(size=(1, 4, 3))
        for label in ([], [1], [1, 1], [1, 2], [2, 1]):
            with self.subTest(label=label):
                loss, gradient, details = ctc_loss_and_grad(logits, [label])
                self.assertAlmostEqual(loss, exhaustive_loss(logits[0], label), places=11)
                np.testing.assert_allclose(details['posterior'].sum(-1), 1, atol=1e-11)
                np.testing.assert_allclose(gradient.sum(-1), 0, atol=1e-11)

    def test_analytic_logit_gradient_matches_central_finite_difference(self):
        random = np.random.default_rng(102)
        logits = random.normal(size=(2, 4, 3))
        labels, weights = [[1, 1], [1, 2]], np.asarray([1., 2.])
        _, gradient, _ = ctc_loss_and_grad(logits, labels, sample_weights=weights)
        numeric = np.zeros_like(logits)
        epsilon = 1e-5
        for position in np.ndindex(logits.shape):
            plus, minus = logits.copy(), logits.copy()
            plus[position] += epsilon
            minus[position] -= epsilon
            numeric[position] = (ctc_loss_and_grad(plus, labels, sample_weights=weights)[0] -
                                 ctc_loss_and_grad(minus, labels, sample_weights=weights)[0]) / (2 * epsilon)
        np.testing.assert_allclose(gradient, numeric, rtol=2e-6, atol=2e-8)

    def test_impossible_repeated_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Not enough'):
            ctc_loss_and_grad(np.zeros((1, 2, 3)), [[1, 1]])


class ResidualTests(unittest.TestCase):
    def test_zero_output_projection_is_exactly_the_baseline(self):
        random = np.random.default_rng(201)
        features = random.normal(size=(2, 5, 3)).astype(np.float32)
        baseline = random.normal(size=(2, 5, 4)).astype(np.float32)
        head = TemporalResidual.initialize(dimensions=3, hidden=4, classes=4)
        actual, cache = head.forward(features, baseline)
        np.testing.assert_array_equal(actual, baseline)
        np.testing.assert_array_equal(cache['residual'], np.zeros_like(baseline))
        np.testing.assert_array_equal(context_window(features)[:, 0, :3], features[:, 0])
        np.testing.assert_array_equal(context_window(features)[:, -1, -3:], features[:, -1])

    def test_adapter_backward_matches_finite_difference(self):
        random = np.random.default_rng(202)
        head = TemporalResidual(random.normal(0, .1, (9, 4)), np.zeros(4), random.normal(0, .1, (4, 3)), np.zeros(3))
        features, baseline = random.normal(size=(1, 4, 3)), random.normal(size=(1, 4, 3))
        corrected, cache = head.forward(features, baseline)
        _, gradient, _ = ctc_loss_and_grad(corrected, [[1, 2]])
        derivatives = head.backward(gradient, cache)
        epsilon = 1e-5
        for name, parameter in head.parameters().items():
            for position in np.ndindex(parameter.shape):
                original = parameter[position]
                parameter[position] = original + epsilon
                plus = ctc_loss_and_grad(head.forward(features, baseline)[0], [[1, 2]])[0]
                parameter[position] = original - epsilon
                minus = ctc_loss_and_grad(head.forward(features, baseline)[0], [[1, 2]])[0]
                parameter[position] = original
                self.assertAlmostEqual(derivatives[name][position], (plus - minus) / (2 * epsilon), places=7)

    def test_teacher_consistency_zero_and_gradient(self):
        random = np.random.default_rng(203)
        baseline = random.normal(size=(2, 4, 3))
        loss, gradient = teacher_kl_and_grad(baseline, baseline, [1., 0.])
        self.assertEqual(0., loss)
        np.testing.assert_array_equal(gradient, np.zeros_like(gradient))
        corrected = baseline + random.normal(0, .1, baseline.shape)
        _, gradient = teacher_kl_and_grad(corrected, baseline, [1., 0.])
        np.testing.assert_array_equal(gradient[1], np.zeros_like(gradient[1]))

    def test_adam_updates_live_arrays_and_clips_gradients(self):
        parameter = np.zeros((2,), dtype=np.float64)
        optimizer = Adam({'value': parameter}, clip_norm=.5)
        report = optimizer.step({'value': np.asarray([3., 4.])})
        self.assertAlmostEqual(.1, report['gradient_clip_scale'])
        self.assertTrue(np.all(parameter < 0))
        self.assertIs(parameter, optimizer.parameters['value'])


if __name__ == '__main__':
    unittest.main()
