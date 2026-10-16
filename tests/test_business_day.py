import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import check_business_day as calendar


HOLIDAYS = ('国民の祝日・休日月日,国民の祝日・休日名称\r\n'
            '2026/1/1,元日\r\n'
            '2026/5/6,休日\r\n'
            '2026/9/22,休日\r\n'
            '2026/11/3,文化の日\r\n').encode('cp932')


class BusinessDayTests(unittest.TestCase):
    def setUp(self):
        self.response = MagicMock()
        self.response.__enter__.return_value.read.return_value = HOLIDAYS
        setting = patch.object(calendar, 'urlopen', return_value=self.response)
        self.fetch = setting.start()
        self.addCleanup(setting.stop)

    def test_holiday_substitute_and_citizens_holiday_are_skipped(self):
        for month, day in ((1, 1), (5, 6), (9, 22), (11, 3)):
            with self.subTest(month=month, day=day):
                self.assertFalse(calendar.business_day(
                    datetime(2026, month, day, 7, tzinfo=calendar.JST))[0])

    def test_jst_date_is_used_even_on_previous_utc_day(self):
        # Monday in UTC has already become Culture Day Tuesday in Japan.
        self.assertFalse(calendar.business_day(
            datetime(2026, 11, 2, 22, tzinfo=timezone.utc))[0])
        self.assertTrue(calendar.business_day(
            datetime(2026, 10, 18, 22, tzinfo=timezone.utc))[0])

    def test_weekend_skips_without_network(self):
        for day in (17, 18):
            self.assertFalse(calendar.business_day(
                datetime(2026, 10, day, 7, tzinfo=calendar.JST))[0])
        self.fetch.assert_not_called()

    def test_year_end_closure_includes_both_boundaries_without_network(self):
        for year in (2026, 2027, 2028):
            for month, days in ((12, range(29, 32)), (1, range(1, 5))):
                for day in days:
                    with self.subTest(year=year, month=month, day=day):
                        allowed, reason = calendar.business_day(
                            datetime(year, month, day, 7, tzinfo=calendar.JST))
                        self.assertFalse(allowed)
                        self.assertIn('year-end/New Year closure', reason)
        self.fetch.assert_not_called()

    def test_days_outside_year_end_closure_and_jst_boundary(self):
        self.assertTrue(calendar.business_day(
            datetime(2026, 12, 28, 7, tzinfo=calendar.JST))[0])
        self.assertTrue(calendar.business_day(
            datetime(2026, 1, 5, 7, tzinfo=calendar.JST))[0])
        # December 28 UTC is already December 29 in Japan.
        self.assertFalse(calendar.business_day(
            datetime(2026, 12, 28, 22, tzinfo=timezone.utc))[0])

    def test_calendar_errors_do_not_allow_report(self):
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            calendar.business_day(datetime(2028, 1, 5, 7, tzinfo=calendar.JST))
        self.response.__enter__.return_value.read.return_value = b'<html>Error</html>'
        with self.assertRaises(ValueError):
            calendar.business_day(datetime(2026, 10, 16, 7, tzinfo=calendar.JST))
        self.fetch.side_effect = OSError('network unavailable')
        with self.assertRaises(OSError):
            calendar.business_day(datetime(2026, 10, 16, 7, tzinfo=calendar.JST))

    def test_gate_outputs_and_manual_override(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            summary = Path(directory) / 'summary'
            with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'schedule',
                                         'GITHUB_OUTPUT': str(output),
                                         'GITHUB_STEP_SUMMARY': str(summary)}):
                with patch.object(calendar, 'business_day', return_value=(False, 'Holiday')):
                    calendar.main()
                self.assertEqual(output.read_text(), 'should_run=false\n')
                self.assertIn('Holiday', summary.read_text())
                output.write_text('')
                with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}), \
                        patch.object(calendar, 'business_day') as check:
                    calendar.main()
                    check.assert_not_called()
                self.assertEqual(output.read_text(), 'should_run=true\n')


if __name__ == '__main__':
    unittest.main()
