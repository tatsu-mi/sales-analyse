from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from sales_analyze import html_report
from sales_analyze import mail_analyzer as app
from sales_analyze import prioritization


class ParsedReport(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.tags, self.links, self.source = [], [], []
        self.heading_ids = []
        self.details = []
        self.in_source = False
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'details':
            self.details.append(dict(attrs))
        if tag == 'a':
            self.links.append(dict(attrs)['href'])
        if tag == 'h3' and 'id' in dict(attrs):
            self.heading_ids.append(dict(attrs)['id'])
        if tag == 'pre':
            self.in_source = True

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.in_source = False

    def handle_data(self, data):
        if self.in_source:
            self.source.append(data)


class HtmlReportTests(unittest.TestCase):
    def test_summary_links_resolve_to_separate_details_for_each_engineer(self):
        rules = prioritization.load_rules('priority_rules.example.md')
        title = r'[案件](https://example.com) *開発* <確認> C:\work'
        entry = dict(rank=1, latest=dict(title=title, contact='', budget='', start='',
                     location='', skills=[], contract='', notes='', assessments=[]),
                     verification='適合', matches=0, total_criteria=0, sender='',
                     source_email={'body': '[本文](#project-1-1)\n### 見出し {#project-9-9}'})
        ranked = {'ses_by_engineer': [
            {'file': 'A.pdf', 'entries': [entry]},
            {'file': 'empty.pdf', 'entries': []},
            {'file': 'B.pdf', 'entries': [entry]}]}
        content = prioritization.render_report(ranked, rules)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            output.mkdir()
            app.save_report_files(output, content)
            for path in (output / 'report.html', output.parent / 'report.html'):
                report = path.read_text(encoding='utf-8')
                parsed = ParsedReport(report)
                self.assertEqual([link for link in parsed.links if link.startswith('#')],
                                 ['#project-1-1', '#project-3-1'])
                self.assertEqual(parsed.heading_ids, ['project-1-1', 'project-3-1'])
                self.assertIn('>[案件](https://example.com) *開発* &lt;確認&gt; C:\\work</a>', report)
                self.assertIn('[本文](#project-1-1)', ''.join(parsed.source))

    def test_headings_lists_tables_and_escaped_values_are_readable(self):
        report = html_report.render_report(
            '# メール分析レポート\n\n## 案件\n\n**候補**\n\n'
            '1. 案件\\[A\\] <確認>\n2. 案件B\n\n'
            '| 条件 | 判定 |\n| --- | --- |\n| C\\|C++ | 適合 |\n')
        parsed = ParsedReport(report)
        for tag in ('h1', 'h2', 'strong', 'ol', 'li', 'table', 'th', 'td'):
            self.assertIn(tag, parsed.tags)
        self.assertIn('案件[A] &lt;確認&gt;', report)
        self.assertIn('<td>C|C++</td>', report)
        self.assertIn('<meta charset="utf-8">', report)
        self.assertIn('<html lang="ja">', report)

    def test_source_email_preserves_literal_text_and_cannot_inject_html(self):
        mail = {'subject': '<script>alert(1)</script>', 'from': 'a&b@example.com',
                'to': '宛先', 'body': '\n  本文 & <img src=x onerror=alert(1)>\n\n'
                '```\n## 見出し\n**強調**\n[資料](ranked_emails.json)\n'}
        report = html_report.render_report(prioritization.render_source_mail(mail))
        parsed = ParsedReport(report)
        expected = ('件名: ' + mail['subject'] + '\n送信元: ' + mail['from']
                    + '\n宛先: 宛先\n\n' + mail['body'])
        self.assertEqual(''.join(parsed.source), expected)
        self.assertNotIn('script', parsed.tags)
        self.assertNotIn('img', parsed.tags)
        self.assertEqual(parsed.links, [])
        self.assertEqual(parsed.details, [{'class': 'source-mail-details'}])
        self.assertIn('<summary>参照元メール</summary><pre class="source-mail">', report)
        self.assertIn('</pre></details>', report)
        self.assertEqual(report.count('参照元メール'), 1)

    def test_unsafe_links_and_raw_html_cannot_execute(self):
        report = html_report.render_report(
            '[危険](javascript:alert) [外部](https://example.com/report)\n'
            '<svg onload="alert(1)">')
        parsed = ParsedReport(report)
        self.assertEqual(parsed.links, ['https://example.com/report'])
        self.assertNotIn('svg', parsed.tags)

    def test_history_and_latest_html_rewrite_only_generated_data_links(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            output.mkdir()
            source = prioritization.render_source_mail({'body': '[元の資料](ranked_emails.json)'})
            app.save_report_files(output, '[集計](ranked_emails.json)\n\n' + source)
            self.assertFalse((output / 'report.md').exists())
            self.assertFalse((output.parent / 'report.md').exists())
            history = ParsedReport((output / 'report.html').read_text(encoding='utf-8'))
            latest = ParsedReport((output.parent / 'report.html').read_text(encoding='utf-8'))
            self.assertEqual(history.links, ['ranked_emails.json'])
            self.assertEqual(latest.links, ['run/ranked_emails.json'])
            self.assertEqual(history.source, latest.source)
            self.assertEqual(history.details, [{'class': 'source-mail-details'}])
            self.assertEqual(latest.details, history.details)


if __name__ == '__main__':
    unittest.main()
