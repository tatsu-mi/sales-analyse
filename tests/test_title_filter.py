import copy
import json
import unittest

import prioritization
import title_filter


class TitleFilterTests(unittest.TestCase):
    def setUp(self):
        self.settings = prioritization.load_rules('priority_rules.toml')['prefilter']

    def test_mechanical_rules(self):
        excluded = ['【人材紹介】Pythonエンジニア', '弊社社員紹介', '社員の紹介', 'エンジニアのご紹介', '新年のご挨拶',
                    '自社製品のご紹介', '弊社SaaSのご案内', 'テスト専任', '運用専任募集',
                    'ホームページ制作専業', 'ウェビナーのご案内']
        for subject in excluded:
            with self.subTest(subject=subject):
                self.assertTrue(title_filter.mechanical_exclude(subject, self.settings))

    def test_possible_development_and_unknown_titles_survive(self):
        kept = ['', '案件情報', 'Java・Python案件', 'テストも含むJava開発', '運用システムの改修',
                'HP制作と業務システム開発相談', 'ご挨拶とJava要員募集',
                '社員紹介およびPython案件募集', '自社製品開発のご相談', 'C++ 100万円']
        for subject in kept:
            with self.subTest(subject=subject):
                self.assertFalse(title_filter.mechanical_exclude(subject, self.settings))

    def test_normalizes_fullwidth_text(self):
        self.assertTrue(title_filter.mechanical_exclude('弊社ＳａａＳのご紹介', self.settings))
        self.assertFalse(title_filter.mechanical_exclude('Ｊａｖａ開発相談', self.settings))

    def test_bad_settings_are_rejected(self):
        for key, value in [('enabled', 'yes'), ('ai_enabled', 1), ('batch_size', 0),
                           ('batch_size', 101), ('exclude_patterns', ['[']),
                           ('keep_patterns', 'word'), ('ai_instructions', [])]:
            settings = copy.deepcopy(self.settings)
            settings[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                title_filter.validate_settings(settings)

    def test_ai_must_cover_uids_once_and_use_boolean_decisions(self):
        items = [{'uid': '1'}, {'uid': '2'}]
        record = {'uid': '1', 'exclude': True}
        for records in [[], [record], [record, record], [record, {'uid': '3', 'exclude': False}],
                        [record, {'uid': '2', 'exclude': 'false'}], [record, {'uid': '2'}]]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                title_filter.validate_answer(json.dumps({'records': records}), items)
        records = [record, {'uid': '2', 'exclude': False}]
        self.assertEqual(title_filter.validate_answer(json.dumps({'records': records}), items), records)


if __name__ == '__main__':
    unittest.main()
