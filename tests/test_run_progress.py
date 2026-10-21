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


if __name__ == '__main__':
    unittest.main()
