"""Pure helpers for the Audit Workflow Calendar (routes/calendar.py) —
manual-task validation and event shaping. No DB access, no AI calls, so
every function here is unit-testable without a Flask request context or
a real database connection, same pattern as helpers/send_back.py.

Reuses helpers/send_back.py's PRIORITIES vocabulary (normal/medium/high)
rather than redefining a second, parallel set of priority values.
"""
from datetime import date, datetime

from helpers.send_back import PRIORITIES

__all__ = ['PRIORITIES', 'validate_task_payload', 'exception_to_calendar_event', 'anomaly_to_calendar_event']


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except ValueError:
        return None


def validate_task_payload(data):
    """Validates a Manual Audit Task creation payload: title, date
    required; description/assigned_to optional; priority defaults to
    'normal'. Returns (errors: list[str], cleaned: dict|None) — cleaned
    is None whenever errors is non-empty."""
    errors = []

    title = (data.get('title') or '').strip()
    if not title:
        errors.append('title is required')

    event_date = _parse_date(data.get('date'))
    if not data.get('date'):
        errors.append('date is required')
    elif event_date is None:
        errors.append('date must be a valid date (YYYY-MM-DD)')

    priority = (data.get('priority') or 'normal').strip().lower()
    if priority not in PRIORITIES:
        errors.append(f'priority must be one of {PRIORITIES}')

    assigned_to = data.get('assigned_to')
    if assigned_to is not None:
        try:
            assigned_to = int(assigned_to)
        except (TypeError, ValueError):
            errors.append('assigned_to must be a user id')
            assigned_to = None

    if errors:
        return errors, None

    return [], {
        'title':        title,
        'description':  (data.get('description') or '').strip() or None,
        'event_date':   event_date,
        'assigned_to':  assigned_to,
        'priority':     priority,
    }


# ── Exception/anomaly rows -> calendar events ──────────────────────────
# Both reuse an EXISTING, unmodified classification (routes/auditor.py's
# _classify_exception()/_build_comparison() for exceptions; the
# `anomalies` table rows exactly as helpers/anomaly_detector.py already
# writes them) — these two functions only reshape an already-computed
# result into the calendar's common event shape, they don't classify or
# detect anything themselves.

def exception_to_calendar_event(document_id, invoice_no, vendor_name, uploaded_at,
                                 exception_type, label, detail, severity):
    """`uploaded_at` (the document's own real upload timestamp) is used
    as the event date — the date this exception has been outstanding
    SINCE, not a fabricated deadline (exceptions have no due-date column
    in this schema)."""
    return {
        'event_type':   'exception_followup',
        'date':         uploaded_at,
        'title':        label,
        'document_id':  document_id,
        'invoice_no':   invoice_no,
        'vendor_name':  vendor_name,
        'priority':     'high' if severity == 'high' else ('medium' if severity == 'medium' else 'normal'),
        'description':  detail,
        'status':       exception_type,
    }


def anomaly_to_calendar_event(document_id, invoice_no, vendor_name, created_at,
                               anomaly_type, severity, explanation):
    """`created_at` (when the anomaly was detected) is used as the event
    date, for the same reason as exception_to_calendar_event above —
    anomalies have no due-date column either."""
    return {
        'event_type':   'anomaly_followup',
        'date':         created_at,
        'title':        f'Investigate {anomaly_type.replace("_", " ")} anomaly',
        'document_id':  document_id,
        'invoice_no':   invoice_no,
        'vendor_name':  vendor_name,
        'priority':     'high' if severity == 'high' else ('medium' if severity == 'medium' else 'normal'),
        'description':  explanation,
        'status':       severity,
    }
