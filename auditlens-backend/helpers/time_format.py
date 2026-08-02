"""Single shared place to turn a naive `timestamp without time zone`
value read back from Postgres into an unambiguous ISO 8601 UTC string.

Every timestamp column in this schema is naive (see db.py's pooled
connections, which now force the session timezone to UTC on every
environment), so a plain `.isoformat()` produces a string with no
timezone marker at all - a browser's `new Date(...)` then parses it as
LOCAL time instead of UTC, silently shifting the displayed time by
whatever the viewer's UTC offset is. Appending 'Z' here is what tells
the frontend (and any other ISO 8601 consumer) this value is UTC, so it
can be converted to Asia/Kuala_Lumpur exactly once, in exactly one
place (auditlens-frontend/src/app/shared/datetime.util.ts).

Date-only business fields (Invoice Date, PO Date, response_due_date,
etc.) carry no time-of-day and must never gain one here - callers pass
those through unchanged (see date-only note below).

Also the one shared place charts/trends/"today" boundaries GROUP BY or
filter on a Malaysia calendar day instead of a UTC one - see
malaysia_date_sql()/malaysia_today()/malaysia_midnight_utc()/
to_malaysia_date() below. Reused by both the Auditor and Finance sides
of the app (routes/auditor.py's report summary + audit trail today).
"""
from datetime import datetime, date, time as _time, timezone
from zoneinfo import ZoneInfo

MALAYSIA_TZ = ZoneInfo('Asia/Kuala_Lumpur')


def to_utc_iso(value):
    """Datetime -> 'YYYY-MM-DDTHH:MM:SSZ'. A plain date (not a datetime -
    checked first, since datetime is itself a subclass of date) or None
    passes through unchanged: a date-only value has no time-of-day to
    mark as UTC, and shifting it would violate the "don't timezone-shift
    date-only fields" rule."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec='seconds') + 'Z'
    return value.isoformat()


def serialize_row_datetimes(row):
    """Row-wide, type-aware replacement for the repeated
    `for k, v in row.items(): if hasattr(v, 'isoformat'): row[k] = v.isoformat()`
    idiom used across routes/ - mutates `row` in place and returns it.
    Real datetime columns get the explicit UTC 'Z' suffix above; plain
    date columns keep their existing bare YYYY-MM-DD form."""
    for k, v in row.items():
        if isinstance(v, datetime):
            row[k] = to_utc_iso(v)
        elif isinstance(v, date) or hasattr(v, 'isoformat'):
            row[k] = v.isoformat()
    return row


def malaysia_today():
    """Today's calendar date in Asia/Kuala_Lumpur, computed from the
    real current UTC instant - not server-local time, which may be any
    timezone depending on where this process happens to run (a local
    dev machine vs. Render's container)."""
    return datetime.now(timezone.utc).astimezone(MALAYSIA_TZ).date()


def to_malaysia_date(value):
    """Naive datetime (known-UTC - see db.py's session timezone) -> the
    Malaysia calendar date it falls on. The Python-side companion to
    malaysia_date_sql() below, for call sites that already fetched rows
    and bucket them in Python instead of in the SQL itself. None in,
    None out."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(MALAYSIA_TZ).date()


def malaysia_midnight_utc(myt_date):
    """The naive UTC datetime for 00:00 Asia/Kuala_Lumpur on `myt_date` -
    the correct bound for `WHERE naive_utc_column >= /< ...` when the
    caller means a whole Malaysia calendar day, not a UTC one (Malaysia
    midnight is 16:00 UTC the PREVIOUS day, so naively comparing against
    `myt_date` itself would cut off/include the wrong ~8 hours)."""
    local_midnight = datetime.combine(myt_date, _time.min, tzinfo=MALAYSIA_TZ)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def malaysia_date_sql(column_expr):
    """SQL fragment: given a naive `timestamp without time zone` column
    or expression known to hold real UTC digits (db.py forces the
    session timezone to UTC), returns an expression for that value's
    Asia/Kuala_Lumpur calendar date - the one shared way every day-
    bucketed chart/trend query in this app must SELECT/GROUP BY, instead
    of Postgres's own DATE(...)/TO_CHAR(...), which would silently use
    the UTC date and end a "today" a few hours early for Malaysia.
    Usage: f"SELECT {malaysia_date_sql('reviewed_at')} AS day, COUNT(*) ... GROUP BY {malaysia_date_sql('reviewed_at')}"
    """
    return f"(({column_expr}) AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kuala_Lumpur')::date"
