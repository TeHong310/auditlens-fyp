"""Pure unit tests for helpers/send_back.py — no Flask, no DB, no AI
calls. Same house style as tests/extraction/*.py (a check() helper that
prints OK/FAIL and collects failures, exit code 0/1).

Usage:
    python tests/reviews/test_send_back_validation.py
"""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.send_back import (
    validate_send_back_payload, validate_finance_response_payload,
    compute_activity_summary, is_overdue,
)

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


TODAY = date(2026, 7, 21)


def valid_payload(**overrides):
    payload = {
        'reason_category': 'possible_duplicate_invoice',
        'instruction': 'Confirm whether this invoice was uploaded twice.',
        'required_actions': ['provide_written_explanation', 'confirm_duplicate_submission'],
        'priority': 'high',
        'due_date': '2026-07-25',
    }
    payload.update(overrides)
    return payload


def run_case_valid_payload_passes():
    print('Case: a fully valid structured send-back payload passes with no errors')
    errors, cleaned = validate_send_back_payload(valid_payload(), today=TODAY)
    check('no errors', errors == [], errors)
    check('reason_category cleaned', cleaned['reason_category'] == 'possible_duplicate_invoice')
    check('due_date parsed to date object', cleaned['due_date'] == date(2026, 7, 25))
    check('priority normalized', cleaned['priority'] == 'high')


def run_case_missing_instruction_fails():
    print('Case: missing/blank instruction is rejected')
    errors, cleaned = validate_send_back_payload(valid_payload(instruction=''), today=TODAY)
    check('rejected', cleaned is None)
    check('error mentions instruction', any('instruction' in e for e in errors), errors)


def run_case_missing_required_actions_fails():
    print('Case: empty required_actions list is rejected')
    errors, cleaned = validate_send_back_payload(valid_payload(required_actions=[]), today=TODAY)
    check('rejected', cleaned is None)
    check('error mentions required_actions', any('required_actions' in e for e in errors), errors)


def run_case_invalid_required_action_value_fails():
    print('Case: an unrecognized required_actions value is rejected')
    errors, cleaned = validate_send_back_payload(valid_payload(required_actions=['fly_to_the_moon']), today=TODAY)
    check('rejected', cleaned is None)
    check('error lists the invalid value', any('fly_to_the_moon' in e for e in errors), errors)


def run_case_due_date_before_today_fails():
    print('Case: a due date earlier than today is rejected')
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    errors, cleaned = validate_send_back_payload(valid_payload(due_date=yesterday), today=TODAY)
    check('rejected', cleaned is None)
    check('error mentions due_date', any('due_date' in e for e in errors), errors)


def run_case_due_date_today_is_allowed():
    print("Case: a due date of exactly today is allowed (not 'earlier than today')")
    errors, cleaned = validate_send_back_payload(valid_payload(due_date=TODAY.isoformat()), today=TODAY)
    check('accepted', errors == [], errors)


def run_case_unparseable_due_date_fails():
    print('Case: a malformed due_date string is rejected with a clear error, not a crash')
    errors, cleaned = validate_send_back_payload(valid_payload(due_date='not-a-date'), today=TODAY)
    check('rejected', cleaned is None)
    check('error mentions due_date', any('due_date' in e for e in errors), errors)


def run_case_high_priority_requires_due_date():
    print('Case: priority=high with no due_date is rejected')
    payload = valid_payload(priority='high')
    del payload['due_date']
    errors, cleaned = validate_send_back_payload(payload, today=TODAY)
    check('rejected', cleaned is None)
    check('error mentions due_date is required for high priority', any('high-priority' in e for e in errors), errors)


def run_case_normal_priority_does_not_require_due_date():
    print('Case: priority=normal with no due_date is fine')
    payload = valid_payload(priority='normal')
    del payload['due_date']
    errors, cleaned = validate_send_back_payload(payload, today=TODAY)
    check('accepted', errors == [], errors)
    check('due_date is None', cleaned['due_date'] is None)


def run_case_other_reason_requires_note():
    print('Case: reason_category="other" with no reason_other_note is rejected')
    errors, cleaned = validate_send_back_payload(
        valid_payload(reason_category='other', reason_other_note=''), today=TODAY)
    check('rejected', cleaned is None)
    check('error mentions reason_other_note', any('reason_other_note' in e for e in errors), errors)

    errors2, cleaned2 = validate_send_back_payload(
        valid_payload(reason_category='other', reason_other_note='A special case not covered above.'), today=TODAY)
    check('accepted once note is provided', errors2 == [], errors2)


def run_case_other_required_action_requires_note():
    print('Case: required_actions includes "other" with no required_action_other_note is rejected')
    errors, cleaned = validate_send_back_payload(
        valid_payload(required_actions=['other'], required_action_other_note=''), today=TODAY)
    check('rejected', cleaned is None)
    check('error mentions required_action_other_note', any('required_action_other_note' in e for e in errors), errors)


def run_case_invalid_reason_category_fails():
    print('Case: an unrecognized reason_category is rejected')
    errors, cleaned = validate_send_back_payload(valid_payload(reason_category='aliens'), today=TODAY)
    check('rejected', cleaned is None)


def run_case_invalid_priority_fails():
    print('Case: an unrecognized priority is rejected')
    errors, cleaned = validate_send_back_payload(valid_payload(priority='urgent'), today=TODAY)
    check('rejected', cleaned is None)


def run_case_finance_response_required():
    print('Case: an empty Finance response is rejected')
    errors, response = validate_finance_response_payload({'response': '   '})
    check('rejected', response is None)
    check('error mentions response', any('response' in e for e in errors), errors)

    errors2, response2 = validate_finance_response_payload(
        {'response': 'The invoice was uploaded twice; the duplicate was withdrawn.'})
    check('accepted', errors2 == [], errors2)
    check('response text preserved', response2 == 'The invoice was uploaded twice; the duplicate was withdrawn.')


def run_case_activity_summary_only_reports_real_post_sendback_changes():
    print('Case: activity_summary reflects only timestamps strictly AFTER sent_back_at — never invented')
    sent_back_at = datetime(2026, 7, 21, 10, 0, 0)
    cycle = {'sent_back_at': sent_back_at, 'finance_response': None}

    before = datetime(2026, 7, 20, 9, 0, 0)
    after = datetime(2026, 7, 22, 9, 0, 0)

    summary_none = compute_activity_summary(cycle, before, before, before)
    check('no activity when everything predates the send-back', summary_none == [], summary_none)

    summary_invoice = compute_activity_summary(cycle, after, None, None)
    check('invoice edit after send-back is reported', 'Invoice fields were corrected' in summary_invoice, summary_invoice)

    summary_po = compute_activity_summary(cycle, None, after, None)
    check('PO upload after send-back is reported', 'Purchase Order was uploaded or replaced' in summary_po, summary_po)

    summary_gr = compute_activity_summary(cycle, None, None, after)
    check('GR upload after send-back is reported', 'Goods Receipt was uploaded or replaced' in summary_gr, summary_gr)

    cycle_with_response = {'sent_back_at': sent_back_at, 'finance_response': 'Fixed the duplicate.'}
    summary_response = compute_activity_summary(cycle_with_response, None, None, None)
    check('finance response is reported when present', summary_response == ['Finance response added'], summary_response)


def run_case_activity_summary_empty_without_sent_back_at():
    print('Case: compute_activity_summary returns [] if the cycle has no sent_back_at (defensive)')
    summary = compute_activity_summary({}, datetime.now(), datetime.now(), datetime.now())
    check('empty list', summary == [], summary)


def run_case_is_overdue():
    print('Case: is_overdue is true only while awaiting Finance AND past due')
    yesterday = TODAY - timedelta(days=1)
    tomorrow = TODAY + timedelta(days=1)

    overdue_cycle = {'cycle_status': 'action_required', 'response_due_date': yesterday}
    check('overdue when action_required and due date passed', is_overdue(overdue_cycle, today=TODAY) is True)

    not_yet_due = {'cycle_status': 'action_required', 'response_due_date': tomorrow}
    check('not overdue when due date is in the future', is_overdue(not_yet_due, today=TODAY) is False)

    resubmitted_cycle = {'cycle_status': 'resubmitted', 'response_due_date': yesterday}
    check('not overdue once resubmitted, even past due date', is_overdue(resubmitted_cycle, today=TODAY) is False)

    resolved_cycle = {'cycle_status': 'resolved', 'response_due_date': yesterday}
    check('not overdue once resolved', is_overdue(resolved_cycle, today=TODAY) is False)

    no_due_date = {'cycle_status': 'action_required', 'response_due_date': None}
    check('not overdue when no due date was set', is_overdue(no_due_date, today=TODAY) is False)


if __name__ == '__main__':
    run_case_valid_payload_passes()
    run_case_missing_instruction_fails()
    run_case_missing_required_actions_fails()
    run_case_invalid_required_action_value_fails()
    run_case_due_date_before_today_fails()
    run_case_due_date_today_is_allowed()
    run_case_unparseable_due_date_fails()
    run_case_high_priority_requires_due_date()
    run_case_normal_priority_does_not_require_due_date()
    run_case_other_reason_requires_note()
    run_case_other_required_action_requires_note()
    run_case_invalid_reason_category_fails()
    run_case_invalid_priority_fails()
    run_case_finance_response_required()
    run_case_activity_summary_only_reports_real_post_sendback_changes()
    run_case_activity_summary_empty_without_sent_back_at()
    run_case_is_overdue()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
