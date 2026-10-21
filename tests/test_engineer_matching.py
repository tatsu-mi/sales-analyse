import copy
import json
import tempfile
import unittest
from pathlib import Path

import engineer_matching as matching
import prioritization


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
        item, record = self.assessment()
        self.assertEqual(self.checked_state(item, record, profile['text']), 'unknown')

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

    def test_result_requires_exact_coverage_and_evidence(self):
        items = [{'id': '0'}, {'id': '1'}]
        valid = [{'id': '0', 'state': 'match', 'evidence': '案件とPDFの根拠'},
                 {'id': '1', 'state': 'unknown', 'evidence': '年数が不明'}]
        for record in valid:
            record['requirements'] = []
            record['unassessed_requirements'] = []
            record['commute'] = dict(mode='full_remote', nearest_station='', workplace='', minutes=-1,
                                     route='', evidence='出社不要のフルリモート')
        result = matching.validate(json.dumps({'records': valid}), items)
        self.assertTrue(all(record['state'] == 'unknown' for record in result))
        for records in (valid[:1], [valid[0], valid[0]], [valid[0], dict(valid[1], id='9')],
                        [valid[0], dict(valid[1], evidence='')],
                        [valid[0], dict(valid[1], state='yes')]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                matching.validate(json.dumps({'records': records}), items)

    def test_commute_boundary_remote_and_missing_information(self):
        base = dict(mode='onsite', nearest_station='仙台駅', workplace='勤務地の駅',
                    route='電車と徒歩', minutes=90, evidence='資料に記載の経路と所要時間')
        for change, expected in (({}, 'match'), ({'minutes': 89}, 'match'),
                                 ({'minutes': 91}, 'mismatch'), ({'minutes': -1}, 'unknown'),
                                 ({'nearest_station': ''}, 'unknown'), ({'workplace': ''}, 'unknown'),
                                 ({'route': ''}, 'unknown'), ({'mode': 'unknown'}, 'unknown'),
                                 ({'mode': 'hybrid', 'minutes': 91}, 'mismatch'),
                                 ({'mode': 'hybrid'}, 'match'),
                                 ({'mode': 'full_remote', 'nearest_station': '', 'minutes': -1}, 'match')):
            with self.subTest(change=change):
                self.assertEqual(matching.commute_state(dict(base, **change)), expected)

    def test_invalid_commute_result_cannot_be_used(self):
        base = dict(id='0', state='match', evidence='根拠', commute=dict(
            mode='onsite', nearest_station='駅', workplace='就業先', minutes=90, route='経路', evidence='根拠'))
        base['requirements'] = []
        base['unassessed_requirements'] = []
        for change in ({'minutes': True}, {'minutes': '90'}, {'minutes': -2},
                       {'mode': 'remote'}, {'evidence': ''}, {'nearest_station': None}):
            record = dict(base, commute=dict(base['commute'], **change))
            with self.subTest(change=change), self.assertRaises(ValueError):
                matching.validate(json.dumps({'records': [record]}), [{'id': '0'}])

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

    def assessment(self):
        requirements = [dict(requirement='Python実務3年以上', source_quote='Python実務3年以上'),
                        dict(requirement='基本設計', source_quote='基本設計必須')]
        item = dict(id='0', project=dict(mandatory_requirements=requirements))
        record = dict(id='0', state='match', evidence='両条件を照合', unassessed_requirements=[],
                      requirements=[dict(criterion=1, state='match', project_quote='Python実務3年以上',
                                         engineer_quote='Python実務4年', reason='4年で要件充足'),
                                    dict(criterion=2, state='match', project_quote='基本設計必須',
                                         engineer_quote='基本設計2年', reason='工程経験あり')],
                      commute=dict(mode='full_remote', nearest_station='', workplace='', minutes=-1,
                                   route='', evidence='出社不要'))
        return item, record

    def checked_state(self, item, record, profile='Python実務4年、基本設計2年'):
        return matching.validate(json.dumps(dict(records=[record])), [item], profile)[0]['state']

    def test_all_requirements_must_be_supported_and_overall_ai_state_is_not_trusted(self):
        item, record = self.assessment()
        self.assertEqual(self.checked_state(item, record), 'match')
        record['requirements'][1].update(state='unknown', engineer_quote='', reason='工程記載なし')
        self.assertEqual(self.checked_state(item, record), 'unknown')
        record['requirements'][0].update(state='mismatch', reason='年数不足')
        self.assertEqual(self.checked_state(item, record), 'mismatch')

    def test_fabricated_quotes_and_other_engineers_evidence_cannot_confirm_match(self):
        for field in ('project_quote', 'engineer_quote'):
            item, record = self.assessment()
            record['requirements'][0][field] = '存在しない経験'
            self.assertEqual(self.checked_state(item, record), 'unknown')
        item, record = self.assessment()
        self.assertEqual(self.checked_state(item, record, 'Java実務5年'), 'unknown')

    def test_missing_duplicate_and_out_of_order_requirement_checks_are_rejected(self):
        for numbers in ([1], [1, 1], [2, 1], [True, 2]):
            item, record = self.assessment()
            record['requirements'] = [dict(record['requirements'][min(i, 1)], criterion=number)
                                      for i, number in enumerate(numbers)]
            with self.subTest(numbers=numbers), self.assertRaises(ValueError):
                self.checked_state(item, record)

    def test_omitted_requirements_or_truncated_mail_prevent_confirmation(self):
        item, record = self.assessment()
        record['unassessed_requirements'] = ['リーダー経験必須']
        self.assertEqual(self.checked_state(item, record), 'unknown')
        record['unassessed_requirements'] = []
        item['source_truncated'] = True
        self.assertEqual(self.checked_state(item, record), 'unknown')

    def test_wrapped_quotes_are_accepted_but_paraphrases_are_not(self):
        self.assertTrue(matching.contains_quote('Python 実務\n4年', 'Python実務4年'))
        self.assertFalse(matching.contains_quote('', 'Python実務4年'))
        self.assertFalse(matching.contains_quote('Python経験4年', 'Python実務4年'))


if __name__ == '__main__':
    unittest.main()
