import io
import json
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from sales_analyze import mail_analyzer as app


class MailTests(unittest.TestCase):
    def setUp(self):
        for setting in (patch.object(app.environment, "load_env"), patch.dict(app.os.environ, {
                "SAKURA_MAIL_HOST": "example.invalid", "SAKURA_MAIL_USERNAME": "test@example.invalid",
                "SAKURA_MAIL_PASSWORD": "test-password", "GEMINI_API_KEY": "test-key"}, clear=True)):
            setting.start()
            self.addCleanup(setting.stop)
        profiles = tempfile.TemporaryDirectory()
        self.addCleanup(profiles.cleanup)
        self.engineers_file = Path(profiles.name) / "engineers.md"
        self.engineers_file.write_text("## Test Engineer\n\nPython development", encoding="utf-8")
        # All integration tests use the live pipeline with deterministic API responses.
        limits = patch.object(app, "model_limits", return_value={"inputTokenLimit": 1048576, "outputTokenLimit": 65536})
        count = patch.object(app, "count_input_tokens", return_value=100)
        pace = patch.object(app.analysis_pipeline.RateLimiter, "reserve")
        for mock in (limits, count, pace):
            mock.start()
            self.addCleanup(mock.stop)

    def analysis_config(self):
        config = app.load_config(Path("config.example.toml"))
        config["analysis"]["engineers_file"] = str(self.engineers_file)
        return config

    @staticmethod
    def analysis_answer(mails, final):
        extraction = json.dumps({"records": [{"uid": mail["uid"], "kind": "other", "tags": [],
                                                "market_notes": "",
                                                "opportunities": []} for mail in mails]})
        screening = json.dumps({"records": [{"uid": mail["uid"], "exclude": False} for mail in mails]})
        return [screening, extraction, final]

    def test_logging_level_controls_console_and_file(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(app.LOGGER, "handlers", []), \
                patch.object(app.LOGGER, "disabled", False), \
                patch.object(app.LOGGER, "propagate", False), \
                patch.object(app.LOGGER, "level", app.logging.INFO), \
                patch.object(app.sys, "stderr", new_callable=io.StringIO) as stderr:
            path = Path(temp) / "execution.log"
            try:
                for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                    with self.subTest(level=level):
                        stderr.seek(0)
                        stderr.truncate()
                        app.configure_logging({"logging": {"level": level}}, path)
                        for message_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                            app.LOGGER.log(getattr(app.logging, message_level), "%s message", message_level)
                        text = stderr.getvalue()
                        for message_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                            self.assertEqual(message_level + " message" in text,
                                             getattr(app.logging, message_level) >= getattr(app.logging, level))
                        self.assertTrue(path.read_text(encoding="utf-8").endswith(text))
                        for line in text.splitlines():
                            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ")
                app.configure_logging({}, path)
                app.LOGGER.debug("hidden detail")
                app.LOGGER.info("progress first line\nprogress second line")
                text = stderr.getvalue()
                self.assertNotIn("hidden detail", text)
                for line in text.splitlines()[-2:]:
                    self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO progress ")
            finally:
                for handler in app.LOGGER.handlers:
                    handler.close()

    def test_logging_config_validation_and_default(self):
        source = Path("config.example.toml").read_text(encoding="utf-8").replace(
            'priority_rules_file = "priority_rules.example.md"',
            'priority_rules_file = "' + Path("priority_rules.example.md").resolve().as_posix() + '"')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                path.write_text(source.replace('level = "INFO"', f'level = "{level}"'), encoding="utf-8")
                self.assertEqual(app.load_config(path)["logging"]["level"], level)
            for invalid in ('true', '10', '"TRACE"', '"info"', '[]', '{}'):
                path.write_text(source.replace('level = "INFO"', 'level = ' + invalid), encoding="utf-8")
                with self.assertRaisesRegex(app.ToolError, "logging.level"):
                    app.load_config(path)
            path.write_text(source.replace("[logging]\n", "", 1).replace('level = "INFO"', "", 1), encoding="utf-8")
            self.assertEqual(app.load_config(path)["logging"]["level"], "INFO")
            path.write_text(source.replace('level = "INFO"', 'enabled = true'), encoding="utf-8")
            with self.assertRaises(app.ToolError):
                app.load_config(path)

    def test_password_uses_only_environment(self):
        with patch.dict(app.os.environ, {"SAKURA_MAIL_PASSWORD": "environment-password"}, clear=True):
            self.assertEqual(app.secret({"password": " config-password "}, "password"), "environment-password")

    def test_password_falls_back_to_fixed_environment(self):
        with patch.dict(app.os.environ, {"SAKURA_MAIL_PASSWORD": "environment-password"}, clear=True):
            for settings in ({}, {"password": ""}, {"password": "   "}):
                self.assertEqual(app.secret(settings, "password"), "environment-password")
        with patch.dict(app.os.environ, {}, clear=True):
            with self.assertRaisesRegex(app.ToolError, "SAKURA_MAIL_PASSWORD"):
                app.secret({}, "password")

    def test_gemini_key_uses_only_environment(self):
        with patch.dict(app.os.environ, {"GEMINI_API_KEY": "environment-key"}, clear=True):
            self.assertEqual(app.secret({"api_key": "config-key"}, "api_key"), "environment-key")

    def test_gemini_key_falls_back_to_fixed_environment(self):
        with patch.dict(app.os.environ, {"GEMINI_API_KEY": "environment-key", "OTHER_KEY": "other-key"}, clear=True):
            for value in ("", "   "):
                self.assertEqual(app.secret({"api_key": value}, "api_key"), "environment-key")
            self.assertEqual(app.secret({}, "api_key"), "environment-key")
            # Legacy environment-name settings do not change the fixed fallback.
            self.assertEqual(app.secret({"api_key_env": "OTHER_KEY"}, "api_key"), "environment-key")

    def test_gemini_key_missing_reports_fixed_environment_name(self):
        with patch.dict(app.os.environ, {}, clear=True):
            with self.assertRaisesRegex(app.ToolError, "GEMINI_API_KEY"):
                app.secret({"api_key": ""}, "api_key")

    def test_japan_period_includes_both_dates(self):
        start, stop = app.period("2026-09-01", "2026-09-30")
        self.assertEqual(start.isoformat(), "2026-09-01T00:00:00+09:00")
        self.assertEqual(stop.isoformat(), "2026-10-01T00:00:00+09:00")
        with self.assertRaises(app.ToolError):
            app.period("2026-09-30", "2026-09-01")

    def test_internal_date_timezone_and_space_padded_day(self):
        received = app.internal_date(b'1 (INTERNALDATE " 1-Sep-2026 02:00:00 +0000")')
        self.assertEqual(received.isoformat(), "2026-09-01T11:00:00+09:00")

    def test_japanese_multipart_prefers_plain_and_ignores_attachment(self):
        message = EmailMessage()
        message["Subject"] = "お見積もり"
        message.set_content("納期を教えてください。", charset="iso-2022-jp")
        message.add_alternative("<p>HTML本文</p>", subtype="html")
        message.add_attachment(b"secret attachment", maintype="application", subtype="octet-stream", filename="quote.pdf")
        decoded = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(str(decoded["Subject"]), "お見積もり")
        self.assertEqual(app.mail_body(decoded), "納期を教えてください。")

    def test_html_fallback(self):
        message = EmailMessage()
        message.set_content("<style>hidden</style><p>見積 &amp; 納期</p><script>bad()</script>", subtype="html")
        self.assertEqual(app.mail_body(message), "見積 & 納期")

    def test_imap_boundary_and_readonly(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-01")
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.return_value = ("OK", [b"Logged in"])
        client.select.return_value = ("OK", [b"3"])
        dates = {b"1": "31-Aug-2026 14:59:59 +0000", b"2": "31-Aug-2026 15:00:00 +0000",
                 b"3": "01-Sep-2026 15:00:00 +0000"}
        raw = b"Subject: hello\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody"

        def uid(command, *args):
            if command == "search":
                return "OK", [b"1 2 3"]
            if args[1] == "(INTERNALDATE RFC822.SIZE)":
                return "OK", [f'1 (INTERNALDATE "{dates[args[0]]}" RFC822.SIZE 100)'.encode()]
            self.assertEqual(args[1], "(BODY.PEEK[])")
            self.assertIn(args[0], (b"2", b"3"))
            return "OK", [(b"literal", raw), b")"]

        client.uid.side_effect = uid
        with patch.object(app.imaplib, "IMAP4_SSL", return_value=client), patch.dict(app.os.environ, {"SAKURA_MAIL_PASSWORD": "test-password"}, clear=True):
            mails = app.fetch_mails(config, start, stop)
        client.select.assert_called_once_with('"INBOX"', readonly=True)
        self.assertEqual([m["uid"] for m in mails], ["2"])

        config["analysis"]["max_emails"] = 1
        dates[b"3"] = "01-Sep-2026 14:59:59 +0000"
        client.uid.reset_mock()
        with patch.object(app.imaplib, "IMAP4_SSL", return_value=client), patch.dict(app.os.environ, {"SAKURA_MAIL_PASSWORD": "test-password"}, clear=True):
            mails = app.fetch_mails(config, start, stop)
        self.assertEqual([m["uid"] for m in mails], ["3"])
        self.assertEqual(client.uid.call_count, 3)  # SEARCH, metadata, body; no older fetch.

    def test_imap_limit_counts_only_in_period_and_sorts_uids_numerically(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-01")
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.return_value = client.select.return_value = ("OK", [])
        dates = {b"20": "01-Sep-2026 15:00:00 +0000",  # Outside the exact period.
                 b"10": "01-Sep-2026 14:00:00 +0000",
                 b"9": "01-Sep-2026 13:00:00 +0000",
                 b"2": "31-Aug-2026 15:00:00 +0000"}
        read_uids, metadata_uids = [], []

        def uid(command, *args):
            if command == "search":
                return "OK", [b"9 2 20 10"]
            if args[1] == "(INTERNALDATE RFC822.SIZE)":
                metadata_uids.append(args[0])
                return "OK", [f'1 (INTERNALDATE "{dates[args[0]]}" RFC822.SIZE 100)'.encode()]
            read_uids.append(args[0])
            return "OK", [(b"literal", b"Subject: test\r\n\r\nbody")]

        client.uid.side_effect = uid
        for limit, expected in ((1, [b"10"]), (2, [b"10", b"9"]),
                                (3, [b"10", b"9", b"2"]), (4, [b"10", b"9", b"2"])):
            with self.subTest(limit=limit):
                config["analysis"]["max_emails"] = limit
                read_uids.clear()
                metadata_uids.clear()
                with patch.object(app.imaplib, "IMAP4_SSL", return_value=client):
                    mails = list(app.iter_fetch_mails(config, start, stop))
                self.assertEqual([mail["uid"] for mail in mails], [value.decode() for value in expected])
                self.assertEqual(read_uids, expected)
                self.assertEqual(metadata_uids, [b"20"] + expected)

    def test_imap_connection_failure_has_reason_and_traceback(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-01")
        with patch.object(app.imaplib, "IMAP4_SSL", side_effect=OSError(11001, "getaddrinfo failed")), \
                self.assertLogs(app.LOGGER, level="DEBUG") as logs:
            with self.assertRaisesRegex(app.ToolError, "SSL/TLS接続.*OSError.*getaddrinfo failed"):
                app.fetch_mails(config, start, stop)
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assertTrue(any(line.startswith("DEBUG:") and "Traceback" in line for line in logs.output))
        self.assertNotIn("test-password", "\n".join(logs.output))

    def test_imap_skips_bad_metadata_or_missing_body_and_continues(self):
        config = self.analysis_config()
        config["analysis"]["max_emails"] = 1
        config["analysis"]["max_message_bytes"] = 100
        start, stop = app.period("2026-09-01", "2026-09-01")
        good = b'1 (INTERNALDATE "01-Sep-2026 00:00:00 +0000" RFC822.SIZE 100)'
        cases = (
            (good.replace(b'SIZE 100', b'SIZE 101'), False, "max_message_bytes"),
            (good.replace(b' RFC822.SIZE 100', b''), False, "メールサイズ"),
            (b'1 (RFC822.SIZE 100)', False, "受信日時"),
            (good.replace(b'Sep', b'Bad'), False, "KeyError"),
            (good.replace(b'00:00:00', b'99:00:00'), False, "ValueError"),
            (b'', False, "受信日時"),
            (good, True, "本文を取得できません"),
        )
        for metadata, missing_body, reason in cases:
            with self.subTest(reason=reason, metadata=metadata):
                client = MagicMock()
                client.__enter__.return_value = client
                client.login.return_value = client.select.return_value = ("OK", [])

                def uid(command, *args):
                    if command == "search":
                        return "OK", [b"41 42 43"]
                    if args[1] == "(INTERNALDATE RFC822.SIZE)":
                        return "OK", [metadata if args[0] == b"43" else good]
                    return "OK", [None] if missing_body and args[0] == b"43" else [
                        (b"literal", b"Subject: valid\r\n\r\nbody")]

                client.uid.side_effect = uid
                with patch.object(app.imaplib, "IMAP4_SSL", return_value=client), \
                        self.assertLogs(app.LOGGER, level="INFO") as logs:
                    mails = list(app.iter_fetch_mails(config, start, stop))
                self.assertEqual([mail["uid"] for mail in mails], ["42"])
                output = "\n".join(logs.output)
                self.assertIn("WARNING:mail_analyzer:IMAP メールをスキップ", output)
                self.assertIn("UID=43", output)
                self.assertIn(reason, output)
                self.assertIn("対象=1件 スキップ=1件", output)
                if not missing_body:
                    self.assertNotIn(unittest.mock.call("fetch", b"43", "(BODY.PEEK[])"),
                                     client.uid.call_args_list)
                self.assertNotIn(unittest.mock.call("fetch", b"41", "(INTERNALDATE RFC822.SIZE)"),
                                 client.uid.call_args_list)

    def test_imap_fetch_transport_failures_stop_processing(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-01")
        for error in (OSError("connection lost"), app.imaplib.IMAP4.abort("closed"),
                      app.imaplib.IMAP4.error("bad command")):
            for fetch_body in (False, True):
                with self.subTest(error=type(error).__name__, fetch_body=fetch_body):
                    client = MagicMock()
                    client.__enter__.return_value = client
                    client.login.return_value = client.select.return_value = ("OK", [])
                    responses = [("OK", [b"42 41"])]
                    if fetch_body:
                        responses.append(("OK", [b'1 (INTERNALDATE "01-Sep-2026 00:00:00 +0000" RFC822.SIZE 100)']))
                    client.uid.side_effect = responses + [error]
                    with patch.object(app.imaplib, "IMAP4_SSL", return_value=client), \
                            self.assertLogs(app.LOGGER, level="ERROR"), \
                            self.assertRaises(app.ToolError):
                        list(app.iter_fetch_mails(config, start, stop))

    def test_imap_authentication_failure_redacts_credentials(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-01")
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.side_effect = app.imaplib.IMAP4.error(
            f"AUTHENTICATIONFAILED {config['imap']['username']} test-password")
        with patch.object(app.imaplib, "IMAP4_SSL", return_value=client), \
                self.assertLogs(app.LOGGER, level="INFO") as logs:
            with self.assertRaises(app.ToolError) as failure:
                app.fetch_mails(config, start, stop)
        output = "\n".join(logs.output) + str(failure.exception)
        self.assertIn("処理=認証", output)
        self.assertIn("AUTHENTICATIONFAILED", output)
        self.assertNotIn("test-password", output)
        self.assertNotIn(config["imap"]["username"], output)

    def test_imap_failure_reports_uid_and_server_response_without_literal(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-01")
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.return_value = ("OK", [])
        client.select.return_value = ("OK", [b"1"])
        client.uid.side_effect = [("OK", [b"42"]), ("NO", [b"Permission denied", (b"literal", b"private mail body")])]
        with patch.object(app.imaplib, "IMAP4_SSL", return_value=client), \
                self.assertLogs(app.LOGGER, level="INFO") as logs:
            with self.assertRaisesRegex(app.ToolError, "UID=42.*status=NO.*Permission denied"):
                app.fetch_mails(config, start, stop)
        self.assertNotIn("private mail body", "\n".join(logs.output))

    def test_mail_parse_failure_skips_and_does_not_log_raw_content(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-01")
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.return_value = ("OK", [])
        client.select.return_value = ("OK", [b"1"])
        client.uid.side_effect = [
            ("OK", [b"42 41"]),
            ("OK", [b'1 (INTERNALDATE "01-Sep-2026 00:00:00 +0000" RFC822.SIZE 100)']),
            ("OK", [(b"literal", b"private mail body")]),
            ("OK", [b'1 (INTERNALDATE "01-Sep-2026 00:00:00 +0000" RFC822.SIZE 100)']),
            ("OK", [(b"literal", b"Subject: valid\r\n\r\nbody")])]
        error = UnicodeEncodeError("ascii", "private mail body", 0, 17, "ordinal not in range(128)")
        with patch.object(app.imaplib, "IMAP4_SSL", return_value=client), \
                patch.object(app, "mail_body", side_effect=[error, "valid body"]), \
                self.assertLogs(app.LOGGER, level="INFO") as logs:
            mails = app.fetch_mails(config, start, stop)
        self.assertEqual([mail["uid"] for mail in mails], ["41"])
        output = "\n".join(logs.output)
        self.assertIn("メール解析, UID=42, UnicodeEncodeError", output)
        self.assertIn("スキップ=1件", output)
        self.assertNotIn("private mail body", output)

    def test_api_retry_and_key_not_in_url(self):
        settings = {"model": "test-model", "max_output_tokens": 8192, "timeout_seconds": 10, "retries": 1}
        response = io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": "analysis"}]}}]}).encode())
        error = HTTPError("https://example.com", 429, "limited", {}, None)
        before_request = MagicMock()
        with patch.object(app, "urlopen", side_effect=[error, response]) as mocked, patch.object(app.time, "sleep"):
            self.assertEqual(app.generate(settings, "secret-key", "prompt", before_request=before_request), "analysis")
        self.assertEqual(before_request.call_count, 2)
        request = mocked.call_args[0][0]
        self.assertNotIn("secret-key", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "secret-key")

    def test_incomplete_answer_is_failure(self):
        response = io.BytesIO(b'{"candidates":[{"finishReason":"MAX_TOKENS","content":{"parts":[{"text":"partial"}]}}]}')
        settings = {"model": "test", "max_output_tokens": 1, "timeout_seconds": 10, "retries": 1}
        with patch.object(app, "urlopen", return_value=response), self.assertRaises(app.ToolError):
            app.generate(settings, "key", "prompt")

    def test_429_respects_google_retry_delay(self):
        settings = {"model": "test", "max_output_tokens": 100, "timeout_seconds": 10, "retries": 1}
        body = {"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "55.5s"},
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                {"quotaMetric": "generativelanguage.googleapis.com/generate_content_input_tokens",
                 "quotaId": "GenerateContentInputTokensPerModelPerMinute", "quotaValue": "250000"}]}]}}
        error = HTTPError("https://example.com", 429, "limited", {}, io.BytesIO(json.dumps(body).encode()))
        response = io.BytesIO(b'{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"OK"}]}}]}')
        with patch.object(app, "urlopen", side_effect=[error, response]), \
                patch.object(app.random, "uniform", return_value=0), patch.object(app.time, "sleep") as sleep:
            self.assertEqual(app.generate(settings, "key", "private prompt"), "OK")
            sleep.assert_called_once_with(55.5)

    def test_429_daily_or_zero_quota_stops_without_retry_and_hides_server_message(self):
        settings = {"model": "test", "max_output_tokens": 100, "timeout_seconds": 10, "retries": 3}
        for quota_id, quota_value in [("GenerateRequestsPerDay", "20"), ("TokensPerMinute", "0")]:
            body = {"error": {"message": "private email body secret-key", "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                    {"quotaMetric": "metric/secret-key", "quotaId": quota_id, "quotaValue": quota_value}]}]}}
            error = HTTPError("https://example.com", 429, "limited", {}, io.BytesIO(json.dumps(body).encode()))
            with self.subTest(quota_id=quota_id), patch.object(app, "urlopen", side_effect=error) as urlopen, \
                    patch.object(app.time, "sleep") as sleep:
                with self.assertRaises(app.ToolError) as failure:
                    app.generate(settings, "secret-key", "private email body")
                self.assertIn(quota_id, str(failure.exception))
                self.assertIn("日次上限または割当量0", str(failure.exception))
                self.assertNotIn("secret-key", str(failure.exception))
                self.assertNotIn("private email body", str(failure.exception))
                urlopen.assert_called_once()
                sleep.assert_not_called()

    def test_429_retry_after_and_exhausted_details(self):
        settings = {"model": "test", "max_output_tokens": 100, "timeout_seconds": 10, "retries": 0}
        error = HTTPError("https://example.com", 429, "limited", {"Retry-After": "60"}, io.BytesIO(b"invalid JSON"))
        with patch.object(app, "urlopen", side_effect=error), patch.object(app.time, "sleep") as sleep:
            with self.assertRaisesRegex(app.ToolError, "60.0秒"):
                app.generate(settings, "key", "prompt")
            sleep.assert_not_called()


    def test_upload_streams_exact_bytes_and_polls_until_active(self):
        settings = {"timeout_seconds": 10}
        uri = "https://generativelanguage.googleapis.com/v1beta/files/test"
        processing = {"name": "files/test", "uri": uri, "state": "PROCESSING"}
        active = dict(processing, state="ACTIVE", sizeBytes="55", mimeType="text/plain")
        start_response = io.BytesIO(b"")
        start_response.headers = {"X-Goog-Upload-URL": "https://generativelanguage.googleapis.com/upload/v1beta/files?upload_id=test"}
        sent = []
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "emails.json"
            original = '[ {"uid": "1", "body": "日本語"} ]\n'.encode("utf-8")
            source.write_bytes(original)

            def request_response(request, **unused):
                sent.append(request)
                if len(sent) == 1:
                    return start_response
                if len(sent) == 2:
                    self.assertTrue(hasattr(request.data, "read"))
                    self.assertEqual(request.data.read(8) + request.data.read(), original)
                    self.assertEqual(request.get_header("Content-length"), str(len(original)))
                    self.assertEqual(request.get_header("X-goog-upload-command"), "upload, finalize")
                    return io.BytesIO(json.dumps({"file": processing}).encode())
                return io.BytesIO(json.dumps(active).encode())

            with patch.object(app, "urlopen", side_effect=request_response), patch.object(app.time, "sleep") as sleep:
                result = app.upload_mail_file(settings, "secret-key", source)
            self.assertEqual(result["uri"], uri)
            self.assertEqual(result["mimeType"], "text/plain")
            self.assertEqual(len(sent), 3)
            self.assertEqual(sent[0].get_header("X-goog-upload-header-content-type"), "text/plain")
            self.assertEqual(sent[0].get_header("X-goog-api-key"), "secret-key")
            self.assertTrue(sent[1].data.closed)
            self.assertNotIn("secret-key", sent[0].full_url)
            sleep.assert_called_once()

    def test_generate_references_file_without_inlining_contents(self):
        settings = {"model": "test", "max_output_tokens": 100, "timeout_seconds": 10, "retries": 0}
        uploaded = {"uri": "https://generativelanguage.googleapis.com/v1beta/files/test", "mimeType": "text/plain"}
        response = io.BytesIO(b'{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"OK"}]}}]}')
        with patch.object(app, "urlopen", return_value=response) as urlopen:
            self.assertEqual(app.generate(settings, "key", "Analyze the file.", file_data=uploaded), "OK")
        parts = json.loads(urlopen.call_args[0][0].data)["contents"][0]["parts"]
        self.assertEqual(parts, [{"text": "Analyze the file."}, {"fileData": {
            "mimeType": "text/plain", "fileUri": uploaded["uri"]}}])

    def test_upload_rejects_untrusted_url_and_failed_processing(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "emails.jsonl"
            source.write_text("test", encoding="utf-8")
            start = io.BytesIO(b"")
            start.headers = {"X-Goog-Upload-URL": "https://example.com/upload"}
            with patch.object(app, "urlopen", return_value=start) as urlopen:
                with self.assertRaisesRegex(app.ToolError, "有効なアップロード先"):
                    app.upload_mail_file({"timeout_seconds": 10}, "key", source)
                urlopen.assert_called_once()
            start = io.BytesIO(b"")
            start.headers = {"X-Goog-Upload-URL": "https://generativelanguage.googleapis.com/upload/v1beta/files"}
            failed = {"file": {"name": "files/test", "uri": "https://generativelanguage.googleapis.com/v1beta/files/test",
                               "state": "FAILED", "error": {"message": "private body"}}}
            with patch.object(app, "urlopen", side_effect=[start, io.BytesIO(json.dumps(failed).encode())]):
                with self.assertRaisesRegex(app.ToolError, "ファイル処理に失敗") as failure:
                    app.upload_mail_file({"timeout_seconds": 10}, "key", source)
                self.assertNotIn("private body", str(failure.exception))

    def test_file_upload_failure_preserves_local_mail_and_marks_failure(self):
        config = self.analysis_config()
        mails = [{"uid": "1", "body": "saved", "body_truncated": False}]
        with tempfile.TemporaryDirectory() as temp:
            config["analysis"]["output_dir"] = temp
            args = SimpleNamespace(config="config.example.toml", start=None, end=None, fetch_only=False)
            with patch.object(app, "load_config", return_value=config), \
                    patch.object(app, "iter_fetch_mails", return_value=iter(mails)), \
                    patch.object(app, "upload_mail_file", side_effect=app.ToolError("upload failure")), \
                    patch.object(app, "generate") as generate:
                with self.assertRaisesRegex(app.ToolError, "upload failure"):
                    app.run(args)
                generate.assert_not_called()
            run_dir = next(Path(temp).glob("*/metadata.json")).parent
            self.assertEqual(list(app.iter_saved_mails(run_dir / "emails.jsonl")), mails)
            self.assertEqual(app.read_metadata(run_dir / "metadata.json")["status"], "analysis_failed")

    def test_file_input_rejection_stops_without_retry(self):
        settings = {"model": "test", "max_output_tokens": 100, "timeout_seconds": 10, "retries": 3}
        error = HTTPError("https://example.com", 400, "bad request", {}, io.BytesIO(b"{}"))
        with patch.object(app, "urlopen", side_effect=error) as urlopen, patch.object(app.time, "sleep") as sleep:
            with self.assertRaisesRegex(app.ToolError, "ファイル経由でも入力上限"):
                app.generate(settings, "key", "prompt", file_data={"uri": "file-uri", "mimeType": "text/plain"})
            urlopen.assert_called_once()
            sleep.assert_not_called()

    def test_fetch_only_never_calls_gemini(self):
        config = self.analysis_config()
        with tempfile.TemporaryDirectory() as temp:
            config["analysis"]["output_dir"] = temp
            args = SimpleNamespace(config="config.example.toml", start=None, end=None, fetch_only=True)
            with patch.object(app, "load_config", return_value=config), patch.object(app, "iter_fetch_mails", return_value=iter([])), patch.object(app, "generate") as generate:
                app.run(args)
            generate.assert_not_called()
            run_dir = next(Path(temp).glob("*/metadata.json")).parent
            self.assertTrue((run_dir / "emails.jsonl").exists())
            self.assertFalse((run_dir / "report.html").exists())

    def test_mails_and_aggregated_report_are_sent_as_files(self):
        config = self.analysis_config()
        mails = [{"uid": "1", "body": "first", "body_truncated": False},
                 {"uid": "2", "body": "second", "body_truncated": True}]
        with tempfile.TemporaryDirectory() as temp:
            config["analysis"]["output_dir"] = temp
            args = SimpleNamespace(config="config.example.toml", start=None, end=None, fetch_only=False)
            with patch.object(app, "load_config", return_value=config), \
                    patch.object(app, "iter_fetch_mails", return_value=iter(mails)), \
                    patch.object(app, "upload_mail_file", return_value={"uri": "https://generativelanguage.googleapis.com/v1beta/files/test", "mimeType": "text/plain"}) as upload, \
                    patch.object(app, "generate", side_effect=self.analysis_answer(mails, "統合結果")) as generate:
                app.run(args)
                self.assertEqual(upload.call_count, 2)
                self.assertEqual(generate.call_count, 2)
                titles = [json.loads(line) for line in upload.call_args_list[0][0][2].read_text(encoding="utf-8").splitlines()]
                self.assertTrue(all(set(item) == {"uid", "subject"} for item in titles))
                self.assertEqual(list(app.iter_saved_mails(upload.call_args_list[-1][0][2])), mails)
            run_dir = next(Path(temp).glob("*/metadata.json")).parent
            self.assertIn("該当候補はありません", (run_dir / "report.html").read_text(encoding="utf-8"))
            meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "complete")
            self.assertEqual(meta["truncated_bodies"], 1)

    def test_fetch_saves_each_mail_before_next_and_keeps_partial_on_failure(self):
        config = self.analysis_config()
        first = {"uid": "1", "body": "saved immediately", "body_truncated": False}
        with tempfile.TemporaryDirectory() as temp:
            config["analysis"]["output_dir"] = temp
            args = SimpleNamespace(config="config.example.toml", start=None, end=None, fetch_only=True)

            def interrupted_fetch(*unused):
                yield first
                partial = next(Path(temp).glob("*/emails.partial.jsonl"))
                self.assertEqual(json.loads(partial.read_text(encoding="utf-8")), first)
                raise app.ToolError("connection lost")

            with patch.object(app, "load_config", return_value=config), \
                    patch.object(app, "iter_fetch_mails", side_effect=interrupted_fetch), \
                    patch.object(app, "generate") as generate:
                with self.assertRaisesRegex(app.ToolError, "connection lost"):
                    app.run(args)
            generate.assert_not_called()
            run_dir = next(Path(temp).glob("*/metadata.json")).parent
            meta = app.read_metadata(run_dir / "metadata.json")
            self.assertEqual(meta["status"], "fetch_failed")
            self.assertEqual(meta["count"], 1)
            self.assertFalse((run_dir / "emails.jsonl").exists())
            self.assertIsNone(app.find_saved_mails(Path(temp), meta["dataset_key"]))

    def test_auto_reuse_and_refresh(self):
        config = self.analysis_config()
        mails = [{"uid": "1", "body": "from disk", "body_truncated": False}]
        with tempfile.TemporaryDirectory() as temp:
            config["analysis"]["output_dir"] = temp
            args = SimpleNamespace(config="config.example.toml", start="2026-10-01", end="2026-10-02", fetch_only=True, refresh=False)
            with patch.object(app, "load_config", return_value=config), \
                    patch.object(app, "iter_fetch_mails", return_value=iter(mails)) as fetch:
                app.run(args)
                fetch.assert_called_once()
            # Reusing a complete file does not require a mail password or IMAP connection.
            args.fetch_only = False
            with patch.object(app, "load_config", return_value=config), \
                    patch.dict(app.os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True), \
                    patch.object(app.imaplib, "IMAP4_SSL") as imap, \
                    patch.object(app, "iter_fetch_mails") as fetch, \
                    patch.object(app, "upload_mail_file", return_value={"uri": "https://generativelanguage.googleapis.com/v1beta/files/test", "mimeType": "text/plain"}) as upload, \
                    patch.object(app, "generate", side_effect=self.analysis_answer(mails, "disk analysis")) as generate:
                app.run(args)
                imap.assert_not_called()
                fetch.assert_not_called()
                self.assertEqual(upload.call_count, 2)
                self.assertEqual(list(app.iter_saved_mails(upload.call_args_list[-1][0][2])), mails)
                self.assertIn("file_data", generate.call_args.kwargs)
            args.refresh = True
            args.fetch_only = True
            with patch.object(app, "load_config", return_value=config), \
                    patch.object(app, "iter_fetch_mails", return_value=iter(mails)) as fetch:
                app.run(args)
                fetch.assert_called_once()

    def test_explicit_legacy_file_uses_source_period_and_skips_imap(self):
        config = self.analysis_config()
        mails = [{"uid": "1", "body": "legacy", "body_truncated": False}]
        with tempfile.TemporaryDirectory() as temp:
            legacy_dir = Path(temp) / "legacy"
            legacy_dir.mkdir()
            source = legacy_dir / "emails.json"
            app.write_json(source, mails)
            app.write_json(legacy_dir / "metadata.json", {
                "start_date": "2026-08-01", "end_date": "2026-08-02", "status": "analysis_failed",
                "count": 1, "truncated_bodies": 0, "folder": "INBOX"})
            config["analysis"]["output_dir"] = str(Path(temp) / "reports")
            args = SimpleNamespace(config="config.example.toml", start=None, end=None,
                                   fetch_only=False, emails_file=str(source))
            with patch.object(app, "load_config", return_value=config), \
                    patch.object(app, "iter_fetch_mails") as fetch, \
                    patch.object(app, "upload_mail_file", return_value={"uri": "https://generativelanguage.googleapis.com/v1beta/files/test", "mimeType": "text/plain"}) as upload, \
                    patch.object(app, "generate", side_effect=self.analysis_answer(mails, "imported")) as generate:
                app.run(args)
                fetch.assert_not_called()
                self.assertEqual(upload.call_count, 2)
                self.assertEqual(list(app.iter_saved_mails(upload.call_args_list[-1][0][2])), mails)
                self.assertIn("2026-08-01", generate.call_args[0][2])
                self.assertNotIn("legacy", generate.call_args[0][2])
                self.assertEqual(generate.call_count, 2)
                self.assertIn("file_data", generate.call_args.kwargs)
            run_dir = next((Path(temp) / "reports").glob("*/metadata.json")).parent
            self.assertEqual(list(app.iter_saved_mails(run_dir / "emails.jsonl")), mails)
            self.assertIsNone(app.read_metadata(run_dir / "metadata.json")["dataset_key"])

    def test_cache_identity_changes_with_mailbox_period_and_limits(self):
        config = self.analysis_config()
        start, stop = app.period("2026-09-01", "2026-09-02")
        key = app.dataset_key(config, start, stop)
        for field in ("host", "username", "folder"):
            original = config["imap"][field]
            config["imap"][field] = "another"
            self.assertNotEqual(app.dataset_key(config, start, stop), key)
            config["imap"][field] = original
        other_start, other_stop = app.period("2026-09-01", "2026-09-03")
        self.assertNotEqual(app.dataset_key(config, other_start, other_stop), key)
        for field in ("max_body_chars", "max_emails"):
            config["analysis"][field] += 1
            self.assertNotEqual(app.dataset_key(config, start, stop), key)
            config["analysis"][field] -= 1

    def test_saved_file_invalid_or_incomplete_is_rejected_before_api(self):
        config = self.analysis_config()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "emails.jsonl"
            source.write_text('{"uid":"1","body":"ok","body_truncated":false}\n', encoding="utf-8")
            meta_path = Path(temp) / "metadata.json"
            config["analysis"]["output_dir"] = str(Path(temp) / "reports")
            args = SimpleNamespace(config="config.example.toml", start=None, end=None,
                                   fetch_only=False, emails_file=str(source))
            meta = {"start_date": "2026-09-01", "end_date": "2026-09-02",
                    "status": "fetched", "count": 1, "truncated_bodies": 0}
            cases = [("fetch_failed", 1, None), ("fetched", 2, None),
                     ("fetched", 1, "{invalid}\n"), ("fetched", 1, '{"uid":"1"}\n')]
            for status, count, content in cases:
                with self.subTest(status=status, count=count, content=content):
                    meta.update(status=status, count=count)
                    app.write_json(meta_path, meta)
                    if content is not None:
                        source.write_text(content, encoding="utf-8")
                    with patch.object(app, "load_config", return_value=config), \
                            patch.object(app, "iter_fetch_mails") as fetch, \
                            patch.object(app, "generate") as generate:
                        with self.assertRaises(app.ToolError):
                            app.run(args)
                        fetch.assert_not_called()
                        generate.assert_not_called()

    def test_legacy_json_streams_large_records_and_rejects_bad_syntax(self):
        mails = [{"uid": str(i), "body": "日本語" * 30000, "body_truncated": False} for i in range(3)]
        self.assertEqual(list(app.iter_json_array(io.StringIO(json.dumps(mails, ensure_ascii=False)))), mails)
        self.assertEqual(list(app.iter_json_array(io.StringIO(" [ ] \n"))), [])
        for source in ("", "{}", "[{}", "[{},]", "[]garbage", "[{} {}]"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                list(app.iter_json_array(io.StringIO(source)))


    def test_api_failure_preserves_emails_and_marks_failure(self):
        config = self.analysis_config()
        mails = [{"uid": "1", "body": "saved", "body_truncated": False}]
        with tempfile.TemporaryDirectory() as temp:
            config["analysis"]["output_dir"] = temp
            args = SimpleNamespace(config="config.example.toml", start=None, end=None, fetch_only=False)
            with patch.object(app, "load_config", return_value=config), patch.object(app, "iter_fetch_mails", return_value=iter(mails)), \
                    patch.object(app, "upload_mail_file", return_value={"uri": "https://generativelanguage.googleapis.com/v1beta/files/test", "mimeType": "text/plain"}), \
                    patch.object(app, "generate", side_effect=app.ToolError("API failure")):
                with self.assertRaises(app.ToolError):
                    app.run(args)
            run_dir = next(Path(temp).glob("*/metadata.json")).parent
            self.assertEqual(list(app.iter_saved_mails(run_dir / "emails.jsonl")), mails)
            self.assertEqual(json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))["status"], "analysis_failed")
            self.assertFalse((run_dir / "report.html").exists())


if __name__ == "__main__":
    unittest.main()
