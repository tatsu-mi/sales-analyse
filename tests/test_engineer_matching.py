import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import engineer_matching as matching
import prioritization


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_recursive_pdfs_are_distinguished_by_relative_path_and_hash(self):
        (self.root / 'sub').mkdir()
        (self.root / 'A.PDF').write_bytes(b'one')
        (self.root / 'sub' / 'A.pdf').write_bytes(b'two')
        (self.root / 'ignore.txt').write_text('ignore')
        reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: 'Python 3年'),
                                       SimpleNamespace(extract_text=lambda: '基本設計')])
        with patch('pypdf.PdfReader', return_value=reader):
            profiles = matching.load_profiles(self.root)
        self.assertEqual([p['file'] for p in profiles], ['A.PDF', 'sub/A.pdf'])
        self.assertNotEqual(profiles[0]['sha256'], profiles[1]['sha256'])
        self.assertEqual(profiles[0]['text'], 'Python 3年\n\n基本設計')

    def test_missing_empty_corrupt_and_scanned_pdfs(self):
        with self.assertRaisesRegex(ValueError, 'フォルダがありません'):
            matching.load_profiles(self.root / 'missing')
        self.assertEqual(matching.load_profiles(self.root), [])
        (self.root / 'broken.pdf').write_bytes(b'not a pdf')
        with self.assertRaisesRegex(ValueError, 'broken.pdf'):
            matching.load_profiles(self.root)
        reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: '')])
        with patch('pypdf.PdfReader', return_value=reader), self.assertRaisesRegex(ValueError, 'OCR'):
            matching.load_profiles(self.root)

    def test_result_requires_exact_coverage_and_evidence(self):
        items = [{'id': '0'}, {'id': '1'}]
        valid = [{'id': '0', 'state': 'match', 'evidence': '案件とPDFの根拠'},
                 {'id': '1', 'state': 'unknown', 'evidence': '年数が不明'}]
        for record in valid:
            record['commute'] = dict(mode='full_remote', nearest_station='', workplace='', minutes=-1,
                                     route='', evidence='出社不要のフルリモート')
        self.assertEqual(matching.validate(json.dumps({'records': valid}), items), valid)
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
        for change in ({'minutes': True}, {'minutes': '90'}, {'minutes': -2},
                       {'mode': 'remote'}, {'evidence': ''}, {'nearest_station': None}):
            record = dict(base, commute=dict(base['commute'], **change))
            with self.subTest(change=change), self.assertRaises(ValueError):
                matching.validate(json.dumps({'records': [record]}), [{'id': '0'}])

    def test_confirmed_engineer_matches_outrank_unknown_and_keep_confirmation_action(self):
        rules = prioritization.load_rules('priority_rules.toml')
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
