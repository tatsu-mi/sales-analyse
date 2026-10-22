import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mail_analyzer as app
import sharepoint_run as runner
import run_progress
import analysis_pipeline
import title_filter

snapshot_settings = runner.snapshot_settings


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.fail_checkpoint = False

    def folder(self, *args):
        pass

    def put(self, path, data):
        if self.fail_checkpoint and path == 'run-state/checkpoint.json':
            raise app.ToolError('checkpoint upload failed')
        self.files[path] = data
        return {'webUrl': 'https://example.sharepoint.com/' + path}

    def request(self, method, path, missing=False):
        if missing:
            return self.files.get(path)
        return self.files[path]

    def get_json(self, path, missing=False):
        raw = self.request('GET', path, missing=missing)
        return None if raw is None else json.loads(raw)


class SharePointRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore()
        env = {'SAKURA_MAIL_HOST': 'example.invalid', 'SAKURA_MAIL_USERNAME': 'user',
               'SAKURA_MAIL_PASSWORD': 'private-password', 'GEMINI_API_KEY': 'private-key',
               'TEAMS_WEBHOOK_URL': 'https://example.invalid/private-hook',
               'DRIVE_ID': 'drive', 'FOLDER_ID': 'folder'}
        setting = patch.dict(runner.os.environ, env, clear=True)
        setting.start()
        self.addCleanup(setting.stop)
        for setting in (patch.object(app.environment, 'load_env'),
                        patch.object(runner.sharepoint_reports, 'ensure_reports_folder'),
                        patch.object(runner, 'snapshot_settings', side_effect=self.settings)):
            setting.start()
            self.addCleanup(setting.stop)

    def settings(self, source, work):
        text = Path('config.example.toml').read_text(encoding='utf-8')
        text = text.replace('priority_rules.example.md', 'priority_rules.md')
        text = text.replace('enabled = true', 'enabled = false')
        (work / 'config.toml').write_text(text, encoding='utf-8')
        (work / 'engineers.md').write_text('## A\n\nPython', encoding='utf-8')
        runner.shutil.copyfile('priority_rules.example.md', work / 'priority_rules.md')

    def execute(self, name, resume=False):
        work = self.root / name
        work.mkdir()
        runner.execute(self.store, work, '123-1', resume=resume)
        return work

    def test_notification_failure_resumes_without_fetch_or_settings_download(self):
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])) as fetch, \
                patch.object(runner.scheduled_run, 'send_card', side_effect=app.ToolError('notify failed')):
            with self.assertRaisesRegex(app.ToolError, 'notify failed'):
                self.execute('first')
        self.assertEqual(fetch.call_count, 1)
        manifest = self.store.get_json('temporary-runs/123-1/progress.json')
        self.assertEqual(manifest['status'], 'report_uploaded')
        self.assertNotIn('run-state/checkpoint.json', self.store.files)
        with patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('IMAP repeated')), \
                patch.object(runner, 'snapshot_settings', side_effect=AssertionError('settings changed')), \
                patch.object(runner.scheduled_run, 'send_card') as send:
            self.execute('resumed', resume=True)
        send.assert_called_once()
        self.assertEqual(self.store.get_json('temporary-runs/123-1/progress.json')['status'], 'complete')
        self.assertIn('last_run_at', self.store.get_json('run-state/checkpoint.json'))
        for name in ('config.toml', 'engineers.md', 'priority_rules.md'):
            data = self.store.files['temporary-runs/123-1/' + name]
            self.assertNotIn(b'private-password', data)
            self.assertNotIn(b'private-key', data)
            self.assertNotIn(b'private-hook', data)

    def test_boundary_upload_failure_resumes_without_notification_and_keeps_new_boundary(self):
        old = {'mailbox': 'placeholder', 'last_run_at': '2026-10-01T09:00:00+09:00'}
        # Seed a valid mailbox identity for the copied settings.
        config_work = self.root / 'settings'
        config_work.mkdir()
        self.settings(None, config_work)
        config = app.load_config(config_work / 'config.toml')
        old['mailbox'] = app.last_run_path(config_work / 'output', config).stem
        self.store.files['run-state/checkpoint.json'] = json.dumps(old).encode()
        self.store.fail_checkpoint = True
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])), \
                patch.object(runner.scheduled_run, 'send_card'):
            with self.assertRaisesRegex(app.ToolError, 'checkpoint upload failed'):
                self.execute('first')
        self.assertEqual(self.store.get_json('run-state/checkpoint.json'), old)
        manifest = self.store.get_json('temporary-runs/123-1/progress.json')
        self.assertEqual(manifest['status'], 'notified')
        self.store.fail_checkpoint = False
        with patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('fetch')), \
                patch.object(runner.scheduled_run, 'send_card', side_effect=AssertionError('duplicate notify')):
            self.execute('resumed', resume=True)
        self.assertGreater(self.store.get_json('run-state/checkpoint.json')['last_run_at'], old['last_run_at'])

    def test_resume_after_fetch_failure_of_analysis_uses_original_period_and_mail(self):
        mail = {'uid': '1', 'body': 'Python', 'body_truncated': False}
        public = io.StringIO()
        with patch.object(app, 'iter_fetch_mails', return_value=iter([mail])), \
                patch.object(run_progress, 'stream', public), \
                patch.object(app.analysis_pipeline, 'Pipeline', side_effect=app.ToolError('analysis failed')):
            with self.assertRaisesRegex(app.ToolError, 'analysis failed'):
                self.execute('first')
        self.assertIn('メール取得中', public.getvalue())
        self.assertIn('メール取得完了：1通', public.getvalue())
        self.assertIn('SharePointへの一時保存が完了', public.getvalue())
        self.assertNotIn('Python', public.getvalue())
        manifest = self.store.get_json('temporary-runs/123-1/progress.json')
        self.assertEqual(manifest['status'], 'fetched')
        original = copy.deepcopy(manifest)
        def finish(api, config, key, source, output, metadata):
            self.assertEqual(list(app.iter_saved_mails(source)), [mail])
            self.assertEqual(metadata['stop_at'], original['period']['stop_at'])
            app.write_json(output / 'priority_rules.json', config['priority_rules'])
            app.write_json(output / 'ranked_emails.json', {'ses': [], 'contract': []})
            from types import SimpleNamespace
            return SimpleNamespace(run=lambda: 'resumed report')

        with patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('IMAP repeated')), \
                patch.object(app.analysis_pipeline, 'Pipeline', side_effect=finish), \
                patch.object(runner.scheduled_run, 'send_card'):
            self.execute('resumed', resume=True)
        self.assertEqual(self.store.get_json('run-state/checkpoint.json')['last_run_at'],
                         original['period']['stop_at'])

    def test_newer_delivery_prevents_stale_resume(self):
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])), \
                patch.object(runner.scheduled_run, 'send_card', side_effect=app.ToolError('notify failed')):
            with self.assertRaises(app.ToolError):
                self.execute('first')
        self.store.files['run-state/checkpoint.json'] = b'{"last_run_at":"newer"}'
        with patch.object(runner.scheduled_run, 'prepare') as prepare:
            with self.assertRaisesRegex(app.ToolError, 'newer delivery'):
                self.execute('resumed', resume=True)
        prepare.assert_not_called()

    def test_snapshot_settings_rebases_paths_and_preserves_instruction_text(self):
        source = self.root / 'source'
        source.mkdir()
        self.settings(None, source)
        config = source / 'config.toml'
        text = config.read_text(encoding='utf-8')
        text = text.replace('output_dir = "output"', '  output_dir = "elsewhere"')
        text = text.replace('instructions = """', 'instructions = """\noutput_dir = "literal instruction"')
        config.write_text(text, encoding='utf-8')
        destination = self.root / 'snapshot'
        destination.mkdir()
        snapshot_settings(config, destination)
        saved = app.load_config(destination / 'config.toml')
        self.assertEqual(saved['analysis']['output_dir'], 'output')
        self.assertIn('output_dir = "literal instruction"', saved['analysis']['instructions'])
        self.assertEqual(Path(saved['analysis']['engineers_file']), destination / 'engineers.md')

    def test_fetch_failure_resume_keeps_the_original_requested_period(self):
        with patch.object(app, 'iter_fetch_mails', side_effect=app.ToolError('fetch failed')):
            with self.assertRaisesRegex(app.ToolError, 'fetch failed'):
                self.execute('first')
        period = self.store.get_json('temporary-runs/123-1/progress.json')['period']
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])) as fetch, \
                patch.object(runner.scheduled_run, 'send_card'):
            self.execute('resumed', resume=True)
        self.assertEqual([value.isoformat() for value in fetch.call_args.args[1:]],
                         [period['start_at'], period['stop_at']])

    def test_analysis_failure_uploads_cached_ai_results_and_resume_does_not_resend_them(self):
        mails = [dict(uid=str(i), body=f'mail {i}', body_truncated=False,
                      subject=f'project {i}', received_at='2026-10-01T09:00:00+09:00')
                 for i in (1, 2)]
        uploads, extraction_calls, uploaded_bodies = {}, [], []
        fail = True
        snapshots_before_analysis = None
        original_groups = analysis_pipeline.Pipeline.groups

        def groups(pipeline, items, prompt, settings, limit, schema=None):
            if schema == analysis_pipeline.EXTRACTION_SCHEMA:
                for item in items:
                    yield [item], analysis_pipeline.encoded(item) + '\n', 100
            else:
                yield from original_groups(pipeline, items, prompt, settings, limit, schema)

        def upload(settings, key, path):
            items = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            uri = str(len(uploads))
            uploads[uri] = items
            uploaded_bodies.extend(item['uid'] for item in items if 'body' in item)
            return {'uri': uri, 'mimeType': 'text/plain'}

        def generate(settings, key, prompt, *, file_data, response_schema, before_request):
            nonlocal snapshots_before_analysis
            before_request()
            items = uploads[file_data['uri']]
            if response_schema == title_filter.SCHEMA:
                return json.dumps({'records': [dict(uid=item['uid'], exclude=False) for item in items]})
            self.assertEqual(response_schema, analysis_pipeline.EXTRACTION_SCHEMA)
            snapshot_count = sum('/snapshot-' in name for name in self.store.files)
            if snapshots_before_analysis is None:
                snapshots_before_analysis = snapshot_count
            uid = items[0]['uid']
            extraction_calls.append(uid)
            if fail and uid == '2':
                # Successful AI calls are cached locally without immediate uploads.
                self.assertEqual(snapshot_count, snapshots_before_analysis)
                raise app.ToolError('rate limit interrupted analysis')
            return json.dumps({'records': [dict(uid=item['uid'], kind='other', tags=[],
                                                 market_notes='', opportunities=[]) for item in items]})

        with patch.object(app, 'model_limits', return_value={'inputTokenLimit': 1048576, 'outputTokenLimit': 65536}), \
                patch.object(app, 'count_input_tokens', return_value=100), \
                patch.object(app, 'upload_mail_file', side_effect=upload), \
                patch.object(app, 'generate', side_effect=generate), \
                patch.object(analysis_pipeline.Pipeline, 'groups', groups), \
                patch.object(analysis_pipeline.RateLimiter, 'reserve'), \
                patch.object(runner.scheduled_run, 'send_card'):
            with patch.object(app, 'iter_fetch_mails', return_value=iter(mails)):
                with self.assertRaisesRegex(app.ToolError, 'rate limit interrupted'):
                    self.execute('first')
            manifest = self.store.get_json('temporary-runs/123-1/progress.json')
            self.assertEqual(sum('/snapshot-' in name for name in self.store.files),
                             snapshots_before_analysis + 1)
            self.assertIn(manifest['archive'], '\n'.join(self.store.files))
            fail = False
            with patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('mail fetched again')):
                self.execute('resumed', resume=True)
        self.assertEqual(extraction_calls, ['1', '2', '2'])
        self.assertEqual(uploaded_bodies.count('1'), 1)
        self.assertEqual(self.store.get_json('temporary-runs/123-1/progress.json')['status'], 'complete')

    def test_failed_error_snapshot_keeps_original_error_and_last_saved_snapshot(self):
        failing = False
        original_put = self.store.put
        public = io.StringIO()

        def put(path, data):
            if failing and '/snapshot-' in path:
                raise OSError('snapshot upload failed')
            return original_put(path, data)

        def prepare(*args, **kwargs):
            nonlocal failing
            failing = True
            raise app.ToolError('original analysis failure')

        with patch.object(self.store, 'put', side_effect=put), \
                patch.object(runner.scheduled_run, 'prepare', side_effect=prepare), \
                patch.object(run_progress, 'stream', public):
            with self.assertRaisesRegex(app.ToolError, 'original analysis failure'):
                self.execute('first')
        manifest = self.store.get_json('temporary-runs/123-1/progress.json')
        self.assertEqual(manifest['status'], 'created')
        self.assertIn('temporary-runs/123-1/' + manifest['archive'], self.store.files)
        self.assertIn('前回保存分から再開', public.getvalue())

    def test_cli_saves_error_history_redacts_credentials_and_preserves_resume_state(self):
        log = self.root / 'mail-analysis.log'
        log.write_text('mail fetch completed\nprivate-password private-key\n'
                       'https://example.invalid/download?sig=secret-signature\n'
                       'Authorization: Bearer unknown-access-token\n', encoding='utf-8')
        resume_state = b'{"status":"fetched","archive":"snapshot-original.zip"}'
        self.store.files['temporary-runs/123-1/progress.json'] = resume_state
        public, details = io.StringIO(), io.StringIO()
        for attempt in range(2):
            with patch.object(runner.sys, 'argv', ['sharepoint_run.py', '--resume', '123-1', '--log-file', str(log)]), \
                    patch.object(runner, 'Store', return_value=self.store), \
                    patch.object(runner.Path, 'resolve', return_value=self.root / f'work-{attempt}'), \
                    patch.object(runner, 'execute', side_effect=RuntimeError('analysis failed: private-key')), \
                    patch.object(run_progress, 'stream', public), \
                    contextlib.redirect_stdout(details), contextlib.redirect_stderr(details):
                self.assertEqual(runner.main(), 1)
        errors = {name: data for name, data in self.store.files.items() if '/error-' in name}
        self.assertEqual(len(errors), 2)
        for name, data in errors.items():
            self.assertTrue(name.startswith('temporary-runs/123-1/error-'))
            text = data.decode('utf-8')
            self.assertIn('mail fetch completed', text)
            self.assertIn('Traceback (most recent call last)', text)
            self.assertIn('RuntimeError: analysis failed', text)
            for secret in ('private-password', 'private-key', 'secret-signature', 'unknown-access-token'):
                self.assertNotIn(secret, text)
        self.assertEqual(self.store.files['temporary-runs/123-1/progress.json'], resume_state)
        self.assertIn('詳細エラーログをSharePoint', public.getvalue())
        self.assertNotIn('analysis failed', public.getvalue())

    def test_error_upload_failure_returns_original_failure_and_keeps_local_diagnostics(self):
        log = self.root / 'mail-analysis.log'
        log.write_text('execution details', encoding='utf-8')
        public, details = io.StringIO(), io.StringIO()
        with patch.object(runner.sys, 'argv', ['sharepoint_run.py', '--resume', '123-1', '--log-file', str(log)]), \
                patch.object(runner, 'Store', return_value=self.store), \
                patch.object(runner.Path, 'resolve', return_value=self.root / 'work'), \
                patch.object(runner, 'execute', side_effect=ValueError('original failure')), \
                patch.object(self.store, 'put', side_effect=app.ToolError('SharePoint unavailable')), \
                patch.object(run_progress, 'stream', public), \
                contextlib.redirect_stdout(details), contextlib.redirect_stderr(details):
            self.assertEqual(runner.main(), 1)
        errors = list(self.root.glob('error-*.log'))
        self.assertEqual(len(errors), 1)
        self.assertIn('ValueError: original failure', errors[0].read_text(encoding='utf-8'))
        self.assertIn('エラーログ保存にも失敗', public.getvalue())


class MatchingCacheTests(unittest.TestCase):
    def test_only_matching_records_survive_round_trip_to_a_fresh_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / 'old', root / 'new'
            cache = old / 'output/.analysis_cache/token-files-v1'
            for folder in ('career_facts', 'shared_projects', 'answers', 'tokens'):
                path = cache / folder / ('a' * 64 + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"record":{"id":"example"}}', encoding='utf-8')
            store = MemoryStore()
            runner.save_matching_cache(store, old)
            runner.restore_matching_cache(store.files['run-state/matching-cache.zip'], new)
            restored = new / 'output/.analysis_cache/token-files-v1'
            self.assertEqual((restored / 'career_facts' / ('a' * 64 + '.json')).read_bytes(),
                             (cache / 'career_facts' / ('a' * 64 + '.json')).read_bytes())
            self.assertTrue((restored / 'shared_projects' / ('a' * 64 + '.json')).is_file())
            self.assertFalse((restored / 'answers').exists())
            self.assertFalse((restored / 'tokens').exists())

    def test_unexpected_or_traversal_cache_paths_are_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('../config.toml', 'career_facts/../../config.toml',
                         '/absolute.json', 'answers/' + 'a' * 64 + '.json'):
                buffer = io.BytesIO()
                with runner.zipfile.ZipFile(buffer, 'w') as archive:
                    archive.writestr(name, '{}')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    runner.restore_matching_cache(buffer.getvalue(), root)
            self.assertFalse(list(root.rglob('*')))


if __name__ == '__main__':
    unittest.main()
