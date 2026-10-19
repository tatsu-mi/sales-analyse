import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import analysis_pipeline as pipeline
import mail_analyzer as app
import prioritization as priority


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.rules = priority.load_rules("priority_rules.toml")

    def candidate(self, uid, kind="ses", states=None, received="2026-10-15T12:00:00+09:00"):
        states = states or ["match"] * len(self.rules[kind]["criteria"])
        project = dict.fromkeys(pipeline.TEXT_FIELDS, "")
        project.update(title="開発案件", kind=kind, status="open", skills=["Python"],
                       assessments=[{"criterion": i, "state": state, "evidence": "本文の根拠"}
                                    for i, state in enumerate(states, 1)])
        return {"uid": str(uid), "source_uids": [str(uid)], "kind": kind, "latest": project,
                "sender": "sales@example.invalid", "received_at": received,
                "source_email": {"subject": "開発案件", "from": "sales@example.invalid", "to": "receiver@example.invalid",
                                 "body": "参照元メール本文", "body_truncated": False},
                "expired_at_period_end": False}

    def test_top_ten_independently_and_no_duplicate_mails(self):
        values = [self.candidate(i, kind) for kind in priority.KINDS for i in range(15)]
        values += [self.candidate(14), self.candidate(14)]
        result = priority.shortlist(values, self.rules)
        for kind in priority.KINDS:
            self.assertEqual(len(result[kind]), 10)
            self.assertEqual(len({value["uid"] for value in result[kind]}), 10)
            self.assertEqual([value["rank"] for value in result[kind]], list(range(1, 11)))

    def test_mismatch_closed_expired_unknown_kind_are_excluded(self):
        mismatch = self.candidate(1, states=["match"] * (len(self.rules['ses']['criteria']) - 1) + ["mismatch"])
        closed, expired, other = (self.candidate(i) for i in range(2, 5))
        closed["latest"]["status"] = "closed"
        expired["expired_at_period_end"] = True
        other["kind"] = "unknown"
        result = priority.shortlist([mismatch, closed, expired, other], self.rules)
        self.assertEqual(result, {"ses": [], "contract": []})

    def test_complete_match_beats_newer_unknown_and_unknown_is_labeled(self):
        complete = self.candidate(1, received="2026-10-15T01:00:00+09:00")
        unknown = self.candidate(2, states=["match"] * (len(self.rules['ses']['criteria']) - 1) + ["unknown"])
        result = priority.shortlist([unknown, complete], self.rules)["ses"]
        self.assertEqual([value["uid"] for value in result], ["1", "2"])
        self.assertEqual([value["verification"] for value in result], ["適合", "要確認"])
        report = priority.render_report({"ses": result, "contract": []}, self.rules)
        self.assertIn("未確認の案件条件を問い合わせる", report)
        self.assertNotIn("本文の根拠", report)
        self.assertNotIn("| 条件 |", report)

    def test_best_project_represents_a_mail_in_each_category(self):
        weak = self.candidate(1, states=["unknown"] * len(self.rules['ses']['criteria']))
        strong, contract = self.candidate(1), self.candidate(1, "contract")
        result = priority.shortlist([weak, strong, contract], self.rules)
        self.assertEqual(result["ses"][0]["matches"], len(self.rules['ses']['criteria']))
        self.assertEqual(result["contract"][0]["matches"], 4)

    def test_recent_and_explicitly_open_break_ties(self):
        old = self.candidate(1, received="2026-10-15T01:00:00+09:00")
        recent, unknown = self.candidate(2), self.candidate(3)
        unknown["latest"]["status"] = "unknown"
        result = priority.shortlist([recent, unknown, old], self.rules)["ses"]
        self.assertEqual([value["uid"] for value in result], ["2", "1", "3"])

    def test_missing_invalid_or_unsupported_assessments_stop_extraction(self):
        base = self.candidate(1)["latest"]
        cases = []
        for change in (lambda p: p.pop("assessments"),
                       lambda p: p["assessments"].pop(),
                       lambda p: p["assessments"][0].update(criterion=2),
                       lambda p: p["assessments"][0].update(evidence=""),
                       lambda p: p["assessments"][0].update(state="yes")):
            project = copy.deepcopy(base)
            change(project)
            cases.append(project)
        for project in cases:
            answer = {"records": [{"uid": "1", "kind": "ses", "tags": [],
                                   "market_notes": "", "opportunities": [project]}]}
            with self.subTest(project=project), self.assertRaises(pipeline.InvalidExtraction):
                pipeline.validate_extraction(json.dumps(answer), [{"uid": "1"}], self.rules)

    def test_other_cannot_smuggle_opportunities_into_ranking(self):
        answer = {"records": [{"uid": "1", "kind": "other", "tags": [], "market_notes": "",
                               "opportunities": [self.candidate(1)["latest"]]}]}
        with self.assertRaises(pipeline.InvalidExtraction):
            pipeline.validate_extraction(json.dumps(answer), [{"uid": "1"}], self.rules)

    def test_rules_validation_rejects_invalid_limits_and_empty_criteria(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "rules.toml"
            for text in ('top_n = 11', 'top_n = true', 'top_n = 10\n[ses]\ncriteria = []'):
                source.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    priority.load_rules(source)


class SourceMailTests(unittest.TestCase):
    def test_source_is_literal_and_fence_cannot_be_closed_by_mail(self):
        mail = {"subject": "案件 <確認>", "from": "sender@example.com", "to": "receiver@example.com",
                "body": "元本文\r\n\r\n```\r\n# 見出し\r\n  空白と|記号 <tag>", "body_truncated": False}
        rendered = priority.render_source_mail(mail)
        self.assertIn('````text\n', rendered)
        self.assertIn('元本文\n\n```\n# 見出し\n  空白と|記号 <tag>', rendered)
        self.assertIn('件名: 案件 <確認>', rendered)
        self.assertNotIn('一部省略', rendered)

    def test_truncated_source_is_explicit(self):
        self.assertIn('一部省略', priority.render_source_mail({'body': '保存部分', 'body_truncated': True}))

    def test_latest_report_rewrites_artifact_links_but_preserves_source(self):
        source = priority.render_source_mail({'body': '[元の資料](ranked_emails.json)'})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'run'
            output.mkdir()
            app.save_report_files(output, '[集計](ranked_emails.json)\n\n' + source)
            latest = (Path(temp) / 'report.md').read_text(encoding='utf-8')
            self.assertIn('[集計](run/ranked_emails.json)', latest)
            self.assertIn(source, latest)

    def test_report_uses_source_mail_in_place_of_uid_row(self):
        fixture = PriorityTests()
        fixture.setUp()
        value = fixture.candidate(123)
        value.update(rank=1, matches=7, total_criteria=7, verification='適合')
        report = priority.render_report({'ses': [value], 'contract': []}, fixture.rules)
        self.assertIn('参照元メール本文', report)
        self.assertIn('宛先: receiver@example.invalid', report)
        self.assertNotIn('UID:', report)
        self.assertNotIn('関連UID:', report)


class TimeWindowTests(unittest.TestCase):
    def setUp(self):
        for setting in (patch.object(app.environment, "load_env"), patch.dict(app.os.environ, {
                "SAKURA_MAIL_HOST": "example.invalid", "SAKURA_MAIL_USERNAME": "test@example.invalid",
                "SAKURA_MAIL_PASSWORD": "test-password"}, clear=True)):
            setting.start()
            self.addCleanup(setting.stop)

    def test_imap_filters_12_hour_boundaries_by_internaldate_before_reading_body(self):
        config = app.load_config(Path("config.example.toml"))
        start, stop = app.analysis_period({}, now=datetime(2026, 10, 15, 9, tzinfo=app.JST))
        instants = [start - timedelta(seconds=1), start, stop - timedelta(seconds=1),
                    stop, stop + timedelta(seconds=1)]
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.return_value = client.select.return_value = ("OK", [])
        read_uids = []

        def uid(command, *args):
            if command == "search":
                return "OK", [b"0 1 2 3 4"]
            number = int(args[0])
            if args[1] == "(INTERNALDATE RFC822.SIZE)":
                # Mixed timezone representation: filter absolute instants, not header dates.
                value = instants[number].astimezone(timezone.utc).strftime("%d-%b-%Y %H:%M:%S %z")
                return "OK", [f'1 (INTERNALDATE "{value}" RFC822.SIZE 100)'.encode()]
            read_uids.append(number)
            return "OK", [(b"literal", b"Subject: test\r\nDate: Tue, 1 Jan 2000 00:00:00 +0000\r\n\r\nbody")]

        client.uid.side_effect = uid
        with patch.object(app.imaplib, "IMAP4_SSL", return_value=client):
            mails = list(app.iter_fetch_mails(config, start, stop))
        self.assertEqual([mail["uid"] for mail in mails], ["2", "1"])
        self.assertEqual(read_uids, [2, 1])
        client.select.assert_called_once_with('"INBOX"', readonly=True)

    def test_default_24_hours_crosses_midnight_and_ignores_old_config_dates(self):
        stop = datetime(2026, 10, 15, 5, 30, tzinfo=app.JST)
        start, end = app.analysis_period({"start_date": "2026-09-01", "end_date": "2026-09-30"}, now=stop)
        self.assertEqual(end, stop)
        self.assertEqual(start, datetime(2026, 10, 14, 5, 30, tzinfo=app.JST))
        self.assertEqual(end - start, timedelta(hours=24))

    def test_custom_window_explicit_period_and_timezone(self):
        now = datetime(2026, 10, 15, 0, tzinfo=timezone.utc)
        start, stop = app.analysis_period({"lookback_hours": 6}, now=now)
        self.assertEqual(stop.hour, 9)
        self.assertEqual(stop - start, timedelta(hours=6))
        self.assertEqual(app.analysis_period({}, "2026-10-01", "2026-10-02"),
                         app.period("2026-10-01", "2026-10-02"))

    def test_saved_window_keeps_exact_instants_and_rejects_bad_metadata(self):
        meta = {"start_at": "2026-10-14T21:00:00+09:00", "stop_at": "2026-10-15T09:00:00+09:00"}
        start, stop = app.saved_period(meta)
        self.assertEqual(stop - start, timedelta(hours=12))
        for bad in ({"start_at": meta["start_at"]}, dict(meta, stop_at="2026-10-15T09:00:00"),
                    dict(meta, stop_at=meta["start_at"])):
            with self.assertRaises(app.ToolError):
                app.saved_period(bad)


if __name__ == "__main__":
    unittest.main()
