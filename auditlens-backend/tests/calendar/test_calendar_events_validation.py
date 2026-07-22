"""Pure unit tests for helpers/calendar_events.py — no Flask, no DB, no
AI calls. Same house style as tests/reviews/test_send_back_validation.py.

Usage:
    python tests/calendar/test_calendar_events_validation.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.calendar_events import (
    validate_task_payload, exception_to_calendar_event, anomaly_to_calendar_event,
)

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def valid_task(**overrides):
    payload = {'title': 'Follow up with vendor', 'date': '2026-07-25', 'priority': 'medium'}
    payload.update(overrides)
    return payload


def run_case_valid_task_passes():
    print('Case: a fully valid manual task payload passes with no errors')
    errors, cleaned = validate_task_payload(valid_task())
    check('no errors', errors == [], errors)
    check('title preserved', cleaned['title'] == 'Follow up with vendor')
    check('event_date parsed to a date object', cleaned['event_date'] == date(2026, 7, 25))
    check('priority preserved', cleaned['priority'] == 'medium')
    check('assigned_to defaults to None (caller assigns to self)', cleaned['assigned_to'] is None)


def run_case_missing_title_fails():
    print('Case: missing/blank title is rejected')
    errors, cleaned = validate_task_payload(valid_task(title='   '))
    check('rejected', cleaned is None)
    check('error mentions title', any('title' in e for e in errors), errors)


def run_case_missing_date_fails():
    print('Case: missing date is rejected')
    payload = valid_task()
    del payload['date']
    errors, cleaned = validate_task_payload(payload)
    check('rejected', cleaned is None)
    check('error mentions date', any('date' in e for e in errors), errors)


def run_case_malformed_date_fails():
    print('Case: a malformed date string is rejected, not a crash')
    errors, cleaned = validate_task_payload(valid_task(date='not-a-date'))
    check('rejected', cleaned is None)
    check('error mentions date', any('date' in e for e in errors), errors)


def run_case_invalid_priority_fails():
    print('Case: an unrecognized priority is rejected')
    errors, cleaned = validate_task_payload(valid_task(priority='urgent'))
    check('rejected', cleaned is None)


def run_case_default_priority_is_normal():
    print('Case: omitting priority defaults to "normal" (same vocabulary as send_back_cycles)')
    payload = valid_task()
    del payload['priority']
    errors, cleaned = validate_task_payload(payload)
    check('accepted', errors == [], errors)
    check('priority defaults to normal', cleaned['priority'] == 'normal')


def run_case_invalid_assigned_to_fails():
    print('Case: a non-numeric assigned_to is rejected')
    errors, cleaned = validate_task_payload(valid_task(assigned_to='not-a-user-id'))
    check('rejected', cleaned is None)
    check('error mentions assigned_to', any('assigned_to' in e for e in errors), errors)


def run_case_valid_assigned_to_is_coerced_to_int():
    print('Case: a numeric-string assigned_to is coerced to int')
    errors, cleaned = validate_task_payload(valid_task(assigned_to='7'))
    check('accepted', errors == [], errors)
    check('assigned_to coerced to int', cleaned['assigned_to'] == 7, cleaned)


def run_case_exception_event_shape_and_priority_mapping():
    print('Case: exception_to_calendar_event shapes the row correctly and maps severity -> priority')
    event = exception_to_calendar_event(
        68, 'IX107587', 'Vertex Microsystems Sdn. Bhd.', '2026-07-20T10:00:00',
        'mismatch', 'Amount Mismatch', 'Amount differs: Invoice RM100 vs PO RM90', 'high',
    )
    check('event_type', event['event_type'] == 'exception_followup')
    check('date carried through unmodified (caller normalizes)', event['date'] == '2026-07-20T10:00:00')
    check('title uses the classification label', event['title'] == 'Amount Mismatch')
    check('document_id carried through', event['document_id'] == 68)
    check('high severity -> high priority', event['priority'] == 'high')
    check('status carries the exception_type', event['status'] == 'mismatch')

    event_medium = exception_to_calendar_event(1, None, None, None, 'missing_document', 'Missing PO', '', 'medium')
    check('medium severity -> medium priority', event_medium['priority'] == 'medium')
    event_low = exception_to_calendar_event(1, None, None, None, 'low_confidence', 'Low OCR', '', 'low')
    check('low severity -> normal priority (no "low" bucket on the calendar)', event_low['priority'] == 'normal')


def run_case_anomaly_event_shape_and_title_formatting():
    print('Case: anomaly_to_calendar_event shapes the row and formats the title from anomaly_type')
    event = anomaly_to_calendar_event(
        70, 'INV-VTX-2026-K201', 'Vertex Microsystems Sdn. Bhd.', '2026-07-18T09:00:00',
        'possible_duplicate_invoice', 'high', 'This invoice closely matches another recent submission.',
    )
    check('event_type', event['event_type'] == 'anomaly_followup')
    check('title humanizes the anomaly_type', event['title'] == 'Investigate possible duplicate invoice anomaly', event['title'])
    check('high severity -> high priority', event['priority'] == 'high')
    check('status carries the raw severity', event['status'] == 'high')


if __name__ == '__main__':
    run_case_valid_task_passes()
    run_case_missing_title_fails()
    run_case_missing_date_fails()
    run_case_malformed_date_fails()
    run_case_invalid_priority_fails()
    run_case_default_priority_is_normal()
    run_case_invalid_assigned_to_fails()
    run_case_valid_assigned_to_is_coerced_to_int()
    run_case_exception_event_shape_and_priority_mapping()
    run_case_anomaly_event_shape_and_title_formatting()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
