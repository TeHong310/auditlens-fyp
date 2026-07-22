"""Pure unit tests for helpers/malaysia_calendar.py — no Flask, no DB, no
AI calls. Same house style as tests/calendar/test_calendar_events_
validation.py.

Usage:
    python tests/calendar/test_malaysia_calendar.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import helpers.malaysia_calendar as mc

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_fixed_holidays_exact_for_any_year():
    print('Case: fixed-date holidays (New Year, Labour Day, Merdeka, Malaysia Day, Christmas) are exact for any year')
    for year in (2024, 2025, 2026, 2030, 2050):
        holidays = mc.get_public_holidays(year)
        check(f'{year}: New Year present', f'{year}-01-01' in holidays, holidays)
        check(f'{year}: Labour Day present', f'{year}-05-01' in holidays, holidays)
        check(f'{year}: Merdeka Day present', f'{year}-08-31' in holidays, holidays)
        check(f'{year}: Malaysia Day present', f'{year}-09-16' in holidays, holidays)
        check(f'{year}: Christmas present', f'{year}-12-25' in holidays, holidays)


def run_case_agong_birthday_is_first_monday_of_june():
    print('Case: Agong\'s Birthday is computed as the first Monday of June (a rule, not a guess)')
    for year in (2025, 2026, 2027, 2030):
        d = mc._nth_weekday_of_month(year, 6, weekday=0, n=1)
        check(f'{year}: falls in June', d.month == 6, d)
        check(f'{year}: is a Monday', d.weekday() == 0, d)
        check(f'{year}: is within the first 7 days of June', d.day <= 7, d)
        holidays = mc.get_public_holidays(year)
        check(f'{year}: present in the holiday table under that exact date', d.isoformat() in holidays, holidays)


def run_case_movable_holidays_only_present_for_populated_years():
    print('Case: movable holidays (CNY, Hari Raya, etc.) are present for 2025-2027 only — no fabricated entry for an unpopulated year')
    for year in (2025, 2026, 2027):
        holidays = mc.get_public_holidays(year)
        check(f'{year}: has more than just the 6 fixed/rule-based holidays', len(holidays) > 6, holidays)

    holidays_2040 = mc.get_public_holidays(2040)
    check('2040 (unpopulated): still has the fixed/rule-based holidays', len(holidays_2040) == 6, holidays_2040)
    check('2040 (unpopulated): no movable holiday guessed', not any(
        name in ('Chinese New Year', 'Hari Raya Aidilfitri', 'Hari Raya Haji', 'Deepavali', 'Wesak Day')
        for name in holidays_2040.values()
    ), holidays_2040)


def run_case_is_public_holiday_and_is_working_day():
    print('Case: is_public_holiday()/is_working_day() correctly flag a known holiday')
    check('2026-01-01 is a public holiday', mc.is_public_holiday(date(2026, 1, 1)) == "New Year's Day")
    check('2026-01-01 is not a working day (holiday)', mc.is_working_day(date(2026, 1, 1)) is False)
    check('an ordinary Wednesday with no holiday is a working day', mc.is_working_day(date(2026, 7, 22)) is True)


def run_case_saturday_and_sunday_excluded():
    print('Case: Saturday and Sunday are never working days, even with no holiday involved')
    # 2026-07-25 is a Saturday, 2026-07-26 is a Sunday (no MY holiday
    # falls on either in this dataset).
    check('Saturday excluded', mc.is_working_day(date(2026, 7, 25)) is False)
    check('Sunday excluded', mc.is_working_day(date(2026, 7, 26)) is False)
    check('the following Monday IS a working day', mc.is_working_day(date(2026, 7, 27)) is True)


def run_case_working_days_until_excludes_weekends_and_holidays():
    print('Case: working_days_until() correctly excludes weekends and the start date itself')
    # Monday 2026-07-20 -> Monday 2026-07-27: Tue,Wed,Thu,Fri,(Sat,Sun excluded),Mon = 5
    check('Mon to next Mon = 5 working days (weekend excluded)',
          mc.working_days_until(date(2026, 7, 20), date(2026, 7, 27)) == 5,
          mc.working_days_until(date(2026, 7, 20), date(2026, 7, 27)))
    check('same day = 0', mc.working_days_until(date(2026, 7, 20), date(2026, 7, 20)) == 0)
    check('to_date before from_date = 0 (never negative)',
          mc.working_days_until(date(2026, 7, 27), date(2026, 7, 20)) == 0)

    # A range that spans a public holiday (2026-08-31 Merdeka Day, a
    # Monday) must exclude that day too, not just weekends.
    before_holiday_days = mc.working_days_until(date(2026, 8, 28), date(2026, 9, 1))
    # Fri 28 -> Sat 29(x), Sun 30(x), Mon 31 Merdeka(x), Tue Sep 1(v) = 1
    check('a public holiday inside the range is excluded, not just weekends',
          before_holiday_days == 1, before_holiday_days)


def run_case_deadline_status_due_in_future():
    print('Case: deadline_status() for a future due date reports "Due in N working days"')
    result = mc.deadline_status(date(2026, 7, 27), today=date(2026, 7, 22))
    check('overdue is False', result['overdue'] is False, result)
    check('working_days is 3 (Thu, Fri, Mon — Sat/Sun excluded)', result['working_days'] == 3, result)


def run_case_deadline_status_overdue():
    print('Case: deadline_status() for a past due date reports "Overdue by N working days"')
    result = mc.deadline_status(date(2026, 7, 15), today=date(2026, 7, 22))
    check('overdue is True', result['overdue'] is True, result)
    check('working_days is 5', result['working_days'] == 5, result)


def run_case_deadline_status_due_today():
    print('Case: deadline_status() when due_date == today reports 0 working days, not overdue')
    result = mc.deadline_status(date(2026, 7, 22), today=date(2026, 7, 22))
    check('overdue is False (due today is not yet overdue)', result['overdue'] is False, result)
    check('working_days is 0', result['working_days'] == 0, result)


def run_case_deadline_status_accepts_iso_string():
    print('Case: deadline_status() accepts a YYYY-MM-DD string, not just a date object')
    result = mc.deadline_status('2026-07-27', today=date(2026, 7, 22))
    check('parsed correctly', result == {'working_days': 3, 'overdue': False}, result)


def run_case_deadline_status_none_when_no_due_date():
    print('Case: deadline_status(None) returns None — never fabricates a deadline that was never set')
    check('None in, None out', mc.deadline_status(None) is None)
    check('empty string in, None out', mc.deadline_status('') is None)


def run_case_holidays_in_range():
    print('Case: get_public_holidays_in_range() returns only holidays within the bounds, sorted ascending')
    result = mc.get_public_holidays_in_range('2026-08-01', '2026-09-30')
    dates = [h['date'] for h in result]
    check('Merdeka Day present', '2026-08-31' in dates, dates)
    check('Malaysia Day present', '2026-09-16' in dates, dates)
    check('Maulidur Rasul present', '2026-08-26' in dates, dates)
    check('sorted ascending', dates == sorted(dates), dates)
    check('nothing outside the range', all('2026-08-01' <= d <= '2026-09-30' for d in dates), dates)


def run_case_holidays_in_range_spans_year_boundary():
    print('Case: get_public_holidays_in_range() correctly spans a year boundary (Dec -> Jan)')
    result = mc.get_public_holidays_in_range('2025-12-20', '2026-01-10')
    dates = [h['date'] for h in result]
    check('2025 Christmas present', '2025-12-25' in dates, dates)
    check('2026 New Year present', '2026-01-01' in dates, dates)


def run_case_holidays_in_range_empty_without_bounds():
    print('Case: get_public_holidays_in_range() returns [] when start/end are missing (defensive)')
    check('no start', mc.get_public_holidays_in_range(None, '2026-01-31') == [])
    check('no end', mc.get_public_holidays_in_range('2026-01-01', None) == [])


def run_case_malaysia_today_is_a_fixed_utc8_offset():
    print('Case: malaysia_today() uses a fixed UTC+8 offset (no DST) — equivalent to Asia/Kuala_Lumpur')
    from datetime import timedelta
    check('MALAYSIA_TZ offset is exactly 8 hours', mc.MALAYSIA_TZ.utcoffset(None) == timedelta(hours=8))


if __name__ == '__main__':
    run_case_fixed_holidays_exact_for_any_year()
    run_case_agong_birthday_is_first_monday_of_june()
    run_case_movable_holidays_only_present_for_populated_years()
    run_case_is_public_holiday_and_is_working_day()
    run_case_saturday_and_sunday_excluded()
    run_case_working_days_until_excludes_weekends_and_holidays()
    run_case_deadline_status_due_in_future()
    run_case_deadline_status_overdue()
    run_case_deadline_status_due_today()
    run_case_deadline_status_accepts_iso_string()
    run_case_deadline_status_none_when_no_due_date()
    run_case_holidays_in_range()
    run_case_holidays_in_range_spans_year_boundary()
    run_case_holidays_in_range_empty_without_bounds()
    run_case_malaysia_today_is_a_fixed_utc8_offset()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
