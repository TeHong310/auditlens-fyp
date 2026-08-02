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
"""
from datetime import datetime, date


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
