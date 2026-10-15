import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import mail_analyzer as app
import scheduled_run as scheduled


class TeamsTests(unittest.TestCase):
    def setUp(self):
        setting = patch.object(scheduled.time, 'sleep')
        self.sleep = setting.start()
        self.addCleanup(setting.stop)

    def test_notification_contains_only_one_link_and_no_report_body(self):
        url = 'https://github.com/o/r/actions/runs/1/artifacts/2'
        with patch.object(scheduled, 'send_card') as send:
            scheduled.post_report(url, 'https://example.com/webhook', 60)
        send.assert_called_once()
        card = json.loads(send.call_args.args[0])['attachments'][0]['content']
        self.assertEqual(card['body'], [{'type': 'TextBlock',
                                       'text': f'[メール分析レポートを開く]({url})', 'wrap': True}])
        self.assertNotIn('actions', card)

    def test_missing_or_invalid_link_is_rejected_before_sending(self):
        for url in ('', 'file:///tmp/report.md', 'http://example.com', 'https://example.com/ bad'):
            with self.subTest(url=url), patch.object(scheduled, 'send_card') as send:
                with self.assertRaises(app.ToolError):
                    scheduled.post_report(url, 'https://example.com/webhook', 60)
                send.assert_not_called()

    def test_accepted_responses_and_safe_failures(self):
        for status, body in ((200, b'1'), (202, b''), (204, b'')):
            response = MagicMock(status=status)
            response.read.return_value = body
            with patch.object(scheduled, 'urlopen') as call:
                call.return_value.__enter__.return_value = response
                scheduled.post_report('https://github.com/o/r/actions/runs/1/artifacts/2', 'https://example.com/private-secret', 15)
                self.assertEqual(call.call_args.kwargs['timeout'], 15)
        for failure in (HTTPError('private-secret', 429, 'private-secret', {}, None),
                        URLError('private-secret')):
            with patch.object(scheduled, 'urlopen', side_effect=failure):
                with self.assertRaises(app.ToolError) as error:
                    scheduled.post_report('https://github.com/o/r/actions/runs/1/artifacts/2', 'https://example.com/private-secret', 15)
                self.assertNotIn('private-secret', str(error.exception))
        response = MagicMock(status=200)
        response.read.return_value = b'Webhook message delivery failed: private-secret'
        with patch.object(scheduled, 'urlopen') as call:
            call.return_value.__enter__.return_value = response
            with self.assertRaises(app.ToolError):
                scheduled.post_report('https://github.com/o/r/actions/runs/1/artifacts/2', 'https://example.com', 15)

    def test_429_retries_same_card_after_retry_after_then_succeeds(self):
        response = MagicMock(status=202)
        response.read.return_value = b''
        accepted = MagicMock()
        accepted.__enter__.return_value = response
        rejected = HTTPError('https://example.com/secret', 429, 'limited', {'Retry-After': '7'}, None)
        with patch.object(scheduled, 'urlopen', side_effect=[rejected, accepted]) as call:
            scheduled.post_report('https://github.com/o/r/actions/runs/1/artifacts/2', 'https://example.com/secret', 60)
        self.sleep.assert_called_once_with(7)
        self.assertEqual(call.call_args_list[0].args[0].data, call.call_args_list[1].args[0].data)

    def test_long_retry_after_and_empty_url_fail(self):
        failure = HTTPError('https://example.com/secret', 429, 'limited', {'Retry-After': '3600'}, None)
        with patch.object(scheduled, 'urlopen', side_effect=failure):
            with self.assertRaises(app.ToolError):
                scheduled.post_report('https://github.com/o/r/actions/runs/1/artifacts/2', 'https://example.com/secret', 60)
        self.sleep.assert_not_called()
        with patch.object(scheduled, 'urlopen') as call, self.assertRaises(app.ToolError):
            scheduled.post_report('', 'https://example.com/secret', 60)
        call.assert_not_called()


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.toml'
        config = Path('config.toml').read_text(encoding='utf-8')
        rules = (Path.cwd() / 'priority_rules.toml').as_posix()
        config = config.replace('priority_rules_file = "priority_rules.toml"', f'priority_rules_file = "{rules}"')
        self.config_path.write_text(config, encoding='utf-8')
        self.checkpoint_dir = self.root / 'state'
        self.publish_dir = self.root / 'publish'
        self.checkpoint = self.checkpoint_dir / 'checkpoint.json'
        env = {'SAKURA_MAIL_HOST': 'example.com', 'SAKURA_MAIL_USERNAME': 'user',
               'SAKURA_MAIL_PASSWORD': 'password', 'GEMINI_API_KEY': 'key',
               'TEAMS_WEBHOOK_URL': 'https://example.com/secret'}
        setting = patch.dict(os.environ, env, clear=True)
        setting.start()
        self.addCleanup(setting.stop)
        self.config = app.load_config(self.config_path)
        self.state = app.last_run_path(self.root / 'output', self.config)

    def execute(self):
        return scheduled.run(self.config_path, self.checkpoint_dir, self.publish_dir, 'https://github.com/o/r/actions/runs/1/artifacts/2')

    def seed(self):
        app.analysis_pipeline.atomic_json(self.checkpoint, {
            'mailbox': self.state.stem, 'last_run_at': '2026-10-01T09:15:00+09:00'})

    def test_prepare_does_not_notify_or_advance_checkpoint_before_upload(self):
        self.seed()
        before = self.checkpoint.read_bytes()
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])), \
                patch.object(scheduled, 'post_report') as post:
            scheduled.prepare(self.config_path, self.checkpoint_dir, self.publish_dir)
        post.assert_not_called()
        self.assertTrue((self.publish_dir / 'report.md').is_file())
        self.assertTrue((self.checkpoint_dir / 'pending.json').is_file())
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertEqual(json.loads(self.state.read_text())['last_run_at'], '2026-10-01T09:15:00+09:00')
        with patch.object(scheduled, 'post_report') as post:
            scheduled.notify(self.config_path, self.checkpoint_dir, self.publish_dir,
                             'https://github.com/o/r/actions/runs/1/artifacts/2')
        post.assert_called_once()
        self.assertNotEqual(self.checkpoint.read_bytes(), before)
        self.assertFalse((self.checkpoint_dir / 'pending.json').exists())

    def test_notification_failure_keeps_pending_state_and_previous_checkpoint(self):
        self.seed()
        before = self.checkpoint.read_bytes()
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])):
            scheduled.prepare(self.config_path, self.checkpoint_dir, self.publish_dir)
        with patch.object(scheduled, 'post_report', side_effect=app.ToolError('failed')):
            with self.assertRaises(app.ToolError):
                scheduled.notify(self.config_path, self.checkpoint_dir, self.publish_dir,
                                 'https://github.com/o/r/actions/runs/1/artifacts/2')
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertTrue((self.checkpoint_dir / 'pending.json').exists())

    def test_zero_mail_delivery_commits_and_next_run_restores_checkpoint(self):
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])), \
                patch.object(scheduled, 'post_report') as post:
            self.execute()
            self.assertEqual(post.call_args.args[0], 'https://github.com/o/r/actions/runs/1/artifacts/2')
        checkpoint = json.loads(self.checkpoint.read_text())
        self.assertEqual(checkpoint['mailbox'], self.state.stem)
        self.assertEqual(json.loads(self.state.read_text())['last_run_at'], checkpoint['last_run_at'])
        self.assertEqual(sorted(p.name for p in self.publish_dir.iterdir()),
                         ['priority_rules.json', 'ranked_emails.json', 'report.md'])
        self.seed()
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])) as fetch, \
                patch.object(scheduled, 'post_report'):
            self.execute()
            self.assertEqual(fetch.call_args.args[1].isoformat(), '2026-10-01T09:15:00+09:00')

    def test_delivery_failure_keeps_checkpoint_and_local_boundary(self):
        self.seed()
        before = self.checkpoint.read_bytes()
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])), \
                patch.object(scheduled, 'post_report', side_effect=app.ToolError('failed')):
            with self.assertRaises(app.ToolError):
                self.execute()
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertEqual(json.loads(self.state.read_text())['last_run_at'], '2026-10-01T09:15:00+09:00')

    def test_fetch_failure_preserves_checkpoint_without_posting(self):
        self.seed()
        before = self.checkpoint.read_bytes()
        with patch.object(app, 'iter_fetch_mails', side_effect=app.ToolError('failed')), \
                patch.object(scheduled, 'post_report') as post:
            with self.assertRaises(app.ToolError):
                self.execute()
            post.assert_not_called()
        self.assertEqual(self.checkpoint.read_bytes(), before)

    def test_env_fills_empty_host_and_username(self):
        self.assertEqual(self.config['imap']['host'], 'example.com')
        self.assertEqual(self.config['imap']['username'], 'user')

    def test_first_delivery_failure_creates_no_checkpoint_or_boundary(self):
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])), \
                patch.object(scheduled, 'post_report', side_effect=app.ToolError('failed')):
            with self.assertRaises(app.ToolError):
                self.execute()
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.state.exists())

    def test_bad_checkpoint_fails_before_fetch(self):
        self.checkpoint_dir.mkdir()
        for data in ('invalid', '{"mailbox":"another"}',
                     json.dumps({'mailbox': self.state.stem, 'last_run_at': 'invalid'})):
            self.checkpoint.write_text(data)
            with patch.object(app, 'iter_fetch_mails') as fetch, self.assertRaises(app.ToolError):
                self.execute()
            fetch.assert_not_called()

    def test_future_restored_checkpoint_does_not_corrupt_local_state(self):
        app.record_last_run(self.state, app.datetime(2026, 10, 1, 9, 15, tzinfo=app.JST))
        original = self.state.read_bytes()
        app.analysis_pipeline.atomic_json(self.checkpoint, {
            'mailbox': self.state.stem, 'last_run_at': '9999-01-01T00:00:00+09:00'})
        with patch.object(app, 'iter_fetch_mails') as fetch, self.assertRaises(app.ToolError):
            self.execute()
        fetch.assert_not_called()
        self.assertEqual(self.state.read_bytes(), original)

    def test_missing_webhook_or_secret_fails_before_fetch(self):
        for name in ('TEAMS_WEBHOOK_URL', 'GEMINI_API_KEY', 'SAKURA_MAIL_PASSWORD'):
            with patch.dict(os.environ, {name: ''}), patch.object(app, 'iter_fetch_mails') as fetch:
                with self.assertRaises(app.ToolError):
                    self.execute()
                fetch.assert_not_called()

    def test_embedded_credentials_rejected(self):
        self.config_path.write_text(self.config_path.read_text(encoding='utf-8').replace(
            '[imap]', '[imap]\npassword = "embedded"'), encoding='utf-8')
        with patch.object(app, 'iter_fetch_mails') as fetch, self.assertRaises(app.ToolError):
            self.execute()
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
