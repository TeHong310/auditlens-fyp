"""Malaysia business-calendar awareness for the Audit Workflow Calendar
(routes/calendar.py) — public holidays, working-day exclusion (Sat/Sun +
public holidays), and working-day-based deadline calculation.

Pure functions only: no DB access, no Flask, no AI calls, so every
function here is unit-testable in isolation, same pattern as helpers/
send_back.py and helpers/calendar_events.py.

TIMEZONE: Malaysia Standard Time is a FIXED UTC+8 offset with no daylight
saving (abolished in 1982) — so a fixed `timezone(timedelta(hours=8))` is
exactly equivalent to the IANA "Asia/Kuala_Lumpur" zone for every date,
past or future. This is used instead of `zoneinfo.ZoneInfo('Asia/
Kuala_Lumpur')` deliberately: zoneinfo needs the OS tz database (present
on Linux, where Render deploys, but NOT on a stock Windows dev machine
without the extra `tzdata` PyPI package) — the fixed offset gives the
identical result with zero new dependency and no platform gap.

HOLIDAY DATA DISCLOSURE: Fixed-date and day-of-week-rule holidays below
are exact for any year. Movable Islamic-calendar holidays (Hari Raya
Aidilfitri, Hari Raya Haji, Awal Muharram, Maulidur Rasul) and other
lunar/astronomical dates (Chinese New Year, Wesak Day, Deepavali) are
officially confirmed only close to the date (moon-sighting for Islamic
dates) — the dates below are best-effort estimates for 2025-2027 and
MUST be reconciled against the official Malaysian government public
holiday gazette (published by JPA, jpa.gov.my) before being relied on
for real compliance/deadline decisions. Only federal-level holidays are
included (not the additional state-specific holidays some states
observe, e.g. Thaipusam, various Sultan's birthdays).
"""
from datetime import date, datetime, timedelta, timezone

MALAYSIA_TZ = timezone(timedelta(hours=8))  # see TIMEZONE note above


def malaysia_today() -> date:
    """'Today' as understood in Malaysia, regardless of the server's own
    timezone (Render runs UTC) — the whole point of being timezone-aware
    rather than using the server-local `date.today()`."""
    return datetime.now(MALAYSIA_TZ).date()


def _nth_weekday_of_month(year, month, weekday, n=1):
    """The date of the Nth occurrence of `weekday` (Mon=0..Sun=6) in the
    given month/year — used for Yang di-Pertuan Agong's Birthday (first
    Monday of June), which is a genuine calendar RULE, not a fixed date,
    so this is exact for any year rather than a guess."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d += timedelta(days=offset + 7 * (n - 1))
    return d


def _fixed_holidays(year: int) -> dict:
    """Federal public holidays that fall on the SAME calendar date every
    year — exact, no estimation involved."""
    return {
        date(year, 1, 1).isoformat():  "New Year's Day",
        date(year, 5, 1).isoformat():  'Labour Day',
        date(year, 8, 31).isoformat(): 'Merdeka Day (National Day)',
        date(year, 9, 16).isoformat(): 'Malaysia Day',
        date(year, 12, 25).isoformat(): 'Christmas Day',
    }


def _rule_based_holidays(year: int) -> dict:
    """Federal holidays defined by a day-of-week RULE rather than a
    fixed date — computed exactly, not estimated."""
    agong_birthday = _nth_weekday_of_month(year, 6, weekday=0, n=1)  # first Monday of June
    return {
        agong_birthday.isoformat(): "Yang di-Pertuan Agong's Birthday",
    }


# Best-effort estimates only — see the module docstring's HOLIDAY DATA
# DISCLOSURE above. Populate additional years here as they're confirmed.
_MOVABLE_HOLIDAYS_BY_YEAR = {
    2025: {
        '2025-01-29': 'Chinese New Year',
        '2025-01-30': 'Chinese New Year Holiday',
        '2025-03-31': 'Hari Raya Aidilfitri',
        '2025-04-01': 'Hari Raya Aidilfitri Holiday',
        '2025-05-12': 'Wesak Day',
        '2025-06-07': 'Hari Raya Haji',
        '2025-06-27': 'Awal Muharram',
        '2025-09-05': 'Maulidur Rasul',
        '2025-10-20': 'Deepavali',
    },
    2026: {
        '2026-02-17': 'Chinese New Year',
        '2026-02-18': 'Chinese New Year Holiday',
        '2026-03-20': 'Hari Raya Aidilfitri',
        '2026-03-21': 'Hari Raya Aidilfitri Holiday',
        '2026-05-27': 'Hari Raya Haji',
        '2026-05-31': 'Wesak Day',
        '2026-06-17': 'Awal Muharram',
        '2026-08-26': 'Maulidur Rasul',
        '2026-11-08': 'Deepavali',
    },
    2027: {
        '2027-02-06': 'Chinese New Year',
        '2027-02-07': 'Chinese New Year Holiday',
        '2027-03-10': 'Hari Raya Aidilfitri',
        '2027-03-11': 'Hari Raya Aidilfitri Holiday',
        '2027-05-16': 'Hari Raya Haji',
        '2027-05-20': 'Wesak Day',
        '2027-06-06': 'Awal Muharram',
        '2027-08-15': 'Maulidur Rasul',
        '2027-10-28': 'Deepavali',
    },
}


def get_public_holidays(year: int) -> dict:
    """Every known federal public holiday for `year` as {iso_date: name}.
    Movable holidays are only present for years populated in
    _MOVABLE_HOLIDAYS_BY_YEAR (2025-2027 today) — no entry is fabricated
    for an unpopulated year, fixed/rule-based holidays still apply."""
    holidays = {}
    holidays.update(_fixed_holidays(year))
    holidays.update(_rule_based_holidays(year))
    holidays.update(_MOVABLE_HOLIDAYS_BY_YEAR.get(year, {}))
    return holidays


def get_public_holidays_in_range(start_iso: str, end_iso: str) -> list:
    """[{'date': iso, 'name': ...}, ...] sorted ascending, for every
    holiday whose date falls within [start_iso, end_iso] inclusive.
    Spans the year(s) the range touches (a range can cross a year
    boundary, e.g. viewing December then January)."""
    if not start_iso or not end_iso:
        return []
    start_year = int(start_iso[:4])
    end_year = int(end_iso[:4])

    merged = {}
    for year in range(start_year, end_year + 1):
        merged.update(get_public_holidays(year))

    result = [
        {'date': iso, 'name': name}
        for iso, name in merged.items()
        if start_iso <= iso <= end_iso
    ]
    result.sort(key=lambda h: h['date'])
    return result


def is_public_holiday(d: date) -> str | None:
    """The holiday name if `d` is a known Malaysian public holiday,
    else None."""
    return get_public_holidays(d.year).get(d.isoformat())


def is_working_day(d: date) -> bool:
    """False for Saturday/Sunday or a known public holiday, else True."""
    if d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    return is_public_holiday(d) is None


def working_days_until(from_date: date, to_date: date) -> int:
    """Counts working days strictly AFTER from_date, up to and including
    to_date. Returns 0 if to_date <= from_date (never negative — use
    deadline_status() below for the signed "overdue" framing)."""
    if to_date <= from_date:
        return 0
    count = 0
    d = from_date + timedelta(days=1)
    while d <= to_date:
        if is_working_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def deadline_status(due_date, today=None):
    """The working-day-aware deadline indicator for a due date (send-back
    response_due_date or a manual task's event_date) — Feature 2/3.

    Returns {'working_days': int (always >= 0), 'overdue': bool} — read
    as "Due in N working days" when overdue=False (N=0 means due today),
    or "Overdue by N working days" when overdue=True. Returns None if
    due_date itself is None/falsy (nothing to compute).
    """
    if not due_date:
        return None
    if isinstance(due_date, str):
        due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
    today = today or malaysia_today()

    if due_date >= today:
        return {'working_days': working_days_until(today, due_date), 'overdue': False}
    return {'working_days': working_days_until(due_date, today), 'overdue': True}
