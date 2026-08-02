"""Route-level tests for the Audit Review page's guided/sequential
review checklist (routes/reviews.py::mark_review_step, POST /reviews/
review-steps/<document_id>/<step>): role restriction, sequential-order
enforcement, upsert-on-remark behavior — exercised through a REAL Flask
test client hitting the REAL route function and JWT machinery, against
a fully fake, in-memory database. No real Postgres connection, no
Claude/Gemini calls (same convention as tests/reviews/test_send_back_
routes.py and tests/extraction/test_anomaly_decision_mapping.py).

Usage:
    python tests/reviews/test_review_steps.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

import routes.reviews as reviews_module
from routes.reviews import reviews_bp, REVIEW_STEP_ORDER

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._last_result = None

    def execute(self, sql, params=None):
        params = params or ()
        s = ' '.join(sql.split())

        if s.startswith('SELECT document_id FROM documents WHERE document_id'):
            doc = self.db['documents'].get(params[0])
            self._last_result = {'document_id': params[0]} if doc else None

        elif s.startswith('SELECT step FROM document_review_steps WHERE document_id = %s AND step = ANY'):
            doc_id, wanted_steps = params
            done = [
                {'step': row['step']} for row in self.db['review_steps']
                if row['document_id'] == doc_id and row['step'] in wanted_steps
            ]
            self._last_result = done  # fetchall() reads this list directly

        elif s.startswith('INSERT INTO document_review_steps'):
            document_id, step, reviewed_by = params
            existing = next((r for r in self.db['review_steps']
                              if r['document_id'] == document_id and r['step'] == step), None)
            reviewed_at = f'ts-for-{step}-{len(self.db["review_steps"])}'
            if existing:
                existing['reviewed_by'] = reviewed_by
                existing['reviewed_at'] = reviewed_at
            else:
                self.db['review_steps'].append({
                    'document_id': document_id, 'step': step,
                    'reviewed_by': reviewed_by, 'reviewed_at': reviewed_at,
                })
            self._last_result = {'reviewed_at': _FakeTimestamp(reviewed_at)}

        else:
            raise AssertionError(f'FakeCursor: unhandled SQL: {s}  params={params}')

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return self._last_result if isinstance(self._last_result, list) else []

    def close(self):
        pass


class _FakeTimestamp:
    """Stand-in for a psycopg2 datetime — only .isoformat() is ever
    called on the mark_review_step response value."""
    def __init__(self, label):
        self.label = label

    def isoformat(self):
        return self.label


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.db)

    def commit(self):
        pass

    def close(self):
        pass


def fresh_db():
    return {
        'documents': {1: {'document_id': 1, 'status': 'under_review'}},
        'review_steps': [],
    }


def make_app(db):
    app = Flask(__name__)
    app.config['JWT_SECRET_KEY'] = 'test-secret-key-at-least-32-bytes-long-for-hs256'
    app.config['TESTING'] = True
    JWTManager(app)
    app.register_blueprint(reviews_bp, url_prefix='/reviews')

    reviews_module.get_db_connection = lambda: FakeConn(db)
    reviews_module.get_user_by_id = lambda uid: (
        {'user_id': 9, 'role': 'auditor', 'full_name': 'Auditor One'} if uid == '9'
        else {'user_id': 2, 'role': 'finance_executive', 'full_name': 'Finance One'}
    )
    reviews_module.log_audit = lambda *a, **k: None

    with app.app_context():
        auditor_token = create_access_token(identity='9')
        finance_token = create_access_token(identity='2')

    return app, auditor_token, finance_token


def run_case_first_step_marks_reviewed_with_no_prior_gate():
    print('Case: three_way_matching (first in order) can be marked reviewed immediately, no prior step required')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/review-steps/1/three_way_matching', json={},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    check('step echoed back', body.get('step') == 'three_way_matching', body)
    check('reviewed_by is the auditor', body.get('reviewed_by') == 9, body)
    check('reviewer_name included', body.get('reviewer_name') == 'Auditor One', body)
    check('reviewed_at included', 'reviewed_at' in body, body)
    check('one row persisted', len(db['review_steps']) == 1, db['review_steps'])


def run_case_later_step_rejected_until_earlier_steps_marked():
    print('Case: exception_review is rejected with 400 until three_way_matching is marked reviewed first')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/review-steps/1/exception_review', json={},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())
    check('error names the missing prior step', 'three way matching' in resp.get_json().get('error', '').lower(),
          resp.get_json())
    check('nothing persisted', len(db['review_steps']) == 0, db['review_steps'])


def run_case_full_sequential_order_unlocks_one_step_at_a_time():
    print('Case: marking steps in order (three_way_matching -> exception_review -> authenticity_review -> anomaly_review) succeeds at each stage')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()
    headers = {'Authorization': f'Bearer {auditor_token}'}

    for i, step in enumerate(REVIEW_STEP_ORDER):
        resp = client.post(f'/reviews/review-steps/1/{step}', json={}, headers=headers)
        check(f'step {i + 1}/{len(REVIEW_STEP_ORDER)} ({step}) succeeds once its predecessors are done',
              resp.status_code == 200, resp.get_json())
    check('all 4 steps persisted', len(db['review_steps']) == 4, db['review_steps'])


def run_case_skipping_ahead_out_of_order_is_rejected():
    print('Case: marking anomaly_review with only three_way_matching done (skipping exception_review/authenticity_review) is rejected')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()
    headers = {'Authorization': f'Bearer {auditor_token}'}

    client.post('/reviews/review-steps/1/three_way_matching', json={}, headers=headers)
    resp = client.post('/reviews/review-steps/1/anomaly_review', json={}, headers=headers)
    check('400 Bad Request', resp.status_code == 400, resp.get_json())
    check('error names the first missing prior step (exception_review)',
          'exception review' in resp.get_json().get('error', '').lower(), resp.get_json())


def run_case_remarking_an_already_reviewed_step_updates_reviewer_and_timestamp():
    print('Case: marking an already-reviewed step again is an upsert (reviewer/timestamp update), not a duplicate row')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()
    headers = {'Authorization': f'Bearer {auditor_token}'}

    client.post('/reviews/review-steps/1/three_way_matching', json={}, headers=headers)
    resp = client.post('/reviews/review-steps/1/three_way_matching', json={}, headers=headers)
    check('200 OK on re-mark', resp.status_code == 200, resp.get_json())
    check('still exactly one row (upsert, not a duplicate)', len(db['review_steps']) == 1, db['review_steps'])


def run_case_finance_cannot_mark_review_steps():
    print('Case: a finance_executive calling mark_review_step gets 403 (auditor-only)')
    db = fresh_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/review-steps/1/three_way_matching', json={},
                        headers={'Authorization': f'Bearer {finance_token}'})
    check('403 Forbidden', resp.status_code == 403, resp.get_json())


def run_case_invalid_step_name_rejected():
    print('Case: an unrecognized step name is rejected with 400')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/review-steps/1/not_a_real_step', json={},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())


def run_case_document_not_found_rejected():
    print('Case: marking a step for a document that does not exist gets 404')
    db = fresh_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/review-steps/9999/three_way_matching', json={},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('404 Not Found', resp.status_code == 404, resp.get_json())


if __name__ == '__main__':
    run_case_first_step_marks_reviewed_with_no_prior_gate()
    run_case_later_step_rejected_until_earlier_steps_marked()
    run_case_full_sequential_order_unlocks_one_step_at_a_time()
    run_case_skipping_ahead_out_of_order_is_rejected()
    run_case_remarking_an_already_reviewed_step_updates_reviewer_and_timestamp()
    run_case_finance_cannot_mark_review_steps()
    run_case_invalid_step_name_rejected()
    run_case_document_not_found_rejected()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
