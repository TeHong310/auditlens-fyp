"""Route-level tests for the send-back workflow (routes/reviews.py):
role restrictions, status-transition guards, and multi-cycle history
preservation, exercised through a REAL Flask test client hitting the
REAL route/view functions and JWT machinery — but against a fully fake,
in-memory database. No real Postgres connection, no Claude/Gemini calls,
no reliance on app.py (which would touch a real DB at import time via
its _ensure_* startup migrations) — this builds its own minimal Flask
app registering only reviews_bp.

Usage:
    python tests/reviews/test_send_back_routes.py
"""
import os
import sys
import json as jsonlib
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

import routes.reviews as reviews_module
from routes.reviews import reviews_bp

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


# ── Fake DB: an in-memory stand-in for documents/review_records/
# send_back_cycles, just enough for these routes' exact queries. ──

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._last_result = None

    def execute(self, sql, params=None):
        params = params or ()
        s = ' '.join(sql.split())  # normalize whitespace for matching

        if s.startswith('SELECT status FROM documents WHERE document_id'):
            doc = self.db['documents'].get(params[0])
            self._last_result = (doc['status'],) if doc else None

        elif s.startswith('INSERT INTO review_records'):
            self.db['next_review_id'] += 1
            review_id = self.db['next_review_id']
            self.db['review_records'].append({
                'review_id': review_id, 'document_id': params[0],
                'reviewed_by': params[1], 'action': params[2], 'remarks': params[3],
            })
            self._last_result = {'review_id': review_id} if isinstance(self, DictFakeCursor) else (review_id,)

        elif s.startswith("UPDATE documents SET status = 'returned'"):
            self.db['documents'][params[0]]['status'] = 'returned'
        elif s.startswith("UPDATE documents SET status = 'resubmitted'"):
            self.db['documents'][params[0]]['status'] = 'resubmitted'
        elif s.startswith("UPDATE documents SET status = 'approved'"):
            self.db['documents'][params[0]]['status'] = 'approved'
        elif s.startswith('UPDATE exceptions'):
            pass

        elif s.startswith('SELECT cycle_id, cycle_number FROM send_back_cycles'):
            doc_id = params[0]
            cycles = [c for c in self.db['send_back_cycles'] if c['document_id'] == doc_id]
            if cycles:
                latest = max(cycles, key=lambda c: c['cycle_number'])
                self._last_result = (latest['cycle_id'], latest['cycle_number'])
            else:
                self._last_result = None

        elif s.startswith("UPDATE send_back_cycles SET cycle_status = 'resolved', resolution = 'returned_again'"):
            for c in self.db['send_back_cycles']:
                if c['cycle_id'] == params[0] and c['cycle_status'] != 'resolved':
                    c['cycle_status'] = 'resolved'
                    c['resolution'] = 'returned_again'

        elif s.startswith('INSERT INTO send_back_cycles'):
            self.db['next_cycle_id'] += 1
            cycle_id = self.db['next_cycle_id']
            self.db['send_back_cycles'].append({
                'cycle_id': cycle_id, 'document_id': params[0], 'cycle_number': params[1],
                'return_reason_category': params[2], 'reason_other_note': params[3],
                'auditor_instruction': params[4], 'required_actions': jsonlib.loads(params[5]),
                'required_action_other_note': params[6], 'priority': params[7],
                'response_due_date': params[8], 'sent_back_by': params[9],
                'cycle_status': 'action_required', 'resolution': None,
                'finance_response': None, 'finance_responded_by': None,
                'resubmitted_by': None,
            })
            self._last_result = (cycle_id,)

        elif s.startswith("SELECT cycle_id FROM send_back_cycles") and 'action_required' in s:
            doc_id = params[0]
            open_cycles = [c for c in self.db['send_back_cycles']
                           if c['document_id'] == doc_id and c['cycle_status'] == 'action_required']
            self._last_result = {'cycle_id': max(open_cycles, key=lambda c: c['cycle_number'])['cycle_id']} \
                if open_cycles else None

        elif s.startswith("SELECT cycle_id FROM send_back_cycles") and 'resubmitted' in s:
            doc_id = params[0]
            open_cycles = [c for c in self.db['send_back_cycles']
                           if c['document_id'] == doc_id and c['cycle_status'] == 'resubmitted']
            self._last_result = (max(open_cycles, key=lambda c: c['cycle_number'])['cycle_id'],) \
                if open_cycles else None

        elif s.startswith('UPDATE send_back_cycles SET finance_response'):
            for c in self.db['send_back_cycles']:
                if c['cycle_id'] == params[3]:
                    c['finance_response'] = params[0]
                    c['finance_responded_by'] = params[1]
                    c['resubmitted_by'] = params[2]
                    c['cycle_status'] = 'resubmitted'

        elif s.startswith("UPDATE send_back_cycles SET cycle_status = 'resolved', resolution = 'approved'"):
            for c in self.db['send_back_cycles']:
                if c['cycle_id'] == params[0]:
                    c['cycle_status'] = 'resolved'
                    c['resolution'] = 'approved'

        else:
            raise AssertionError(f'FakeCursor: unhandled SQL: {s}  params={params}')

    def fetchone(self):
        return self._last_result

    def close(self):
        pass


class DictFakeCursor(FakeCursor):
    """Same backing store, but fetchone() returns dict-shaped rows where
    the route code expects RealDictCursor behavior."""
    def fetchone(self):
        r = self._last_result
        if r is None or isinstance(r, dict):
            return r
        # SELECT status ... -> route reads doc['status']
        if len(r) == 1 and isinstance(r[0], str) and r[0] in ('under_review', 'resubmitted', 'returned', 'approved'):
            return {'status': r[0]}
        return r


class FakeConn:
    def __init__(self, db, dict_cursor=False):
        self.db = db
        self.dict_cursor = dict_cursor
        self.committed = False

    def cursor(self, cursor_factory=None):
        cls = DictFakeCursor if (cursor_factory is not None or self.dict_cursor) else FakeCursor
        return cls(self.db)

    def commit(self):
        self.committed = True

    def close(self):
        pass


def fresh_db():
    return {
        'documents': {1: {'document_id': 1, 'status': 'under_review'}},
        'review_records': [],
        'send_back_cycles': [],
        'next_review_id': 0,
        'next_cycle_id': 0,
    }


def make_app(db):
    app = Flask(__name__)
    app.config['JWT_SECRET_KEY'] = 'test-secret-key-at-least-32-bytes-long-for-hs256'
    app.config['TESTING'] = True
    JWTManager(app)
    app.register_blueprint(reviews_bp, url_prefix='/reviews')

    reviews_module.get_db_connection = lambda: FakeConn(db)
    reviews_module.get_user_by_id = lambda uid: (
        {'user_id': 1, 'role': 'auditor', 'full_name': 'Auditor One'} if uid == '1'
        else {'user_id': 2, 'role': 'finance_executive', 'full_name': 'Finance One'}
    )
    reviews_module.log_audit = lambda *a, **k: None

    with app.app_context():
        auditor_token = create_access_token(identity='1')
        finance_token = create_access_token(identity='2')

    return app, auditor_token, finance_token


VALID_SEND_BACK = {
    'reason_category': 'possible_duplicate_invoice',
    'instruction': 'Confirm whether this invoice was uploaded twice.',
    'required_actions': ['provide_written_explanation', 'confirm_duplicate_submission'],
    'priority': 'high',
    'due_date': (date.today() + timedelta(days=3)).isoformat(),
}


def run_case_valid_send_back_creates_cycle_and_status():
    print('Case: a valid structured send-back request creates cycle #1 and flips status to returned')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/return/1', json=VALID_SEND_BACK,
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    check('cycle_number is 1', body.get('cycle_number') == 1, body)
    check('document status is returned', db['documents'][1]['status'] == 'returned')
    check('one send_back_cycles row created', len(db['send_back_cycles']) == 1, db['send_back_cycles'])
    cycle = db['send_back_cycles'][0]
    check('reason_category stored', cycle['return_reason_category'] == 'possible_duplicate_invoice')
    check('required_actions stored as list', cycle['required_actions'] == VALID_SEND_BACK['required_actions'])
    check('cycle_status starts action_required', cycle['cycle_status'] == 'action_required')


def run_case_send_back_missing_required_fields_rejected():
    print('Case: a send-back request missing instruction/required_actions is rejected with 400')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    bad_payload = {'reason_category': 'other', 'instruction': ''}
    resp = client.post('/reviews/return/1', json=bad_payload,
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())
    check('document status unchanged', db['documents'][1]['status'] == 'under_review')
    check('no cycle created', len(db['send_back_cycles']) == 0)


def run_case_send_back_invalid_due_date_rejected():
    print('Case: a due_date earlier than today is rejected with 400')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    payload = dict(VALID_SEND_BACK, due_date='2020-01-01')
    resp = client.post('/reviews/return/1', json=payload,
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())


def run_case_finance_role_cannot_send_back():
    print('Case: a finance_executive calling send-back gets 403 Access denied')
    db = fresh_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/return/1', json=VALID_SEND_BACK,
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('403 Forbidden', resp.status_code == 403, resp.get_json())


def run_case_auditor_role_cannot_resubmit():
    print('Case: an auditor calling resubmit gets 403 Access denied (role restriction)')
    db = fresh_db()
    db['documents'][1]['status'] = 'returned'
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/resubmit/1', json={'response': 'Fixed it.'},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('403 Forbidden', resp.status_code == 403, resp.get_json())


def run_case_resubmit_without_response_rejected_when_cycle_open():
    print('Case: Finance resubmitting a document with an open cycle but no response text gets 400')
    db = fresh_db()
    app, auditor_token, finance_token = make_app(db)
    client = app.test_client()

    client.post('/reviews/return/1', json=VALID_SEND_BACK,
                headers={'Authorization': f'Bearer {auditor_token}'})

    resp = client.post('/reviews/resubmit/1', json={'response': '   '},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())
    check('document status still returned', db['documents'][1]['status'] == 'returned')


def run_case_finance_cannot_resubmit_a_record_never_sent_back():
    print('Case: resubmitting a document that was never sent back (still under_review) is an invalid transition -> 400')
    db = fresh_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/resubmit/1', json={'response': 'N/A'},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())


def run_case_full_cycle_response_resubmit_approve():
    print('Case: full happy path — send back -> finance response + resubmit -> auditor approve resolves the cycle')
    db = fresh_db()
    app, auditor_token, finance_token = make_app(db)
    client = app.test_client()

    r1 = client.post('/reviews/return/1', json=VALID_SEND_BACK,
                      headers={'Authorization': f'Bearer {auditor_token}'})
    check('send-back OK', r1.status_code == 200)

    r2 = client.post('/reviews/resubmit/1',
                      json={'response': 'The duplicate invoice was withdrawn; no payment was made.'},
                      headers={'Authorization': f'Bearer {finance_token}'})
    check('resubmit OK', r2.status_code == 200, r2.get_json())
    check('document status resubmitted', db['documents'][1]['status'] == 'resubmitted')
    cycle = db['send_back_cycles'][0]
    check('finance_response saved onto the cycle', cycle['finance_response'] == 'The duplicate invoice was withdrawn; no payment was made.')
    check('cycle_status resubmitted', cycle['cycle_status'] == 'resubmitted')

    r3 = client.post('/reviews/approve/1', json={},
                      headers={'Authorization': f'Bearer {auditor_token}'})
    check('approve OK', r3.status_code == 200, r3.get_json())
    check('document status approved', db['documents'][1]['status'] == 'approved')
    check('cycle resolved as approved', cycle['cycle_status'] == 'resolved' and cycle['resolution'] == 'approved')


def run_case_multiple_send_back_cycles_preserve_history():
    print('Case: a second send-back after a response creates cycle #2 and resolves cycle #1 as returned_again — cycle #1 is never overwritten')
    db = fresh_db()
    app, auditor_token, finance_token = make_app(db)
    client = app.test_client()

    client.post('/reviews/return/1', json=VALID_SEND_BACK,
                headers={'Authorization': f'Bearer {auditor_token}'})
    client.post('/reviews/resubmit/1', json={'response': 'First fix attempt.'},
                headers={'Authorization': f'Bearer {finance_token}'})

    second_send_back = dict(VALID_SEND_BACK, instruction='Still not resolved — please clarify further.')
    r = client.post('/reviews/return/1', json=second_send_back,
                     headers={'Authorization': f'Bearer {auditor_token}'})
    check('second send-back OK', r.status_code == 200, r.get_json())
    check('cycle_number is 2', r.get_json().get('cycle_number') == 2, r.get_json())
    check('two cycles exist', len(db['send_back_cycles']) == 2, db['send_back_cycles'])

    cycle1, cycle2 = db['send_back_cycles']
    check('cycle 1 resolved as returned_again', cycle1['cycle_status'] == 'resolved' and cycle1['resolution'] == 'returned_again')
    check('cycle 1 original instruction preserved, not overwritten',
          cycle1['auditor_instruction'] == VALID_SEND_BACK['instruction'])
    check('cycle 1 finance_response preserved, not erased', cycle1['finance_response'] == 'First fix attempt.')
    check('cycle 2 is a fresh action_required cycle', cycle2['cycle_status'] == 'action_required')
    check('cycle 2 has the new instruction', cycle2['auditor_instruction'] == second_send_back['instruction'])


def run_case_legacy_remarks_only_payload_still_works():
    print('Case: the old {"remarks": "..."} payload shape still works (backward compatibility) and creates no cycle')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/return/1', json={'remarks': 'Old-style free-text reason.'},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    check('document status returned', db['documents'][1]['status'] == 'returned')
    check('no cycle created for legacy call', len(db['send_back_cycles']) == 0)
    check('review_records still logged the remarks', db['review_records'][0]['remarks'] == 'Old-style free-text reason.')


if __name__ == '__main__':
    run_case_valid_send_back_creates_cycle_and_status()
    run_case_send_back_missing_required_fields_rejected()
    run_case_send_back_invalid_due_date_rejected()
    run_case_finance_role_cannot_send_back()
    run_case_auditor_role_cannot_resubmit()
    run_case_resubmit_without_response_rejected_when_cycle_open()
    run_case_finance_cannot_resubmit_a_record_never_sent_back()
    run_case_full_cycle_response_resubmit_approve()
    run_case_multiple_send_back_cycles_preserve_history()
    run_case_legacy_remarks_only_payload_still_works()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
