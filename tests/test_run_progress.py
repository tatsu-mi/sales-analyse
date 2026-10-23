import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_progress
import sharepoint_run as runner


class PublicProgressTests(unittest.TestCase):
    def test_cli_keeps_detail_output_separate_from_public_progress(self):
        details = io.StringIO()
        sensitive = 'private email body / engineer name / signed URL'

        def execute(*args, **kwargs):
            print(sensitive)
            print(sensitive, file=runner.sys.stderr)
            run_progress.report('fetching')
            run_progress.report('fetched_count', count=25)

        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as public, \
                patch.object(runner.sys, 'argv', ['sharepoint_run.py', '--progress-fd', str(public.fileno())]), \
                patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}), \
                patch.object(runner, 'Store'), patch.object(Path, 'mkdir'), \
                patch.object(runner, 'execute', side_effect=execute), \
                contextlib.redirect_stdout(details), contextlib.redirect_stderr(details):
            self.assertEqual(runner.main(), 0)
            public.seek(0)
            text = public.read()
        self.assertIn('メール取得中', text)
        self.assertIn('25通', text)
        self.assertNotIn(sensitive, text)
        self.assertIn(sensitive, details.getvalue())
        self.assertIsNone(run_progress.stream)

    def test_public_progress_rejects_text_supplied_as_counts(self):
        public = io.StringIO()
        with patch.object(run_progress, 'stream', public):
            with self.assertRaises(ValueError):
                run_progress.report('fetched_count', count='private body\n::warning::injected')
        self.assertEqual(public.getvalue(), '')

    def test_extraction_progress_distinguishes_send_split_and_saved(self):
        public = io.StringIO()
        with patch.object(run_progress, 'stream', public):
            run_progress.report('extract_request', done=0, total=542, skipped=0,
                                count=20, tokens=12000, attempt=1)
            run_progress.report('extract_split_output', done=0, total=542, skipped=0, count=20)
            run_progress.report('extract_split_invalid', done=0, total=542, skipped=0, count=10)
            run_progress.report('extract_saved', done=5, total=542, skipped=0, count=5)
        lines = public.getvalue().splitlines()
        self.assertIn('今回の20通をAIへ送信', lines[0])
        self.assertIn('12,000トークン', lines[0])
        self.assertIn('試行 1回目', lines[0])
        self.assertIn('トークン上限', lines[1])
        self.assertIn('検証できない', lines[2])
        self.assertIn('5 / 542通完了', lines[3])


if __name__ == '__main__':
    unittest.main()
