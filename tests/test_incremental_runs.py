import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mail_analyzer as app


class FrozenDatetime(datetime):
    instant = datetime(2026, 10, 15, 9, tzinfo=app.JST)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


class IncrementalRunTests(unittest.TestCase):
    def setUp(self):
        for setting in (patch.object(app.environment, "load_env"),
                        patch.dict(app.os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True)):
            setting.start()
            self.addCleanup(setting.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = app.load_config(Path("config.example.toml"))
        engineers = self.root / 'engineers.md'
        engineers.write_text('## A\n\nPython', encoding='utf-8')
        self.config["analysis"]["engineers_file"] = str(engineers)
        self.config["analysis"]["output_dir"] = str(self.root)
        self.config["logging"]["level"] = "WARNING"
        self.args = SimpleNamespace(config="config.example.toml", start=None, end=None,
                                    fetch_only=False, emails_file=None, refresh=False)
        FrozenDatetime.instant = datetime(2026, 10, 15, 9, tzinfo=app.JST)
        for setting in (patch.object(app, "load_config", return_value=self.config),
                        patch.object(app, "datetime", FrozenDatetime)):
            setting.start()
            self.addCleanup(setting.stop)
        self.state = app.last_run_path(self.root, self.config)

    def seed(self, instant=None):
        app.record_last_run(self.state, instant or FrozenDatetime.instant - timedelta(hours=20))

    def last_run(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["last_run_at"]

    def test_first_run_uses_configured_lookback_then_previous_boundary(self):
        first = FrozenDatetime.instant
        (self.root / "report.md").write_text("previous report", encoding="utf-8")
        with patch.object(app, "iter_fetch_mails", return_value=iter([])) as fetch:
            app.run(self.args)
            self.assertEqual(fetch.call_args.args[1:],
                             (first - timedelta(hours=self.config["analysis"]["lookback_hours"]), first))
        self.assertEqual(self.last_run(), first.isoformat())
        self.assertIn("対象メールは0件", (self.root / "report.md").read_text(encoding="utf-8"))
        FrozenDatetime.instant += timedelta(hours=25)
        with patch.object(app, "iter_fetch_mails", return_value=iter([])) as fetch:
            app.run(self.args)
            self.assertEqual(fetch.call_args.args[1:], (first, FrozenDatetime.instant))
        self.assertEqual(self.last_run(), FrozenDatetime.instant.isoformat())

    def test_explicit_period_neither_reads_nor_updates_incremental_state(self):
        self.seed()
        original = self.state.read_bytes()
        self.args.start, self.args.end = "2026-10-01", "2026-10-02"
        with patch.object(app, "incremental_period", side_effect=AssertionError("state read")), \
                patch.object(app.analysis_pipeline, "atomic_json", side_effect=AssertionError("state write")), \
                patch.object(app, "iter_fetch_mails", return_value=iter([])) as fetch:
            app.run(self.args)
            self.assertEqual(fetch.call_args.args[1:], app.period(self.args.start, self.args.end))
        self.assertEqual(self.state.read_bytes(), original)
        # A corrupt incremental record must not prevent explicit period execution.
        self.state.write_text("invalid", encoding="utf-8")
        with patch.object(app, "iter_fetch_mails", return_value=iter([])):
            app.run(self.args)
        self.assertEqual(self.state.read_text(encoding="utf-8"), "invalid")

    def test_fetch_failure_keeps_previous_boundary(self):
        self.seed()
        (self.root / "report.md").write_text("previous report", encoding="utf-8")
        original = self.last_run()
        with patch.object(app, "iter_fetch_mails", side_effect=app.ToolError("fetch failed")):
            with self.assertRaises(app.ToolError):
                app.run(self.args)
        self.assertEqual(self.last_run(), original)
        self.assertEqual((self.root / "report.md").read_text(encoding="utf-8"), "previous report")

    def test_analysis_failure_keeps_previous_boundary_and_success_uses_start_time(self):
        self.seed()
        original = self.last_run()
        mail = {"uid": "1", "body": "mail", "body_truncated": False}
        with patch.object(app, "iter_fetch_mails", return_value=iter([mail])), \
                patch.object(app.analysis_pipeline, "analyze", side_effect=app.ToolError("analysis failed")):
            with self.assertRaises(app.ToolError):
                app.run(self.args)
        self.assertEqual(self.last_run(), original)
        self.args.refresh = True
        start_time = FrozenDatetime.instant

        def analyze(*args):
            FrozenDatetime.instant += timedelta(minutes=15)
            return "report"

        with patch.object(app, "iter_fetch_mails", return_value=iter([mail])), \
                patch.object(app.analysis_pipeline, "analyze", side_effect=analyze):
            app.run(self.args)
        self.assertEqual(self.last_run(), start_time.isoformat())

    def test_successful_fetch_only_updates_boundary(self):
        self.args.fetch_only = True
        with patch.object(app, "iter_fetch_mails", return_value=iter([])):
            app.run(self.args)
        self.assertEqual(self.last_run(), FrozenDatetime.instant.isoformat())
        self.assertFalse((self.root / "report.md").exists())

    def test_latest_report_overwrites_and_links_to_matching_history(self):
        metadata = {"start_date": "2026-10-15", "end_date": "2026-10-15",
                    "count": 1, "truncated_bodies": 0}
        for name in ("first", "second"):
            output = self.root / name
            output.mkdir()
            app.write_report(output, metadata, f"{name} report")
            history = (output / "report.md").read_text(encoding="utf-8")
            latest = (self.root / "report.md").read_text(encoding="utf-8")
            self.assertIn(f"{name} report", latest)
            self.assertIn("](ranked_emails.json)", history)
            self.assertIn(f"]({name}/ranked_emails.json)", latest)
        self.assertNotIn("first report", latest)
        self.assertIn("first report", (self.root / "first" / "report.md").read_text(encoding="utf-8"))

    def test_saved_file_reanalysis_neither_reads_nor_updates_state(self):
        source = self.root / "source" / "emails.jsonl"
        source.parent.mkdir()
        source.write_text("", encoding="utf-8")
        app.write_json(source.parent / "metadata.json", {
            "start_date": "2026-10-01", "end_date": "2026-10-02", "status": "fetched",
            "count": 0, "truncated_bodies": 0})
        self.seed()
        original = self.state.read_bytes()
        self.args.emails_file = str(source)
        with patch.object(app, "incremental_period", side_effect=AssertionError("state read")), \
                patch.object(app, "iter_fetch_mails", side_effect=AssertionError("IMAP fetch")):
            app.run(self.args)
        self.assertEqual(self.state.read_bytes(), original)

    def test_bad_or_future_record_stops_without_overwriting_it(self):
        self.state.parent.mkdir()
        for content in ("invalid", "null", "[]", json.dumps({"last_run_at": "2026-10-15T10:00:00+09:00"}),
                        json.dumps({"last_run_at": "2026-10-15T08:00:00"})):
            self.state.write_text(content, encoding="utf-8")
            with patch.object(app, "iter_fetch_mails") as fetch, self.assertRaises(app.ToolError):
                app.run(self.args)
            fetch.assert_not_called()
            self.assertEqual(self.state.read_text(encoding="utf-8"), content)

    def test_same_second_run_does_not_create_empty_reversed_period(self):
        self.seed(FrozenDatetime.instant)
        with patch.object(app, "iter_fetch_mails") as fetch:
            app.run(self.args)
        fetch.assert_not_called()
        self.assertFalse(list(self.root.glob("*/metadata.json")))

    def test_different_mailboxes_have_separate_records(self):
        for key in ("host", "username", "folder", "port"):
            original = self.config["imap"][key]
            self.config["imap"][key] = 994 if key == "port" else "another"
            self.assertNotEqual(app.last_run_path(self.root, self.config), self.state)
            self.config["imap"][key] = original


if __name__ == "__main__":
    unittest.main()
