import copy
import json
import tempfile
import unittest
from pathlib import Path

from sales_analyze import engineer_matching as matching
from sales_analyze import prioritization


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_people_are_isolated_and_hash_changes_only_for_affected_person(self):
        path = self.root / 'engineers.md'
        text = '# 一覧\n共通注意事項\n\n## 1. A｜元資料.pdf\nPython 3年\n### 工程\n基本設計\n\n## 2. B\nJava 2年\n'
        path.write_text(text, encoding='utf-8-sig')
        profiles = matching.load_profiles(path)
        self.assertEqual([p['file'] for p in profiles], ['A', 'B'])
        self.assertIn('共通注意事項', profiles[0]['text'])
        self.assertIn('基本設計', profiles[0]['text'])
        self.assertNotIn('Java', profiles[0]['text'])
        path.write_text(text.replace('Java 2年', 'Java 3年'), encoding='utf-8')
        updated = matching.load_profiles(path)
        self.assertEqual(profiles[0]['sha256'], updated[0]['sha256'])
        self.assertNotEqual(profiles[1]['sha256'], updated[1]['sha256'])
        path.write_text(text.replace('共通注意事項', '共通注意変更'), encoding='utf-8')
        self.assertNotEqual(profiles[0]['sha256'], matching.load_profiles(path)[0]['sha256'])

    def test_numberless_profiles_keep_identity_when_people_are_added_removed_or_reordered(self):
        path = self.root / 'engineers.md'
        preamble = '# 一覧\n共通注意事項\n\n'
        a = '## A｜元資料.pdf\nPython 3年\n\n'
        b = '## B\nJava 2年\n\n'
        c = '## C\nGo 4年\n\n'
        path.write_text(preamble + a.replace('## A', '## 1. A')
                        + b.replace('## B', '## 2. B'), encoding='utf-8')
        original = {p['name']: p for p in matching.load_profiles(path)}
        for sections, expected in ((a + b, ['A', 'B']),
                                   (c + b + a, ['C', 'B', 'A']),
                                   (b, ['B'])):
            with self.subTest(expected=expected):
                path.write_text(preamble + sections, encoding='utf-8')
                profiles = matching.load_profiles(path)
                self.assertEqual([p['name'] for p in profiles], expected)
                for profile in profiles:
                    if profile['name'] in original:
                        self.assertEqual(profile, original[profile['name']])

    def test_missing_empty_duplicate_and_invalid_profiles(self):
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            matching.load_profiles(self.root / 'missing')
        path = self.root / 'engineers.md'
        for text in ('', '# 一覧のみ', '## A\n', '## A\nPython\n## A\nJava'):
            path.write_text(text, encoding='utf-8')
            with self.subTest(text=text), self.assertRaises(ValueError):
                matching.load_profiles(path)

    def test_many_profiles_have_distinct_people(self):
        path = self.root / 'engineers.md'
        path.write_text('\n'.join(f'## Person {i}\nPython実務3年\n' for i in range(16)),
                        encoding='utf-8')
        profiles = matching.load_profiles(path)
        self.assertEqual(len(profiles), 16)
        self.assertEqual(len({p['file'] for p in profiles}), 16)

    def test_feedback_is_personal_separate_from_skill_evidence_and_invalidates_own_cache(self):
        path = self.root / 'engineers.md'
        text = ('## A\nPython実務4年\n\n### マッチングフィードバック\n'
                '- 案件X：不適合。リーダー経験不足。\n\n### 資格\n基本情報\n\n'
                '## B\nJava実務2年\n\n### マッチングフィードバック\n<!-- 記入案内 -->\n')
        path.write_text(text, encoding='utf-8')
        first, second = matching.load_profiles(path)
        self.assertIn('案件X', first['feedback'])
        self.assertNotIn('案件X', first['text'])
        self.assertIn('基本情報', first['text'])
        self.assertEqual(second['feedback'], '')
        self.assertNotIn('案件X', json.dumps(second, ensure_ascii=False))
        path.write_text(text.replace('リーダー経験不足', '年数不足'), encoding='utf-8')
        updated = matching.load_profiles(path)
        self.assertNotEqual(first['sha256'], updated[0]['sha256'])
        self.assertEqual(first['text'], updated[0]['text'])
        self.assertEqual(second['sha256'], updated[1]['sha256'])

    def test_feedback_cannot_be_used_as_experience_evidence(self):
        path = self.root / 'engineers.md'
        path.write_text('## A\nJava実務1年\n\n### マッチングフィードバック\n'
                        '以前Python実務4年、基本設計2年と誤判定された。', encoding='utf-8')
        profile = matching.load_profiles(path)[0]
        self.assertNotIn('Python', profile['text'])
        self.assertIn('Python', profile['feedback'])

    def test_empty_feedback_and_duplicate_sections(self):
        path = self.root / 'engineers.md'
        path.write_text('## A\nPython\n\n### マッチングフィードバック', encoding='utf-8')
        profile = matching.load_profiles(path)[0]
        self.assertEqual(profile['feedback'], '')
        self.assertNotIn('フィードバック', profile['text'])
        with path.open('a', encoding='utf-8') as file:
            file.write('\n\n### マッチングフィードバック\n追記')
        with self.assertRaisesRegex(ValueError, '重複'):
            matching.load_profiles(path)


    def test_confirmed_engineer_matches_outrank_unknown_and_keep_confirmation_action(self):
        rules = prioritization.load_rules('priority_rules.example.md')
        from test_prioritization import PriorityTests
        factory = PriorityTests()
        factory.rules = rules
        unknown = factory.candidate(1)
        unknown['engineer_match'] = dict(state='unknown', evidence='経験年数が不明')
        matched = copy.deepcopy(unknown)
        matched.update(uid='2', engineer_match=dict(state='match', evidence='案件とPDFの両方にPython'))
        matched['latest']['assessments'][0]['state'] = 'unknown'
        ranked = prioritization.shortlist([unknown, matched], rules)
        self.assertEqual([p['uid'] for p in ranked['ses']], ['2', '1'])
        self.assertEqual(ranked['ses'][1]['verification'], '要確認')
        report = prioritization.render_report(ranked, rules)
        self.assertIn('経験年数が不明', report)
        self.assertIn('未確認の案件条件を問い合わせる', report)


if __name__ == '__main__':
    unittest.main()
