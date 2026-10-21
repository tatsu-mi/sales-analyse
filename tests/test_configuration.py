import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import environment
import mail_analyzer as app
import validate_config


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.env'

    def test_quotes_unicode_special_characters_and_existing_env(self):
        self.path.write_text('# comment\nSAKURA_MAIL_PASSWORD="  secret # $ ` \\\\  "\n'
                             "SINGLE='日本語#${VALUE}'\nexport RAW=abc#123 # note\nEMPTY=\n", encoding='utf-8-sig')
        with patch.dict(os.environ, {'RAW': 'github-secret'}, clear=True):
            environment.load_env(self.path)
            self.assertEqual(os.environ['SAKURA_MAIL_PASSWORD'], '  secret # $ ` \\  ')
            self.assertEqual(os.environ['SINGLE'], '日本語#${VALUE}')
            self.assertEqual(os.environ['RAW'], 'github-secret')
            self.assertEqual(os.environ['EMPTY'], '')

    def test_invalid_file_does_not_change_environment_or_expose_value(self):
        for line in ('BROKEN', 'SECRET="private-secret', 'SECRET="private-secret" trailing',
                     'SECRET=private-secret\nSECRET=duplicate'):
            self.path.write_text('FIRST=valid\n' + line, encoding='utf-8')
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError) as error:
                    environment.load_env(self.path)
                self.assertNotIn('private-secret', str(error.exception))
                self.assertNotIn('FIRST', os.environ)

    def test_missing_file_is_optional(self):
        environment.load_env(self.path)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.toml'
        self.source = Path('config.example.toml').read_text(encoding='utf-8')
        rules = (Path.cwd() / 'priority_rules.example.md').as_posix()
        self.source = self.source.replace('priority_rules_file = "priority_rules.example.md"', f'priority_rules_file = "{rules}"')
        self.path.write_text(self.source, encoding='utf-8')

    def load(self):
        return app.load_config(self.path, load_environment=False)

    def test_default_mail_limit_is_2000(self):
        self.assertEqual(self.load()['analysis']['max_emails'], 2000)
        self.path.write_text(self.source.replace('max_emails = 2000\n', ''), encoding='utf-8')
        self.assertEqual(self.load()['analysis']['max_emails'], 2000)

    def test_engineer_file_is_resolved_relative_to_config(self):
        self.assertEqual(Path(self.load()['analysis']['engineers_file']), self.root / 'engineers.md')
        for invalid in ('""', 'false', '123'):
            self.path.write_text(self.source.replace('engineers_file = "engineers.md"',
                                                   f'engineers_file = {invalid}'), encoding='utf-8')
            with self.subTest(invalid=invalid), self.assertRaises(app.ToolError):
                self.load()

    def test_offline_validation_needs_no_credentials_and_never_reads_env_or_network(self):
        (self.root / '.env').write_text('intentionally invalid', encoding='utf-8')
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(app.environment, 'load_env', side_effect=AssertionError('dotenv read')), \
                patch.object(app, 'urlopen', side_effect=AssertionError('network')), \
                patch.object(app.imaplib, 'IMAP4_SSL', side_effect=AssertionError('IMAP')):
            config = self.load()
            self.assertNotIn('host', config['imap'])
            self.assertNotIn('username', config['imap'])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(validate_config.validate([self.path]), 0)

    def test_runtime_loads_dotenv_relative_to_config_and_environment_wins(self):
        (self.root / '.env').write_text('SAKURA_MAIL_HOST=dotenv.example\nSAKURA_MAIL_USERNAME=user\n'
                                      'SAKURA_MAIL_PASSWORD=" pass "\nGEMINI_API_KEY=local-key\n', encoding='utf-8')
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'github-key'}, clear=True):
            config = app.load_config(self.path)
            self.assertEqual(config['imap']['host'], 'dotenv.example')
            self.assertEqual(config['imap']['username'], 'user')
            self.assertEqual(app.secret({}, 'password'), ' pass ')
            self.assertEqual(app.secret({}, 'api_key'), 'github-key')

    def test_old_secret_fields_rejected_even_when_empty(self):
        for section, field in [('imap', 'host'), ('imap', 'username'), ('imap', 'password'),
                               ('gemini', 'api_key'), ('teams', 'webhook_url')]:
            source = self.source
            if f'[{section}]' not in source:
                source += f'\n[{section}]\n'
            source = source.replace(f'[{section}]', f'[{section}]\n{field} = ""')
            self.path.write_text(source, encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(app.ToolError):
                self.load()

    def test_invalid_types_ranges_typos_and_dates(self):
        invalid = [self.source.replace('port = 993', 'port = 65536'),
                   self.source.replace('port = 993', 'port = true'),
                   self.source.replace('model = "gemini-3.5-flash-lite"', 'model = 123'),
                   self.source.replace('max_emails = 2000', 'max_emails = 0'),
                   self.source.replace('lookback_hours = 24', 'lookback_hour = 24'),
                   self.source.replace('[imap]', '[impa]'),
                   self.source.replace('retry_max_wait_seconds = 120', 'retry_max_wait_seconds = 1'),
                   self.source + '\n[teams]\ntimeout_seconds = -1\n',
                   self.source + '\n[teams]\ntimeout_seconds = true\n',
                   self.source.replace('[analysis]', '[analysis]\nstart_date = "bad"'),
                   self.source.replace('[analysis]', '[analysis]\nstart_date = "20261015"'),
                   self.source.replace('[analysis]', '[analysis]\nstart_date = "2026-10-15"\nend_date = "2026-10-01"')]
        for source in invalid:
            self.path.write_text(source, encoding='utf-8')
            with self.subTest(source=source[:50]), self.assertRaises(app.ToolError):
                self.load()

    def test_cli_reports_failures_without_printing_toml_values(self):
        self.path.write_text('private-secret = "unterminated', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(validate_config.validate([self.path]), 1)
        self.assertNotIn('private-secret', output.getvalue())
