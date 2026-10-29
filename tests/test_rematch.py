import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from sales_analyze.environment import load_env as load_dotenv

from sales_analyze import analysis_pipeline
from sales_analyze import mail_analyzer as app
from sales_analyze import rematch
import test_integrated_pipeline as integrated
from test_sharepoint_run import MemoryStore


class RematchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integrated.IntegratedPipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h = self.fixture.h
        self.config_path = self.h.root / 'config.toml'
        self.config_path.write_text(Path('config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
        (self.h.root / 'engineers.md').write_text('## New engineer\n\nPython', encoding='utf-8')
        (self.h.root / 'priority_rules.example.md').write_bytes(Path('priority_rules.example.md').read_bytes())
        self.env = patch.dict(app.os.environ, {'GEMINI_API_KEY': 'key'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ('model_limits', 'count_input_tokens', 'upload_mail_file', 'generate'):
            setting = patch.object(app, name, getattr(self.h.api, name))
            setting.start()
            self.addCleanup(setting.stop)
        setting = patch.object(analysis_pipeline.RateLimiter, 'reserve')
        setting.start()
        self.addCleanup(setting.stop)

    def original(self):
        pipeline = self.fixture.make(self.fixture.setup_projects(1))
        pipeline.metadata.update(start_at='2026-09-27T00:00:00+09:00',
                                 stop_at='2026-10-04T00:00:00+09:00')
        summary = pipeline.run()
        app.write_report(pipeline.output, pipeline.metadata, summary)
        return pipeline.output

    def test_completed_analysis_rematches_current_profiles_without_fetch_screen_or_extraction(self):
        source = self.original()
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        checkpoint = self.h.root / 'output/.last_run_test.json'
        checkpoint.parent.mkdir(exist_ok=True)
        checkpoint.write_text('unchanged', encoding='utf-8')
        extracted_calls = len(self.h.calls)
        profile_calls, project_calls = len(self.fixture.profile_calls), len(self.fixture.project_calls)
        with patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('IMAP called')), \
                patch.object(analysis_pipeline.Pipeline, 'run', side_effect=AssertionError('full analysis called')), \
                patch.object(analysis_pipeline.Pipeline, 'screen_titles', side_effect=AssertionError('titles called')), \
                patch.object(analysis_pipeline.Pipeline, 'extract', side_effect=AssertionError('extraction called')):
            output = rematch.rematch(self.config_path, source, source_id='123-1')
            second = rematch.rematch(self.config_path, source)
        self.assertNotEqual(output, second)
        self.assertEqual(len(self.h.calls), extracted_calls)
        self.assertEqual(len(self.fixture.profile_calls), profile_calls + 2)
        self.assertEqual(len(self.fixture.project_calls), project_calls + 2)
        ranked = app.read_metadata(output / 'ranked_emails.json')
        self.assertEqual([g['name'] for g in ranked['ses_by_engineer']], ['New engineer'])
        self.assertIn('source_email', ranked['ses_by_engineer'][0]['entries'][0])
        metadata = app.read_metadata(output / 'metadata.json')
        original = app.read_metadata(source / 'metadata.json')
        for key in ('start_at', 'stop_at', 'extracted_unique_count', 'count'):
            self.assertEqual(metadata[key], original[key])
        self.assertEqual(metadata['rematch_source'], '123-1')
        self.assertEqual(metadata['status'], 'complete')
        self.assertEqual(checkpoint.read_text(encoding='utf-8'), 'unchanged')
        self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()})

    def test_common_condition_changes_fail_before_ai(self):
        source = self.original()
        rules = app.read_metadata(source / 'priority_rules.json')
        rules['ses']['criteria'][0] = 'Different condition'
        app.write_json(source / 'priority_rules.json', rules)
        with patch.object(app, 'model_limits') as model:
            with self.assertRaisesRegex(app.ToolError, 'SES共通条件'):
                rematch.rematch(self.config_path, source)
        model.assert_not_called()

    def test_screening_start_accepts_only_fetched_mail_and_rebuilds_all_analysis(self):
        source = self.original()
        for path in source.iterdir():
            if path.is_file() and path.name not in ('emails.jsonl', 'metadata.json'):
                path.unlink()
        metadata = app.read_metadata(source / 'metadata.json')
        for name in ('extracted_unique_count', 'skipped_unique_count', 'skipped_emails', 'extraction_chunks'):
            metadata.pop(name, None)
        metadata['status'] = 'fetched'
        metadata.pop('pipeline_version', None)
        app.write_json(source / 'metadata.json', metadata)
        calls = len(self.h.calls)
        with patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('IMAP called')):
            output = rematch.rematch(self.config_path, source, start_from='screening')
        self.assertGreater(len(self.h.calls), calls)
        self.assertEqual(app.read_metadata(output / 'metadata.json')['status'], 'complete')
        self.assertTrue((output / 'title_selection.json').is_file())

    def test_extraction_start_reuses_title_selection_and_rechecks_common_conditions(self):
        source = self.original()
        rules = app.read_metadata(source / 'priority_rules.json')
        rules['ses']['criteria'][0] = 'Old condition'
        app.write_json(source / 'priority_rules.json', rules)
        selection = (source / 'title_selection.json').read_bytes()
        calls = len(self.h.calls)
        with patch.object(analysis_pipeline.Pipeline, 'screen_part', side_effect=AssertionError('AI titles called')):
            output = rematch.rematch(self.config_path, source, start_from='extraction')
        self.assertGreater(len(self.h.calls), calls)
        self.assertEqual((output / 'title_selection.json').read_bytes(), selection)
        self.assertEqual(app.read_metadata(output / 'priority_rules.json'), app.load_config(self.config_path)['priority_rules'])

    def test_aggregation_start_skips_extraction_and_does_not_require_saved_aggregation(self):
        source = self.original()
        (source / 'opportunities.jsonl').unlink()
        (source / 'statistics.json').unlink()
        with patch.object(analysis_pipeline.Pipeline, 'extract', side_effect=AssertionError('extraction called')):
            output = rematch.rematch(self.config_path, source, start_from='aggregation')
        self.assertEqual(app.read_metadata(output / 'metadata.json')['status'], 'complete')
        self.assertTrue((output / 'opportunities.jsonl').is_file())

    def test_matching_start_reuses_profiles_and_repeats_project_matching(self):
        source = rematch.rematch(self.config_path, self.original())
        before = {p.relative_to(source.parent): p.read_bytes()
                  for p in source.parent.rglob('*') if p.is_file()}
        profiles, projects = len(self.fixture.profile_calls), len(self.fixture.project_calls)
        output = rematch.rematch(self.config_path, source, start_from='matching')
        self.assertEqual(len(self.fixture.profile_calls), profiles)
        self.assertEqual(len(self.fixture.project_calls), projects + 1)
        self.assertEqual(app.read_metadata(output / 'metadata.json')['rematch_start_from'], 'matching')
        self.assertEqual(before, {p.relative_to(source.parent): p.read_bytes()
                                 for p in source.parent.rglob('*') if p.is_file()})

    def test_scoring_start_reuses_both_steps_and_applies_new_weights(self):
        source = rematch.rematch(self.config_path, self.original())
        settings = self.config_path.read_text(encoding='utf-8')
        self.config_path.write_text(settings.replace('matching_mandatory_weight = 10',
                                                     'matching_mandatory_weight = 20'), encoding='utf-8')
        with patch.object(app, 'generate', side_effect=AssertionError('AI generation called')):
            output = rematch.rematch(self.config_path, source, start_from='scoring')
        metadata = app.read_metadata(output / 'metadata.json')
        self.assertEqual(metadata['status'], 'complete')
        self.assertEqual(metadata['integrated_matching']['weights']['mandatory'], 20)
        self.assertIn('20点', (output / 'report.html').read_text(encoding='utf-8'))

    def test_later_start_rejects_changed_profiles_before_generation(self):
        source = rematch.rematch(self.config_path, self.original())
        (self.h.root / 'engineers.md').write_text('## Changed engineer\n\nJava', encoding='utf-8')
        with patch.object(app, 'generate', side_effect=AssertionError('AI generation called')):
            with self.assertRaisesRegex(app.ToolError, '--start-from profiles'):
                rematch.rematch(self.config_path, source, start_from='matching')

    def test_scoring_start_requires_valid_project_cache(self):
        source = rematch.rematch(self.config_path, self.original())
        for path in (source.parent / '.analysis_cache/token-files-v1/shared_projects').glob('*.json'):
            path.write_text('{"record": {"i": 0, "m": []}}', encoding='utf-8')
        with patch.object(app, 'generate', side_effect=AssertionError('AI generation called')):
            with self.assertRaisesRegex(app.ToolError, '--start-from matching'):
                rematch.rematch(self.config_path, source, start_from='scoring')

    def test_cli_passes_selected_start_point(self):
        source = rematch.rematch(self.config_path, self.original())
        with patch.object(rematch.sys, 'argv', ['rematch.py', '--config', str(self.config_path),
                                              '--source-dir', str(source), '--start-from', 'scoring']), \
                patch.object(rematch, 'rematch', wraps=rematch.rematch) as match:
            self.assertEqual(rematch.main(), 0)
        self.assertEqual(match.call_args.kwargs['start_from'], 'scoring')

    def test_local_cli_needs_no_sharepoint_or_mail_credentials_and_does_not_publish(self):
        source = self.original()
        public = io.StringIO()
        with patch.object(rematch.sys, 'argv', ['rematch.py', '--config', str(self.config_path),
                                              '--source-dir', str(source)]), \
                patch.object(rematch.sharepoint_run, 'Store', side_effect=AssertionError('SharePoint called')), \
                patch.object(rematch, 'publish', side_effect=AssertionError('publish called')), \
                patch.object(rematch.run_progress, 'stream', public):
            self.assertEqual(rematch.main(), 0)
        self.assertIn('再マッチングが完了', public.getvalue())
        self.assertNotIn('前回実行時間の更新', public.getvalue())

    def test_sharepoint_cli_loads_dotenv_and_uses_latest_settings_by_default(self):
        source = self.original()
        (self.config_path.parent / '.env').write_text(
            'DRIVE_ID="dotenv-drive"\nFOLDER_ID="dotenv-folder"\n', encoding='utf-8')

        def restore(store, run_id, work):
            self.assertEqual(run_id, '123-1')
            self.assertEqual(store.base, 'https://graph.microsoft.com/v1.0/drives/dotenv-drive/items/env-folder')
            return source

        latest = {'engineers.md': '## SharePoint engineer\n\nPython'.encode('utf-8'),
                  'priority_rules.md': Path('priority_rules.example.md').read_bytes()}

        def request(method, path, **kwargs):
            self.assertEqual(method, 'GET')
            return latest[path]

        with patch.dict(app.os.environ, {'FOLDER_ID': 'env-folder'}), \
                patch.object(app.environment, 'load_env', side_effect=load_dotenv), \
                patch.object(rematch.sys, 'argv', ['rematch.py', '--config', str(self.config_path),
                                              '--run-id', '123-1']), \
                patch.object(rematch, 'restore_run', side_effect=restore) as restored, \
                patch.object(rematch.sharepoint_run.Store, 'request', side_effect=request) as downloaded, \
                patch.object(rematch.sharepoint_run, 'save_failure'):
            self.assertEqual(rematch.main(), 0)
        restored.assert_called_once()
        self.assertEqual([call.args for call in downloaded.call_args_list],
                         [('GET', 'engineers.md'), ('GET', 'priority_rules.md')])
        output = next((self.h.root / 'output').glob('rematch_*/result'))
        ranked = app.read_metadata(output / 'ranked_emails.json')
        self.assertEqual([g['name'] for g in ranked['ses_by_engineer']], ['SharePoint engineer'])
        self.assertEqual((output.parent / 'engineers.md').read_bytes(), latest['engineers.md'])

    def test_latest_settings_download_failure_stops_without_using_old_profiles(self):
        with patch.object(rematch.sys, 'argv', ['rematch.py', '--config', str(self.config_path),
                                              '--run-id', '123-1']), \
                patch.object(rematch.sharepoint_run, 'Store') as store, \
                patch.object(rematch, 'restore_run') as restore, \
                patch.object(rematch, 'rematch') as match, \
                patch.object(rematch.sharepoint_run, 'save_failure'):
            store.return_value.request.side_effect = OSError('settings download failed')
            self.assertEqual(rematch.main(), 1)
        restore.assert_not_called()
        match.assert_not_called()

    def test_missing_or_incompatible_analysis_fails_before_ai(self):
        source = self.original()
        state_path = source / 'extraction_complete.json'
        state = app.read_metadata(state_path)
        state['pipeline_version'] = -1
        app.write_json(state_path, state)
        with patch.object(app, 'model_limits') as model:
            with self.assertRaisesRegex(app.ToolError, 'バージョン'):
                rematch.rematch(self.config_path, source)
            state['pipeline_version'] = analysis_pipeline.VERSION
            app.write_json(state_path, state)
            (source / 'opportunities.jsonl').unlink()
            with self.assertRaisesRegex(app.ToolError, '不足'):
                rematch.rematch(self.config_path, source)
        model.assert_not_called()

    def test_restore_completed_or_old_run_is_read_only_and_rejects_unsafe_paths(self):
        source = self.original()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for path in source.rglob('*'):
                if path.is_file():
                    archive.write(path, 'output/old/' + path.relative_to(source).as_posix())
        store = MemoryStore()
        archive_name = 'snapshot-' + 'a' * 32 + '.zip'
        manifest = dict(status='complete', output='output/old', archive=archive_name,
                        previous_checkpoint=None)
        store.files.update({'temporary-runs/123-1/progress.json': json.dumps(manifest).encode(),
                            'temporary-runs/123-1/' + archive_name: buffer.getvalue(),
                            'run-state/checkpoint.json': b'{"last_run_at":"newer"}'})
        before = copy.deepcopy(store.files)
        work = self.h.root / 'restored'
        source_dir = rematch.restore_run(store, '123-1', work)
        output = rematch.rematch(self.config_path, source_dir, source_id='123-1')
        self.assertTrue((output / 'report.html').is_file())
        self.assertEqual(store.files, before)
        for run_id in ('../escape', 'x/y', ''):
            with self.assertRaises(app.ToolError):
                rematch.restore_run(store, run_id, work)
        manifest['output'] = '../outside'
        store.files['temporary-runs/123-1/progress.json'] = json.dumps(manifest).encode()
        with self.assertRaisesRegex(app.ToolError, '出力先'):
            rematch.restore_run(store, '123-1', work)

    def test_publish_writes_only_a_separate_report_and_notifies(self):
        output = rematch.rematch(self.config_path, self.original())
        store = MemoryStore()
        store.files['run-state/checkpoint.json'] = b'original checkpoint'
        with patch.dict(app.os.environ, {'DRIVE_ID': 'drive', 'FOLDER_ID': 'folder',
                                        'TEAMS_WEBHOOK_URL': 'https://example.invalid/hook'}), \
                patch.object(rematch.sharepoint_run.sharepoint_reports, 'ensure_reports_folder'), \
                patch.object(rematch.scheduled_run, 'post_report') as post:
            rematch.publish(store, output, 'unique')
        post.assert_called_once()
        self.assertEqual(set(store.files), {'run-state/checkpoint.json', 'reports/rematch-unique.html'})
        self.assertTrue(store.files['reports/rematch-unique.html'].startswith(b'<!doctype html>'))
        self.assertEqual(store.files['run-state/checkpoint.json'], b'original checkpoint')

    def test_zero_mail_completed_run_does_not_call_ai(self):
        source = self.h.root / 'empty'
        source.mkdir()
        app.write_json(source / 'metadata.json', dict(count=0, truncated_bodies=0, status='complete',
            start_date='2026-10-01', end_date='2026-10-01',
            start_at='2026-10-01T00:00:00+09:00', stop_at='2026-10-02T00:00:00+09:00'))
        app.write_json(source / 'priority_rules.json', self.h.config['priority_rules'])
        (source / 'emails.jsonl').write_text('', encoding='utf-8')
        with patch.object(app, 'generate', side_effect=AssertionError('AI called')):
            output = rematch.rematch(self.config_path, source)
        self.assertIn('0件', (output / 'report.html').read_text(encoding='utf-8'))
        self.assertEqual(app.read_metadata(output / 'metadata.json')['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
