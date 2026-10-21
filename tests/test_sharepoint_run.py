import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mail_analyzer as app
import sharepoint_run as runner
import run_progress

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


if __name__ == '__main__':
    unittest.main()
