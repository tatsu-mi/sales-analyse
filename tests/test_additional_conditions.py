import json
from pathlib import Path
import tempfile
import unittest

from sales_analyze import additional_conditions as conditions
from sales_analyze import engineer_matching


class AdditionalConditionsTests(unittest.TestCase):
    def test_parse_keeps_continuation_and_nested_bullets(self):
        self.assertEqual(conditions.parse('<!-- 案内 -->'), [])
        self.assertEqual(conditions.parse('- 初級者向け\n  運用監視を担当\n  - 電話対応\n- 月50万円以下'),
                         ['初級者向け\n  運用監視を担当\n  - 電話対応', '月50万円以下'])

    def test_all_conditions_must_be_confirmed(self):
        for state in ('match', 'unknown', 'mismatch'):
            record = dict(checks=[dict(criterion=1, state='match', reason='確認済み'),
                                  dict(criterion=2, state=state, reason='判定理由')])
            check = dict(state='match', evidence='スキル確認済み')
            self.assertEqual(conditions.apply(check, record, ['初級者向け', '月50万円以下']), state == 'match')
            self.assertEqual(check['state'], state)
            self.assertEqual(len(check['additional_conditions']), 2)

    def test_validation_rejects_missing_duplicate_and_invalid_checks(self):
        items = [dict(id='1', conditions=['初級者向け', '月50万円以下'])]
        valid = [dict(criterion=i, state='match', reason='確認済み') for i in (1, 2)]
        self.assertEqual(conditions.validate(json.dumps({'records': [dict(id='1', checks=valid)]}), items)[0]['checks'], valid)
        for checks in (valid[:1], [valid[0], valid[0]], [dict(criterion=1, state='yes', reason='理由'), valid[1]]):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                conditions.validate(json.dumps({'records': [dict(id='1', checks=checks)]}), items)

    def test_new_heading_is_separate_from_career_evidence_and_people_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'engineers.md'
            path.write_text('## A\nPython実務3年\n### 追加条件\n- 初級者向け案件のみ\n\n'
                            '## B\nJava実務2年\n### 追加条件\n- 月50万円以下\n', encoding='utf-8')
            a, b = engineer_matching.load_profiles(path)
            self.assertEqual(a['additional_conditions'], '- 初級者向け案件のみ')
            self.assertNotIn('初級者', a['text'])
            self.assertEqual(b['additional_conditions'], '- 月50万円以下')
            self.assertNotIn('月50万円', b['text'])
            self.assertNotIn('月50万円', json.dumps(a, ensure_ascii=False))
            with path.open('a', encoding='utf-8') as file:
                file.write('\n### 追加条件\n- 重複\n')
            with self.assertRaisesRegex(ValueError, '重複'):
                engineer_matching.load_profiles(path)

