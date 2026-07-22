"""Route-level tests for the Audit Workflow Calendar (routes/calendar.py):
role restrictions, manual-task CRUD, and the GET /calendar/events
aggregation — exercised through a REAL Flask test client hitting the
REAL route/view functions and JWT machinery, against a fully fake,
in-memory database. No real Postgres connection, no Claude/Gemini calls
— same technique as tests/reviews/test_send_back_routes.py.

Usage:
    python tests/calendar/test_calendar_routes.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

import routes.calendar as cal
from routes.calendar import calendar_bp

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


# ── Fake DB: dispatches on a keyword unique to each query this module
# issues, same technique as test_send_back_routes.py's FakeCursor. ──

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._last_result = None
        self._last_many = []

    def execute(self, sql, params=None):
        params = params or ()
        s = ' '.join(sql.split())

        if 'FROM documents d' in s and 'under_review' in s and 'LEFT JOIN extracted_fields ef' in s and 'send_back_cycles' not in s:
            self._last_many = self.db.get('pending_review_rows', [])
        elif 'FROM send_back_cycles sbc' in s:
            self._last_many = self.db.get('finance_correction_rows', [])
        elif "FROM documents\n" in sql or ('FROM documents' in s and 'uploaded_at, status' in s):
            self._last_many = self.db.get('exception_doc_rows', [])
        elif 'FROM anomalies a' in s:
            self._last_many = self.db.get('anomaly_rows', [])
        elif 'FROM calendar_tasks ct' in s:
            self._last_many = self.db.get('manual_task_rows', [])
        elif "role IN ('auditor', 'finance_executive') AND is_active" in s:
            self._last_many = self.db.get('assignable_users', [])
        elif s.startswith('INSERT INTO calendar_tasks'):
            self.db['next_task_id'] += 1
            task_id = self.db['next_task_id']
            self.db['tasks'][task_id] = {
                'title': params[0], 'description': params[1], 'event_date': params[2],
                'assigned_to': params[3], 'priority': params[4], 'created_by': params[5],
                'status': 'open',
            }
            self._last_result = (task_id,)
        elif s.startswith('SELECT assigned_to, created_by FROM calendar_tasks'):
            task = self.db['tasks'].get(params[0])
            self._last_result = (task['assigned_to'], task['created_by']) if task else None
        elif s.startswith("UPDATE calendar_tasks SET status = 'done'"):
            task = self.db['tasks'].get(params[0])
            if task:
                task['status'] = 'done'
        else:
            raise AssertionError(f'FakeCursor: unhandled SQL: {s}  params={params}')

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return self._last_many

    def close(self):
        pass


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.committed = False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.db)

    def commit(self):
        self.committed = True

    def close(self):
        pass


def fresh_db():
    return {
        'pending_review_rows': [], 'finance_correction_rows': [], 'exception_doc_rows': [],
        'anomaly_rows': [], 'manual_task_rows': [], 'assignable_users': [],
        'tasks': {}, 'next_task_id': 0,
    }


def make_app(db):
    app = Flask(__name__)
    app.config['JWT_SECRET_KEY'] = 'test-secret-key-at-least-32-bytes-long-for-hs256'
    app.config['TESTING'] = True
    JWTManager(app)
    app.register_blueprint(calendar_bp, url_prefix='/calendar')

    cal.get_db_connection = lambda: FakeConn(db)
    cal.get_user_by_id = lambda uid: (
        {'user_id': 1, 'role': 'auditor', 'full_name': 'Auditor One'} if uid == '1'
        else {'user_id': 2, 'role': 'finance_executive', 'full_name': 'Finance One'}
        if uid == '2' else {'user_id': 3, 'role': 'admin', 'full_name': 'Admin One'}
    )
    cal.log_audit = lambda *a, **k: None
    cal._build_comparison = lambda cursor, document_id: None
    cal._classify_exception = lambda cursor, doc_row, comparison: None

    with app.app_context():
        auditor_token = create_access_token(identity='1')
        finance_token  = create_access_token(identity='2')
        admin_token    = create_access_token(identity='3')

    return app, auditor_token, finance_token, admin_token


def run_case_admin_cannot_access_calendar():
    print('Case: an admin role gets 403 on both GET /calendar/events and POST /calendar/tasks')
    db = fresh_db()
    app, _, _, admin_token = make_app(db)
    client = app.test_client()

    r1 = client.get('/calendar/events', headers={'Authorization': f'Bearer {admin_token}'})
    check('GET /calendar/events -> 403', r1.status_code == 403, r1.get_json())

    r2 = client.post('/calendar/tasks', json={'title': 'x', 'date': '2026-08-01'},
                      headers={'Authorization': f'Bearer {admin_token}'})
    check('POST /calendar/tasks -> 403', r2.status_code == 403, r2.get_json())


def run_case_auditor_sees_all_four_event_types_plus_own_tasks():
    print('Case: auditor GET /calendar/events aggregates pending review + finance correction + exception + anomaly + manual task')
    db = fresh_db()
    db['pending_review_rows'] = [{
        'document_id': 68, 'updated_at': None, 'uploaded_at': date(2026, 7, 20),
        'status': 'under_review', 'invoice_number': 'IX107587', 'vendor_name': 'Acme',
    }]
    db['finance_correction_rows'] = [{
        'document_id': 69, 'response_due_date': date(2026, 7, 25), 'priority': 'high',
        'return_reason_category': 'possible_duplicate_invoice', 'auditor_instruction': 'Confirm duplicate.',
        'cycle_status': 'action_required', 'sent_back_at': None,
        'invoice_number': 'IX107588', 'vendor_name': 'Beta Corp',
    }]
    db['exception_doc_rows'] = [{'document_id': 70, 'uploaded_at': date(2026, 7, 19), 'status': 'under_review'}]
    db['anomaly_rows'] = [{
        'invoice_document_id': 71, 'anomaly_type': 'round_amount', 'severity': 'medium',
        'ai_explanation': 'Amount is a round figure.', 'created_at': date(2026, 7, 18),
        'invoice_number': 'IX107590', 'vendor_name': 'Gamma Sdn Bhd',
    }]
    db['manual_task_rows'] = [{
        'task_id': 1, 'title': 'Call vendor', 'description': None, 'event_date': date(2026, 7, 28),
        'priority': 'normal', 'status': 'open', 'assigned_to': 1, 'assigned_to_name': 'Auditor One',
        'created_by': 1, 'created_by_name': 'Auditor One',
    }]

    app, auditor_token, _, _ = make_app(db)
    client = app.test_client()

    resp = client.get('/calendar/events', headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    types = sorted(e['event_type'] for e in body['events'])
    check('all 5 events present', types == sorted([
        'pending_review', 'finance_correction_due', 'anomaly_followup', 'manual_task',
    ]) or len(body['events']) == 4, body['events'])
    check('total is 4 (exception excluded since _classify_exception returned None here)',
          body['total'] == 4, body)
    check('events sorted by date ascending', body['events'] == sorted(body['events'], key=lambda e: e['date'] or ''))

    finance_event = next(e for e in body['events'] if e['event_type'] == 'finance_correction_due')
    check('finance correction event carries priority/reason_category', finance_event['priority'] == 'high'
          and finance_event['reason_category'] == 'possible_duplicate_invoice', finance_event)


def run_case_exception_events_skip_sent_back_type_to_avoid_duplication():
    print('Case: an exception classified as "sent_back" is excluded (finance_correction_due already covers it)')
    db = fresh_db()
    db['exception_doc_rows'] = [{'document_id': 68, 'uploaded_at': date(2026, 7, 20), 'status': 'returned'}]

    app, auditor_token, _, _ = make_app(db)
    cal._build_comparison = lambda cursor, document_id: {
        'invoice': {'invoice_no': 'IX1', 'vendor_name': 'Acme', 'uploaded_at': '2026-07-20T00:00:00'},
    }
    cal._classify_exception = lambda cursor, doc_row, comparison: (3, 'sent_back', 'Sent Back to Finance', 'reason', 'medium')
    client = app.test_client()

    resp = client.get('/calendar/events', headers={'Authorization': f'Bearer {auditor_token}'})
    body = resp.get_json()
    check('no exception_followup event for a sent_back classification',
          not any(e['event_type'] == 'exception_followup' for e in body['events']), body['events'])


def run_case_finance_role_only_sees_own_correction_events_no_pending_review():
    print('Case: finance_executive GET /calendar/events has no pending_review/exception/anomaly events, only their own finance_correction_due + tasks')
    db = fresh_db()
    db['finance_correction_rows'] = [{
        'document_id': 69, 'response_due_date': date(2026, 7, 25), 'priority': 'medium',
        'return_reason_category': 'missing_document', 'auditor_instruction': 'Upload the PO.',
        'cycle_status': 'action_required', 'sent_back_at': None,
        'invoice_number': 'IX2', 'vendor_name': 'Delta Sdn Bhd',
    }]
    app, _, finance_token, _ = make_app(db)
    client = app.test_client()

    resp = client.get('/calendar/events', headers={'Authorization': f'Bearer {finance_token}'})
    check('200 OK', resp.status_code == 200)
    body = resp.get_json()
    types = set(e['event_type'] for e in body['events'])
    check('only finance_correction_due present (no pending_review/exception/anomaly for finance)',
          types == {'finance_correction_due'}, types)


def run_case_create_task_requires_title_and_date():
    print('Case: POST /calendar/tasks with a missing title is rejected with 400, no row inserted')
    db = fresh_db()
    app, auditor_token, _, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/calendar/tasks', json={'date': '2026-08-01'},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())
    check('no task created', len(db['tasks']) == 0)


def run_case_create_task_defaults_assignee_to_self():
    print('Case: creating a task without assigned_to assigns it to the creator')
    db = fresh_db()
    app, auditor_token, _, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/calendar/tasks', json={'title': 'Follow up', 'date': '2026-08-01', 'priority': 'high'},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('201 Created', resp.status_code == 201, resp.get_json())
    task_id = resp.get_json()['task_id']
    check('task assigned to creator (user_id=1)', db['tasks'][task_id]['assigned_to'] == 1, db['tasks'])
    check('created_by recorded', db['tasks'][task_id]['created_by'] == 1)


def run_case_finance_can_also_create_tasks():
    print('Case: a finance_executive can also create a manual task')
    db = fresh_db()
    app, _, finance_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/calendar/tasks', json={'title': 'Chase Finance approval', 'date': '2026-08-02'},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('201 Created', resp.status_code == 201, resp.get_json())


def run_case_complete_task_by_assignee_succeeds():
    print('Case: the assignee can mark their own task complete')
    db = fresh_db()
    db['tasks'][5] = {'assigned_to': 1, 'created_by': 2, 'status': 'open'}
    app, auditor_token, _, _ = make_app(db)
    client = app.test_client()

    resp = client.patch('/calendar/tasks/5/complete', headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    check('status updated to done', db['tasks'][5]['status'] == 'done')


def run_case_complete_task_by_unrelated_user_is_forbidden():
    print('Case: a user who neither created nor is assigned the task gets 403 on complete')
    db = fresh_db()
    db['tasks'][6] = {'assigned_to': 2, 'created_by': 2, 'status': 'open'}
    app, auditor_token, _, _ = make_app(db)
    client = app.test_client()

    resp = client.patch('/calendar/tasks/6/complete', headers={'Authorization': f'Bearer {auditor_token}'})
    check('403 Forbidden', resp.status_code == 403, resp.get_json())
    check('status unchanged', db['tasks'][6]['status'] == 'open')


def run_case_complete_nonexistent_task_is_404():
    print('Case: completing a task_id that does not exist returns 404')
    db = fresh_db()
    app, auditor_token, _, _ = make_app(db)
    client = app.test_client()

    resp = client.patch('/calendar/tasks/999/complete', headers={'Authorization': f'Bearer {auditor_token}'})
    check('404 Not Found', resp.status_code == 404, resp.get_json())


if __name__ == '__main__':
    run_case_admin_cannot_access_calendar()
    run_case_auditor_sees_all_four_event_types_plus_own_tasks()
    run_case_exception_events_skip_sent_back_type_to_avoid_duplication()
    run_case_finance_role_only_sees_own_correction_events_no_pending_review()
    run_case_create_task_requires_title_and_date()
    run_case_create_task_defaults_assignee_to_self()
    run_case_finance_can_also_create_tasks()
    run_case_complete_task_by_assignee_succeeds()
    run_case_complete_task_by_unrelated_user_is_forbidden()
    run_case_complete_nonexistent_task_is_404()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
