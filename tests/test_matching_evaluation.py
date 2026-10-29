import unittest

from sales_analyze.evaluate_matching import evaluate


class EvaluationTests(unittest.TestCase):
    def records(self, states, key):
        return [dict(engineer='A', uid=str(i), project_key='0', **{key: state})
                for i, state in enumerate(states)]

    def test_precision_recall_and_abstention_do_not_hide_false_recommendations(self):
        predictions = self.records(['match', 'match', 'unknown', 'mismatch'], 'state')
        labels = self.records(['match', 'mismatch', 'match', 'mismatch'], 'expected')
        result = evaluate(predictions, labels)
        self.assertEqual(result['match_precision'], 0.5)
        self.assertEqual(result['match_recall'], 0.5)
        self.assertEqual(result['false_recommendations'], 1)
        self.assertEqual(result['unknown_rate'], 0.25)
        self.assertEqual(len(result['disagreements']), 2)

    def test_undefined_metrics_are_null_and_unlabeled_cases_are_counted(self):
        predictions = self.records(['unknown', 'mismatch'], 'state')
        result = evaluate(predictions, self.records(['mismatch'], 'expected'))
        self.assertIsNone(result['match_precision'])
        self.assertIsNone(result['match_recall'])
        self.assertEqual(result['unlabeled_count'], 1)

    def test_missing_duplicate_and_empty_cases_are_rejected(self):
        predictions = self.records(['match'], 'state')
        labels = self.records(['match'], 'expected')
        for records, gold in ((predictions, []), ([], labels), (predictions * 2, labels),
                              (predictions, labels * 2), (predictions, [dict(labels[0], uid='other')])):
            with self.subTest(records=records, labels=gold), self.assertRaises(ValueError):
                evaluate(records, gold)
