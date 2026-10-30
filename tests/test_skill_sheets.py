import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sales_analyze import mail_analyzer as app
from sales_analyze import skill_sheets as sheets
from sales_analyze import engineer_matching


def record(name='A', station='国分寺駅'):
    return dict(name=name, id='sheet', complete=True, unassessed_information=[], routes=[],
                attributes=[dict(field='nearest_station', value=station)],
                facts=[dict(terms=['Python', '詳細設計'], event_id='project1',
                            experience='professional', polarity='present', months=12,
                            duration_kind='period', start='2025-01', end='2025-12')])


class SkillSheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.old = (sheets.render_sheet(record(), 'A.pdf')
                    .replace('<!-- 必ず満たす条件を箇条書きで記載してください。 -->', '- 高単価は避ける'))
        self.store = Mock()
        self.store.request.return_value = self.old.encode()
        self.settings = dict(max_output_tokens=1000, requests_per_minute=5, input_tokens_per_minute=10000)
        self.real_analyse = sheets.analyse
        self.real_list_pdfs = sheets.list_pdfs
        for name, value in [('list_pdfs', [{'name': 'A.pdf'}, {'name': 'B.PDF'}]),
                            ('analyse', record('B'))]:
            mock = patch.object(sheets, name, return_value=value).start()
            setattr(self, name, mock)
        patch.object(app, 'model_limits', return_value=dict(inputTokenLimit=50000, outputTokenLimit=1000)).start()
        self.addCleanup(patch.stopall)

    def run_update(self, mode):
        sheets.update(self.store, mode, self.settings, 'key', self.work)

    def output(self):
        return self.store.put.call_args.args[1].decode()

    def test_append_preserves_existing_conditions_and_only_analyses_new_pdf(self):
        self.run_update('append')
        self.assertEqual(self.analyse.call_count, 1)
        self.assertTrue(self.output().startswith(self.old))
        self.assertIn('## B｜B.PDF', self.output())
        self.assertEqual(self.store.put.call_args_list[0].args[1], self.old.encode())
        path = self.work / 'engineers.md'
        path.write_text(self.output(), encoding='utf-8')
        profiles = engineer_matching.load_profiles(path)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0]['additional_conditions'], '- 高単価は避ける')

    def test_overwrite_replaces_old_sections_and_conditions(self):
        self.analyse.side_effect = [record('A'), record('B')]
        self.run_update('overwrite')
        self.assertEqual(self.analyse.call_count, 2)
        self.assertNotIn('高単価は避ける', self.output())
        self.assertEqual(self.output().count('## A｜'), 1)

    def test_missing_engineers_can_be_created(self):
        self.store.request.side_effect = [None, b'%PDF-test', b'%PDF-test', None]
        self.analyse.side_effect = [record('A'), record('B')]
        self.run_update('append')
        self.assertEqual(self.store.put.call_count, 1)
        self.assertIn('## A｜A.pdf', self.output())

    def test_no_new_pdf_is_noop(self):
        self.list_pdfs.return_value = [{'name': 'A.pdf'}]
        self.run_update('append')
        self.analyse.assert_not_called()
        self.store.put.assert_not_called()

    def test_failed_sheet_never_publishes_partial_results(self):
        self.analyse.side_effect = [record('A'), ValueError('最寄り駅がありません')]
        with self.assertRaisesRegex(ValueError, '最寄り駅'):
            self.run_update('overwrite')
        self.store.put.assert_not_called()

    def test_duplicate_names_never_overwrite(self):
        self.analyse.return_value = record('A')
        with self.assertRaisesRegex(ValueError, '重複'):
            self.run_update('append')
        self.store.put.assert_not_called()

    def test_concurrent_edit_is_not_overwritten(self):
        self.store.request.side_effect = [self.old.encode(), b'%PDF-test', b'edited']
        with self.assertRaisesRegex(ValueError, '変更'):
            self.run_update('append')
        self.store.put.assert_not_called()

    def test_station_and_completeness_are_required(self):
        for station in ('', '不明', '未記載', '要確認', 'N/A', '?'):
            with self.subTest(station=station), self.assertRaises(ValueError):
                sheets.validate_sheet(record(station=station))
        value = record()
        value['attributes'] = []
        with self.assertRaises(ValueError):
            sheets.validate_sheet(value)
        value = record()
        value['complete'] = False
        with self.assertRaises(ValueError):
            sheets.validate_sheet(value)
        self.assertEqual(sheets.validate_sheet(record()), record())

    def test_pdf_payload_and_existing_response_validation(self):
        payload = app.generation_payload(self.settings, 'prompt',
                                        {'mimeType': 'application/pdf', 'data': 'JVBERi0='}, sheets.SCHEMA)
        self.assertEqual(payload['contents'][0]['parts'][1]['inlineData']['mimeType'], 'application/pdf')
        limiter = Mock()
        with patch.object(app, 'count_input_tokens', return_value=100), patch.object(
                app, 'generate', return_value=json.dumps(record())) as generate:
            result = self.real_analyse(b'%PDF-test', self.settings, 'key', limiter, {'inputTokenLimit': 5000})
            self.assertEqual(result, record())
            generate.call_args.kwargs['before_request']()
            limiter.reserve.assert_called_once_with(100)
        with self.assertRaises(ValueError):
            self.real_analyse(b'not pdf', self.settings, 'key', limiter, {})

    def test_pdf_model_uses_toml_override_without_changing_mail_model(self):
        common = dict(self.settings, model='mail-model', skill_sheets_model='pdf-model')
        selected = sheets.model_settings(common)
        self.assertEqual(selected['model'], 'pdf-model')
        self.assertEqual(common['model'], 'mail-model')
        self.assertNotIn('skill_sheets_model', selected)
        self.assertEqual(selected['requests_per_minute'], common['requests_per_minute'])
        with patch.object(app, 'model_limits', return_value=dict(
                inputTokenLimit=50000, outputTokenLimit=1000)) as limits:
            sheets.update(self.store, 'append', selected, 'key', self.work)
            self.assertEqual(limits.call_args.args[0]['model'], 'pdf-model')
            self.assertEqual(self.analyse.call_args.args[1]['model'], 'pdf-model')

    def test_pdf_model_defaults_to_common_model(self):
        self.assertEqual(sheets.model_settings({'model': 'mail-model'}), {'model': 'mail-model'})

    def test_internal_id_is_assigned_by_application(self):
        value = record()
        del value['id']
        self.assertNotIn('id', sheets.SCHEMA['properties'])
        self.assertNotIn('各idをrecordsに一度ずつ返す', sheets.PROMPT)
        with patch.object(app, 'count_input_tokens', return_value=100), patch.object(
                app, 'generate', return_value=json.dumps(value)):
            result = self.real_analyse(b'%PDF-test', self.settings, 'key', Mock(), {'inputTokenLimit': 5000})
        self.assertEqual(result['id'], 'sheet')

    def test_invalid_duration_retries_with_specific_feedback(self):
        invalid = record()
        invalid['facts'][0]['duration_kind'] = 'total'
        limiter = Mock()
        with patch.object(app, 'count_input_tokens', return_value=100) as count, patch.object(
                app, 'generate', side_effect=[json.dumps(invalid), json.dumps(record())]) as generate:
            result = self.real_analyse(b'%PDF-test', self.settings, 'key', limiter, {'inputTokenLimit': 5000})
            self.assertEqual(result, record())
            self.assertEqual(generate.call_count, 2)
            prompt = generate.call_args.args[2]
            self.assertIn('facts[0]', prompt)
            self.assertIn('duration_kind=period', prompt)
            self.assertEqual(count.call_args.args[2], prompt)
            for call in generate.call_args_list:
                call.kwargs['before_request']()
            self.assertEqual(limiter.reserve.call_count, 2)

    def test_repeated_invalid_response_reports_field_and_stops(self):
        invalid = record()
        invalid['attributes'].append({'field': 'available_from', 'value': '2026-11'})
        with patch.object(app, 'count_input_tokens', return_value=100), patch.object(
                app, 'generate', return_value=json.dumps(invalid)) as generate:
            with self.assertRaisesRegex(ValueError, r'再解析後.*attributes\[1\].available_from'):
                self.real_analyse(b'%PDF-test', self.settings, 'key', Mock(), {'inputTokenLimit': 5000})
            self.assertEqual(generate.call_count, 2)

    def test_incomplete_pdf_reports_issues_in_retry_and_final_error(self):
        invalid = record()
        invalid.update(complete=False, unassessed_information=['3ページの案件表が判読できない'])
        with patch.object(app, 'count_input_tokens', return_value=100), patch.object(
                app, 'generate', return_value=json.dumps(invalid)) as generate:
            with self.assertRaisesRegex(ValueError, '再解析後.*3ページの案件表が判読できない') as error:
                self.real_analyse(b'%PDF-test', self.settings, 'key', Mock(), {'inputTokenLimit': 5000})
            self.assertIn('"complete": false', str(error.exception))
            self.assertIn('3ページの案件表が判読できない', generate.call_args.args[2])
            self.assertEqual(generate.call_count, 2)

    def test_unknown_optional_information_does_not_prevent_valid_extraction(self):
        value = record()
        value['facts'][0].update(months=-1, duration_kind='unknown', start='', end='', experience='unknown')
        # All optional attributes absent; station remains mandatory.
        self.assertEqual(sheets.validate_sheet(value), value)
        value['complete'] = False
        with self.assertRaisesRegex(ValueError, r'"complete": false.*"unassessed_information": \[\]'):
            sheets.validate_sheet(value)

    def test_completion_prompt_distinguishes_unstated_information_from_unread_pages(self):
        self.assertIn('資料にない情報の不足', sheets.PROMPT)
        self.assertIn('対象箇所と抽出できない理由', sheets.PROMPT)
        self.assertNotIn('未処理の情報があればunassessed_informationに短いキーワードを残す', sheets.PROMPT)

    def test_listing_paginates_and_filters_files(self):
        self.store.base = 'https://graph.microsoft.com/v1.0/drives/d/items/f'
        next_url = 'https://graph.microsoft.com/v1.0/drives/d/items/f/children?$skiptoken=abc'
        with patch.object(sheets.sharepoint_reports, 'graph', side_effect=[
                {'value': [{'name': 'z.PDF', 'file': {}}, {'name': 'folder.pdf', 'folder': {}}],
                 '@odata.nextLink': next_url},
                {'value': [{'name': 'a.pdf', 'file': {}}, {'name': 'x.txt', 'file': {}}]}]):
            self.assertEqual([i['name'] for i in self.real_list_pdfs(self.store)], ['a.pdf', 'z.PDF'])
        with patch.object(sheets.sharepoint_reports, 'graph', return_value={
                'value': [], '@odata.nextLink': 'https://evil.example/page'}):
            with self.assertRaises(ValueError):
                self.real_list_pdfs(self.store)


if __name__ == '__main__':
    unittest.main()
