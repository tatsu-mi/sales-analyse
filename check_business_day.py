"""Gate scheduled reports using JST and the Cabinet Office holiday calendar."""
import csv
import io
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

JST = timezone(timedelta(hours=9))
HOLIDAY_URL = 'https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv'


def parse_holidays(data):
    rows = csv.reader(io.StringIO(data.decode('cp932')))
    if next(rows, None) != ['国民の祝日・休日月日', '国民の祝日・休日名称']:
        raise ValueError('Unexpected holiday calendar header')
    holidays = {}
    for row in rows:
        if not row:
            continue
        day, name = row
        year, month, day_of_month = map(int, day.split('/'))
        if not name.strip():
            raise ValueError('Missing holiday name')
        holidays[date(year, month, day_of_month)] = name
    return holidays


def business_day(now=None):
    today = (now or datetime.now(JST)).astimezone(JST).date()
    if (today.month == 12 and today.day >= 29) or (today.month == 1 and today.day <= 4):
        return False, f'{today}: year-end/New Year closure (JST)'
    if today.weekday() >= 5:
        return False, f'{today}: weekend (JST)'
    with urlopen(HOLIDAY_URL, timeout=30) as response:
        holidays = parse_holidays(response.read())
    if today.year not in {day.year for day in holidays}:
        raise ValueError(f'Holiday calendar does not cover {today.year}')
    if today in holidays:
        return False, f'{today}: {holidays[today]} (JST)'
    return True, f'{today}: business day (JST)'


def main():
    # Explicit manual runs remain available for testing and recovery on holidays.
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        should_run, reason = True, 'Manual run: bypass calendar check'
    else:
        should_run, reason = business_day()
    print(reason)
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
        output.write(f'should_run={str(should_run).lower()}\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as summary:
            summary.write(f'{reason}\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Business-day check failed; report will not run: {error}', file=sys.stderr)
        sys.exit(1)
