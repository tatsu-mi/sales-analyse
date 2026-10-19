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

    def test_titles_preserve_text_without_creating_markdown_links(self):
        ranked = {'ses': [{'rank': 1, 'latest': {'title': '[案件](https://example.com)\r\n*開発*'}}],
                  'contract': []}
        rules = {'top_n': 10, 'ses': {'label': 'SES'}, 'contract': {'label': '受託'}}
        payload = scheduled.card_payload('https://example.com/report', ranked, rules)
        text = json.loads(payload)['attachments'][0]['content']['body'][0]['text']
        self.assertIn(r'1. \[案件\]\(https://example\.com\)  \*開発\*', text)
        self.assertIn('受託（上位10件まで）\n\n該当候補はありません。', text)

    def card_texts(self, payloads):
        return [json.loads(payload)['attachments'][0]['content']['body'][0]['text']
                for payload in payloads]

    def test_split_keeps_projects_whole_and_preserves_all_text(self):
        projects = [f'### {i}位: 案件{i}\n' + ('日本語の案件詳細\\"\n' * 20)
                    for i in range(1, 5)]
        report = '# レポート\n\n## SES\n\n' + ''.join(projects) + '## 受託\n該当なし\n'
        with patch.object(scheduled, 'MAX_CARD_BYTES', 2100):
            payloads = scheduled.report_card_payloads('タイトル一覧', report)
        texts = self.card_texts(payloads)
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(payload) <= 2100 for payload in payloads))
        self.assertTrue(any(sum(project in text for project in projects) >= 2 for text in texts))
        for project in projects:
            self.assertEqual(sum(project in text for text in texts), 1)
        bodies = [text.split('\n\n', 1)[1] for text in texts]
        self.assertEqual(''.join(bodies), 'タイトル一覧\n\n' + report)
        for index, text in enumerate(texts, 1):
            self.assertTrue(text.startswith(f'メール分析レポート（{index}/{len(texts)}）'))

    def test_skill_sheet_top_ten_end_posts_even_with_capacity_remaining(self):
        sections = [
            f'## SES / {name}.pdf（上位10件まで）\n' + ''.join(
                f'### {rank}位: {name}の案件{rank}\n詳細\n' for rank in range(1, 11))
            for name in ('A', 'B')
        ]
        sections += ['## SES / C.pdf（上位10件まで）\n該当候補はありません。\n',
                     '## 受託（上位10件まで）\n### 1位: 受託案件\n詳細\n']
        report = '# レポート\n' + ''.join(sections)
        # The entire report could fit, but each sheet must end its own card.
        self.assertLess(len(scheduled.text_card_payload('一覧\n\n' + report)),
                        scheduled.MAX_CARD_BYTES)
        payloads = scheduled.report_card_payloads('一覧', report)
        bodies = [text.split('\n\n', 1)[1] for text in self.card_texts(payloads)]
        self.assertEqual(bodies, ['一覧\n\n# レポート\n' + sections[0], *sections[1:]])
        self.assertTrue(all(len(payload) <= scheduled.MAX_CARD_BYTES for payload in payloads))

    def test_mail_category_heading_does_not_force_a_new_post(self):
        report = ('## SES / A.pdf\n### 1位: 案件\n````text\n'
                  '## メール内の見出し\n```\n本文\n````\n'
                  '### 2位: 次の案件\n詳細\n')
        payloads = scheduled.report_card_payloads('一覧', report)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(self.card_texts(payloads)[0].split('\n\n', 1)[1],
                         '一覧\n\n' + report)

    def test_oversized_project_splits_without_loss_including_long_lines(self):
        for detail in ('長い日本語😀\\"' * 1500, '案件の詳細\n' * 1500):
            with self.subTest(detail=detail[:10]):
                project = '### 1位: 巨大案件\n' + detail + '\n'
                following = '### 2位: 小さい案件\n全体を一つに保つ\n'
                with patch.object(scheduled, 'MAX_CARD_BYTES', 1400):
                    payloads = scheduled.report_card_payloads('一覧', project + following)
                self.assertTrue(all(len(payload) <= 1400 for payload in payloads))
                texts = self.card_texts(payloads)
                self.assertEqual(''.join(text.split('\n\n', 1)[1] for text in texts),
                                 '一覧\n\n' + project + following)
                self.assertEqual(sum(following in text for text in texts), 1)

    def test_source_mail_headings_and_nested_fences_are_not_project_boundaries(self):
        first = ('## SES\n### 1位: 案件\n参照元メール\n````text\n'
                 '### 99位: メール内の見出し\n## 偽カテゴリ\n```\n本文\n````\n次の対応\n')
        second = '### 2位: 次の案件\n本文\n'
        self.assertEqual(scheduled.report_blocks(first + second), [first, second])

    def test_posts_full_report_in_order_and_rewrites_only_real_data_links(self):
        url = 'https://example.com/artifact'
        report = ('# レポート\n[優先メール](ranked_emails.json)\n'
                  '## SES\n### 1位: 案件\n```text\n'
                  '[原文](ranked_emails.json)\n```\n' + '詳細\n' * 1000)
        with patch.object(scheduled, 'MAX_CARD_BYTES', 1600), \
                patch.object(scheduled, 'send_card') as send:
            scheduled.post_report(url, 'https://example.com/webhook', 60, report=report)
        payloads = [call.args[0] for call in send.call_args_list]
        text = ''.join(self.card_texts(payloads))
        self.assertIn(f'[優先メール]({url})', text)
        self.assertIn('[原文](ranked_emails.json)', text)
        self.assertEqual(self.sleep.call_count, len(payloads) - 1)
        self.assertTrue(all(call.args == (10,) for call in self.sleep.call_args_list))

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
        (self.root / 'スキルシート').mkdir()
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

    def test_cli_test_notification_skips_email_analysis_and_all_state_changes(self):
        self.seed()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.dict(os.environ, {'TEAMS_WEBHOOK_URL': 'https://example.com/secret'}, clear=True), \
                patch.object(scheduled.sys, 'argv', ['scheduled_run.py', '--config', str(self.config_path), '--test-notification']), \
                patch.object(app, 'run', side_effect=AssertionError('mail analysis')), \
                patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('IMAP')), \
                patch.object(app, 'generate', side_effect=AssertionError('Gemini')), \
                patch.object(app, 'record_last_run', side_effect=AssertionError('state update')), \
                patch.object(scheduled, 'send_card') as send:
            self.assertEqual(scheduled.main(), 0)
        send.assert_called_once()
        card = json.loads(send.call_args.args[0])['attachments'][0]['content']
        self.assertEqual(card['body'][0]['text'], '【テスト投稿】ローカル環境からTeamsへの接続確認です。')
        self.assertNotIn('actions', card)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_test_notification_requires_webhook_without_exposing_it(self):
        with patch.dict(os.environ, {'TEAMS_WEBHOOK_URL': ''}), patch.object(scheduled, 'send_card') as send:
            with self.assertRaisesRegex(app.ToolError, 'TEAMS_WEBHOOK_URL'):
                scheduled.send_test_notification(self.config_path)
        send.assert_not_called()

    def test_cli_local_report_posts_all_parts_without_analysis_or_state_changes(self):
        self.seed()
        report_path = self.root / 'report.md'
        projects = [f'### {i}位: 案件{i}\n' + '日本語の詳細\n' * 15 for i in range(1, 7)]
        report = '# レポート\n## SES\n' + ''.join(projects)
        report_path.write_text(report, encoding='utf-8-sig')
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.dict(os.environ, {'TEAMS_WEBHOOK_URL': 'https://example.com/secret'}, clear=True), \
                patch.object(scheduled.sys, 'argv', ['scheduled_run.py', '--config', str(self.config_path),
                                                   '--test-report', str(report_path)]), \
                patch.object(app, 'run', side_effect=AssertionError('analysis')), \
                patch.object(app, 'iter_fetch_mails', side_effect=AssertionError('IMAP')), \
                patch.object(app, 'generate', side_effect=AssertionError('Gemini')), \
                patch.object(app, 'record_last_run', side_effect=AssertionError('state update')), \
                patch.object(scheduled, 'MAX_CARD_BYTES', 1700), \
                patch.object(scheduled.time, 'sleep') as sleep, \
                patch.object(scheduled, 'send_card') as send:
            self.assertEqual(scheduled.main(), 0)
        payloads = [call.args[0] for call in send.call_args_list]
        texts = [json.loads(p)['attachments'][0]['content']['body'][0]['text'] for p in payloads]
        self.assertGreater(len(payloads), 1)
        self.assertTrue(all(len(p) <= 1700 for p in payloads))
        self.assertEqual(sleep.call_count, len(payloads) - 1)
        self.assertTrue(all(call.args == (10,) for call in sleep.call_args_list))
        for i, text in enumerate(texts, 1):
            self.assertTrue(text.startswith(f'【テスト投稿】メール分析レポート（{i}/{len(texts)}）'))
        self.assertEqual(''.join(t.split('\n\n', 1)[1] for t in texts),
                         'ローカルのreport.mdからのテスト投稿です。\n\n' + report)
        for project in projects:
            self.assertEqual(sum(project in text for text in texts), 1)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                  for p in self.root.rglob('*') if p.is_file()})

    def test_local_report_missing_empty_or_invalid_utf8_never_sends(self):
        report_path = self.root / 'report.md'
        for content in (None, b'', b' \r\n', b'\xff\xfe'):
            with self.subTest(content=content):
                if content is not None:
                    report_path.write_bytes(content)
                with patch.object(scheduled, 'send_card') as send:
                    with self.assertRaises(app.ToolError):
                        scheduled.send_test_report(self.config_path, report_path)
                send.assert_not_called()

    def test_local_report_requires_webhook_and_stops_after_failed_part(self):
        report_path = self.root / 'report.md'
        report_path.write_text('### 1位: 案件\n' + '詳細\n' * 1000, encoding='utf-8')
        with patch.dict(os.environ, {'TEAMS_WEBHOOK_URL': ''}), \
                patch.object(scheduled, 'send_card') as send:
            with self.assertRaisesRegex(app.ToolError, 'TEAMS_WEBHOOK_URL'):
                scheduled.send_test_report(self.config_path, report_path)
        send.assert_not_called()
        with patch.object(scheduled, 'MAX_CARD_BYTES', 1600), \
                patch.object(scheduled.time, 'sleep'), \
                patch.object(app, 'record_last_run', side_effect=AssertionError('state update')), \
                patch.object(scheduled, 'send_card', side_effect=[None, app.ToolError('failed')]) as send:
            with self.assertRaisesRegex(app.ToolError, 'failed'):
                scheduled.send_test_report(self.config_path, report_path)
        self.assertEqual(send.call_count, 2)
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.state.exists())

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

    def test_failure_on_second_part_stops_delivery_and_preserves_checkpoint(self):
        self.seed()
        before = self.checkpoint.read_bytes()
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])):
            scheduled.prepare(self.config_path, self.checkpoint_dir, self.publish_dir)
        (self.publish_dir / 'report.md').write_text(
            '## SES\n' + ''.join(f'### {i}位: 案件\n' + '詳細\n' * 300 for i in range(5)),
            encoding='utf-8')
        with patch.object(scheduled, 'MAX_CARD_BYTES', 2000), \
                patch.object(scheduled.time, 'sleep'), \
                patch.object(scheduled, 'send_card', side_effect=[None, app.ToolError('failed')]) as send:
            with self.assertRaises(app.ToolError):
                scheduled.notify(self.config_path, self.checkpoint_dir, self.publish_dir,
                                 'https://github.com/o/r/actions/runs/1/artifacts/2')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertEqual(json.loads(self.state.read_text())['last_run_at'], '2026-10-01T09:15:00+09:00')
        self.assertTrue((self.checkpoint_dir / 'pending.json').exists())

    def test_notification_lists_report_titles_by_category_in_rank_order(self):
        with patch.object(app, 'iter_fetch_mails', return_value=iter([])):
            scheduled.prepare(self.config_path, self.checkpoint_dir, self.publish_dir)
        ranked = {
            kind: [{'rank': i, 'latest': {'title': f'{kind}案件{i}', 'budget': '非公開予算'},
                    'source_email': {'body': '非公開メール本文'}} for i in range(1, count + 1)]
            for kind, count in [('ses', 10), ('contract', 2)]
        }
        app.write_json(self.publish_dir / 'ranked_emails.json', ranked)
        rules = app.read_metadata(self.publish_dir / 'priority_rules.json')
        rules['ses']['label'] = '保存済みSES条件'
        app.write_json(self.publish_dir / 'priority_rules.json', rules)
        with patch.object(scheduled, 'send_card') as send:
            scheduled.notify(self.config_path, self.checkpoint_dir, self.publish_dir,
                             'https://github.com/o/r/actions/runs/1/artifacts/2')
        text = '\n'.join(json.loads(call.args[0])['attachments'][0]['content']['body'][0]['text']
                         for call in send.call_args_list)
        self.assertIn('[メール分析レポートを開く]', text)
        self.assertIn('保存済みSES条件（上位10件まで）', text)
        titles = [f'{i}. {kind}案件{i}' for kind in ('ses', 'contract')
                  for i in range(1, len(ranked[kind]) + 1)]
        positions = [text.index(title) for title in titles]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('非公開予算', text)
        self.assertNotIn('非公開メール本文', text)

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
