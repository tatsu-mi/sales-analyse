from pathlib import Path
import tempfile
import unittest
import contextlib
import io

import mail_analyzer
import prioritization
import validate_config


class MarkdownRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'rules.md'
        self.source = Path('priority_rules.example.md').read_text(encoding='utf-8')

    def load(self, text):
        self.path.write_text(text, encoding='utf-8-sig')
        return prioritization.load_rules(self.path)

    def test_markdown_maps_labels_criteria_and_settings(self):
        rules = self.load(self.source)
        self.assertEqual(rules['top_n'], 10)
        self.assertEqual(rules['ses']['label'], 'SES案件')
        self.assertEqual(len(rules['ses']['criteria']), 4)
        self.assertEqual(len(rules['contract']['criteria']), 4)
        self.assertEqual(rules['prefilter']['batch_size'], 100)
        self.assertTrue(rules['prefilter']['ai_enabled'])
        self.assertNotIn('`', rules['prefilter']['keep_patterns'][0])
        self.assertTrue(rules['prefilter']['ai_instructions'])

    def test_wrapped_criteria_comments_crlf_and_disabled_settings(self):
        rules = self.load(self.source.replace('## 表示件数\n\n10', '## 表示件数\n\n3')
                          .replace('- システム開発の募集である',
                                   '- システム開発の募集である\n  追加条件\n<!-- 非公開メモ -->')
                          .replace('機械選別: 有効', '機械選別：無効')
                          .replace('AI選別: 有効', 'AI選別: 無効').replace('\n', '\r\n'))
        self.assertEqual(rules['top_n'], 3)
        self.assertTrue(rules['ses']['criteria'][0].endswith('追加条件'))
        self.assertNotIn('非公開メモ', str(rules))
        self.assertFalse(rules['prefilter']['enabled'])
        self.assertFalse(rules['prefilter']['ai_enabled'])

    def test_invalid_rules_fail_without_echoing_private_content(self):
        variants = [self.source.replace('## 表示件数\n\n10', '## 表示件数\n\n11'),
                    self.source.replace('## 表示件数\n\n10', '## 表示件数\n\ntrue'),
                    self.source.replace('## SES:', '## private-secret:'),
                    self.source + '\n## 表示件数\n5\n',
                    self.source.replace('- 一括処理件数: 100', '- 一括処理件数: 101'),
                    self.source.replace('- AI選別: 有効', '- AI選別: private-secret'),
                    self.source.replace('- AI選別: 有効', ''),
                    self.source.replace('- AI選別: 有効', '- AI選別: 有効\n- AI選別: 無効'),
                    self.source.replace('## 除外する件名', '### 除外する件名'),
                    self.source.replace('- システム開発の募集である', 'private-secret'),
                    self.source.replace('## 優先して残す件名', '## 優先して残す件名\n\n- `[`'),
                    self.source[:self.source.index('## 件名選別への指示')]]
        for text in variants:
            with self.subTest(text=text[:30]), self.assertRaises(ValueError) as error:
                self.load(text)
            self.assertNotIn('private-secret', str(error.exception))

    def test_empty_criteria_and_invalid_utf8_fail(self):
        start = self.source.index('## SES:')
        end = self.source.index('## 受託:')
        with self.assertRaises(ValueError):
            self.load(self.source[:start] + '## SES: SES案件\n\n' + self.source[end:])
        self.path.write_bytes(b'\xff')
        with self.assertRaises(ValueError):
            prioritization.load_rules(self.path)

    def test_ci_can_validate_config_without_private_rules_but_runtime_cannot(self):
        config = Path(self.temp.name) / 'config.toml'
        config.write_text(Path('config.toml').read_text(encoding='utf-8'), encoding='utf-8')
        loaded = mail_analyzer.load_config(config, load_environment=False, load_priority_rules=False)
        self.assertNotIn('priority_rules', loaded)
        self.assertEqual(Path(loaded['analysis']['priority_rules_file']), config.parent / 'priority_rules.md')
        with self.assertRaises(mail_analyzer.ToolError):
            mail_analyzer.load_config(config, load_environment=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(validate_config.validate([config], structure_only=True), 0)

    def test_cli_rejects_invalid_markdown_without_showing_content(self):
        self.path.write_text('## private-secret\n', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(validate_config.validate([self.path]), 1)
        self.assertNotIn('private-secret', output.getvalue())
