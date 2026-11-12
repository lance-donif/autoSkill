"""Exercise conditional dev selection gates, including aggregate-metric traps."""
import unittest

from select_physical_residual import assess
from train_residual_ctc import measure


class ConditionalSelectionTests(unittest.TestCase):
    """Reject new protected errors even when aggregate exact counts are unchanged."""

    def setUp(self):
        self.rows, self.original = [], []
        for category in ['plain', 'ordinary-aug', 'paired-clean', 'pale', 'through-line',
                         'dark-through-line', 'first-symbol-touching', 'dark-first-symbol-touching']:
            for index, label in enumerate('abcd'):
                self.rows.append({'id': category + str(index), 'category': category, 'label': label,
                                  'normal': category in ['plain', 'ordinary-aug', 'paired-clean']})
                self.original.append(label if index == 0 else 'x')
        self.baseline = measure(self.original, self.rows)
        self.policy = {'line_noise_groups': ['through-line', 'dark-through-line', 'first-symbol-touching', 'dark-first-symbol-touching'],
                       'minimum_pale_first_symbol_exact_gain': 2, 'minimum_line_noise_groups_with_exact_gain': 2}

    def acceptable(self):
        predictions = list(self.original)
        for index, row in enumerate(self.rows):
            if ((row['category'] == 'first-symbol-touching' and row['label'] in 'bc')
                    or (row['category'] == 'through-line' and row['label'] == 'b')):
                predictions[index] = row['label']
        return predictions

    def judged(self, predictions):
        return assess(predictions, self.original, self.rows, self.baseline, self.policy)

    def test_predeclared_first_and_two_noise_group_gains(self):
        judged = self.judged(self.acceptable())
        self.assertTrue(judged['eligible'])
        self.assertEqual(judged['pale_first_symbol_exact_gain'], 2)
        predictions = self.acceptable()
        index = next(i for i, row in enumerate(self.rows) if row['category'] == 'first-symbol-touching' and row['label'] == 'c')
        predictions[index] = 'x'
        self.assertFalse(self.judged(predictions)['eligible'])

    def test_normal_error_swap_does_not_hide_a_new_protected_error(self):
        predictions = self.acceptable()
        predictions[0], predictions[1] = 'x', 'b'
        judged = self.judged(predictions)
        self.assertEqual(judged['groups']['plain']['exact'], self.baseline['plain']['exact'])
        self.assertEqual(judged['normal_new_wrong_count'], 1)
        self.assertFalse(judged['eligible'])

    def test_a_single_line_group_regression_rejects_other_gains(self):
        predictions = self.acceptable()
        index = next(i for i, row in enumerate(self.rows) if row['category'] == 'dark-through-line' and row['label'] == 'a')
        predictions[index] = 'x'
        judged = self.judged(predictions)
        self.assertGreater(judged['total_noise_exact_gain'], 0)
        self.assertEqual(judged['line_noise_exact_gains']['dark-through-line'], -1)
        self.assertFalse(judged['eligible'])


if __name__ == '__main__':
    unittest.main()
