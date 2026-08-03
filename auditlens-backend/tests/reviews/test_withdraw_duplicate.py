"""Route-level tests for Finance's duplicate-resolution actions
(routes/reviews.py::withdraw_duplicate / get_duplicate_suspect):
role restrictions, status-transition guards, the duplicate-only gate,
and — the compliance-critical part — that withdrawing a duplicate
never deletes a row and leaves the suspected original completely
untouched. Exercised through a REAL Flask test client hitting the REAL
route/view functions and JWT machinery, against a fully fake, in-memory
database (same pattern as tests/reviews/test_send_back_routes.py).

Usage:
    python tests/reviews/test_withdraw_duplicate.py
"""
import os
import sys
from datetime import datetime

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


class FakeCursor:
    """RealDictCursor-shaped stand-in — withdraw_duplicate()/
    get_duplicate_suspect() only ever use cursor_factory=RealDictCursor,
    so every row here is a dict, matching real psycopg2 behavior."""
    def __init__(self, db):
        self.db = db
        self._last = None

    def execute(self, sql, params=None):
        params = params or ()
        s = ' '.join(sql.split())

        if s == 'SELECT status FROM documents WHERE document_id = %s':
            doc = self.db['documents'].get(params[0])
            self._last = {'status': doc['status']} if doc else None

        elif s.startswith('SELECT cycle_id, return_reason_category FROM send_back_cycles'):
            doc_id = params[0]
            open_cycles = [c for c in self.db['send_back_cycles']
                           if c['document_id'] == doc_id and c['cycle_status'] == 'action_required']
            self._last = ({'cycle_id': (latest := max(open_cycles, key=lambda c: c['cycle_number']))['cycle_id'],
                            'return_reason_category': latest['return_reason_category']}
                          if open_cycles else None)

        elif s.startswith('UPDATE documents SET status = %s'):
            self.db['documents'][params[1]]['status'] = params[0]

        elif s.startswith('UPDATE send_back_cycles SET finance_response = %s'):
            note, responded_by, resolution, cycle_id = params
            for c in self.db['send_back_cycles']:
                if c['cycle_id'] == cycle_id:
                    c['finance_response'] = note
                    c['finance_responded_by'] = responded_by
                    c['cycle_status'] = 'resolved'
                    c['resolution'] = resolution

        elif s.startswith('INSERT INTO review_records'):
            self.db['next_review_id'] += 1
            review_id = self.db['next_review_id']
            self.db['review_records'].append({
                'review_id': review_id, 'document_id': params[0],
                'reviewed_by': params[1], 'action': 'closed', 'remarks': params[2],
            })
            self._last = {'review_id': review_id}

        elif s.startswith('SELECT detected_pattern FROM anomalies'):
            doc_id = params[0]
            matches = [a for a in self.db.get('anomalies', []) if a['invoice_document_id'] == doc_id]
            self._last = {'detected_pattern': matches[0]['detected_pattern']} if matches else None

        elif s.startswith('SELECT d.document_id, d.status, ef.invoice_number'):
            doc_id = params[0]
            doc = self.db['documents'].get(doc_id)
            self._last = ({'document_id': doc_id, 'status': doc['status'],
                            'invoice_number': doc.get('invoice_number'), 'vendor_name': doc.get('vendor_name'),
                            'invoice_date': doc.get('invoice_date'), 'total_amount': doc.get('total_amount'),
                            'currency': doc.get('currency')} if doc else None)

        else:
            raise AssertionError(f'FakeCursor: unhandled SQL: {s}  params={params}')

    def fetchone(self):
        return self._last

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


def fresh_db(document_status='returned', reason_category='possible_duplicate_invoice'):
    return {
        'documents': {
            1: {'document_id': 1, 'status': document_status},
            2: {'document_id': 2, 'status': 'approved', 'invoice_number': 'INV-ORIG-001',
                'vendor_name': 'Acme Sdn Bhd', 'invoice_date': None, 'total_amount': 1500.0, 'currency': 'RM'},
        },
        'send_back_cycles': [
            {'cycle_id': 1, 'document_id': 1, 'cycle_number': 1, 'cycle_status': 'action_required',
             'return_reason_category': reason_category, 'resolution': None,
             'finance_response': None, 'finance_responded_by': None},
        ],
        'review_records': [],
        'next_review_id': 0,
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


def run_case_withdraw_succeeds_for_open_duplicate_finding():
    print('Case: Withdraw This Duplicate succeeds when the open cycle is a possible-duplicate finding')
    db = fresh_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/withdraw-duplicate/1', json={'note': 'Uploaded twice by mistake.'},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    check("response status is withdrawn_duplicate", body.get('status') == 'withdrawn_duplicate', body)

    check("document 1 status -> withdrawn_duplicate", db['documents'][1]['status'] == 'withdrawn_duplicate')
    check("document 1 row still exists (not deleted)", 1 in db['documents'])

    cycle = db['send_back_cycles'][0]
    check("cycle closed (cycle_status=resolved)", cycle['cycle_status'] == 'resolved', cycle)
    check("cycle resolution=withdrawn_duplicate", cycle['resolution'] == 'withdrawn_duplicate', cycle)
    check("cycle finance_response carries the note", cycle['finance_response'] == 'Uploaded twice by mistake.', cycle)
    check("cycle finance_responded_by is the Finance user", cycle['finance_responded_by'] == 2, cycle)
    check("cycle row still exists (not deleted)", len(db['send_back_cycles']) == 1)

    check("one review_records row logged", len(db['review_records']) == 1, db['review_records'])
    rr = db['review_records'][0]
    check("review_records action='closed'", rr['action'] == 'closed', rr)
    check("review_records reviewed_by is the Finance user (audit trail: who)", rr['reviewed_by'] == 2, rr)
    check("review_records carries the note", rr['remarks'] == 'Uploaded twice by mistake.', rr)

    # The suspected original (document 2) must never be touched.
    check("original document (2) status untouched", db['documents'][2]['status'] == 'approved')


def run_case_withdraw_defaults_note_when_omitted():
    print('Case: Withdraw with no note supplied still records a default audit-trail remark, not blank')
    db = fresh_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/withdraw-duplicate/1', json={},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    check('review_records remarks is non-empty default text',
          bool(db['review_records'][0]['remarks']), db['review_records'])


def run_case_withdraw_rejected_for_non_duplicate_reason():
    print('Case: Withdraw This Duplicate is rejected (400) when the open cycle was NOT a duplicate finding — '
          'field correction/PO-GR replacement stays the only path for every other reason')
    db = fresh_db(reason_category='missing_document')
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/withdraw-duplicate/1', json={},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())
    check('document status unchanged', db['documents'][1]['status'] == 'returned')
    check('cycle untouched', db['send_back_cycles'][0]['cycle_status'] == 'action_required')
    check('no review_records row created', len(db['review_records']) == 0)


def run_case_withdraw_rejected_when_document_not_returned():
    print('Case: Withdraw This Duplicate is rejected (400) when the document is not currently returned')
    db = fresh_db(document_status='under_review')
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/withdraw-duplicate/1', json={},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())
    check('document status unchanged', db['documents'][1]['status'] == 'under_review')


def run_case_auditor_role_cannot_withdraw():
    print('Case: an auditor calling withdraw-duplicate gets 403 (Finance Executive only)')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/withdraw-duplicate/1', json={},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('403 Forbidden', resp.status_code == 403, resp.get_json())
    check('document status unchanged', db['documents'][1]['status'] == 'returned')


def run_case_withdraw_document_not_found():
    print('Case: withdraw-duplicate on a document_id that does not exist gets 404')
    db = fresh_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/withdraw-duplicate/999', json={},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('404 Not Found', resp.status_code == 404, resp.get_json())


def run_case_duplicate_suspect_returns_the_matched_original():
    print('Case: GET /reviews/duplicate-suspect surfaces the cached anomaly finding\'s matched original, no re-detection')
    db = fresh_db()
    db['anomalies'] = [{
        'invoice_document_id': 1,
        'detected_pattern': {
            'matched_document_id': 2, 'matched_invoice_no': 'INV-ORIG-001',
            'matched_date': '2026-01-10', 'days_apart': 2, 'amount_diff_pct': 0.0,
        },
    }]
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.get('/reviews/duplicate-suspect/1', headers={'Authorization': f'Bearer {finance_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    original = body.get('suspected_original')
    check('suspected_original present', original is not None, body)
    check('suspected_original is document 2', original and original.get('document_id') == 2, original)
    check('suspected_original invoice_number surfaced', original and original.get('invoice_number') == 'INV-ORIG-001', original)


def run_case_duplicate_suspect_none_when_no_anomaly_recorded():
    print('Case: GET /reviews/duplicate-suspect returns null suspected_original when no duplicate anomaly was ever raised '
          '(auditor chose the reason manually)')
    db = fresh_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.get('/reviews/duplicate-suspect/1', headers={'Authorization': f'Bearer {finance_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    check('suspected_original is null', resp.get_json().get('suspected_original') is None, resp.get_json())


if __name__ == '__main__':
    run_case_withdraw_succeeds_for_open_duplicate_finding()
    run_case_withdraw_defaults_note_when_omitted()
    run_case_withdraw_rejected_for_non_duplicate_reason()
    run_case_withdraw_rejected_when_document_not_returned()
    run_case_auditor_role_cannot_withdraw()
    run_case_withdraw_document_not_found()
    run_case_duplicate_suspect_returns_the_matched_original()
    run_case_duplicate_suspect_none_when_no_anomaly_recorded()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
