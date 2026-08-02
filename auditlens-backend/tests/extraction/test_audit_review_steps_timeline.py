"""Regression tests for the Audit Review page's guided/sequential
review checklist, end to end against the real local Postgres dev DB:
  - routes/reviews.py::mark_review_step (POST /reviews/review-steps/
    <document_id>/<step>) — already covered in isolation by tests/
    reviews/test_review_steps.py (fake DB); this file instead proves
    the real document_review_steps table + the real GET /documents/
    <id>/timeline endpoint agree with each other.
  - routes/documents.py::get_document_timeline — the NEW review_steps
    field in its response, purely additive (the pre-existing `events`
    array, which Finance Correction Detail's WorkflowTimelineComponent
    depends on, is asserted unchanged).

Integration tests use the real local Postgres dev DB and a real Flask
test client (same permitted convention as test_transaction_auditor_
integration.py). Every test creates its own rows and deletes them in
__exit__, leaving the DB exactly as found.

Usage:
    python tests/extraction/test_audit_review_steps_timeline.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from db import get_db_connection
import routes.documents as documents_module
import routes.reviews as reviews_module
from routes.documents import documents_bp
from routes.reviews import reviews_bp

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class _Fixture:
    def __init__(self):
        self.doc_ids = []
        self.uid_auditor = None

    def __enter__(self):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE email = 'review_steps_fixture_auditor@x.com'")
        row = cur.fetchone()
        if row:
            self.uid_auditor = row[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, role, full_name) "
                "VALUES ('review_steps_fixture_auditor@x.com', 'x', 'auditor', 'Review Steps Fixture Auditor') "
                "RETURNING user_id"
            )
            self.uid_auditor = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return self

    def invoice(self, invoice_number='INV-RS-1', status='under_review'):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO documents (uploaded_by, file_name, file_path, file_type, input_method, status) "
            "VALUES (%s, %s, %s, 'pdf', 'upload', %s) RETURNING document_id",
            (self.uid_auditor, f'{invoice_number}.pdf', f'/tmp/{invoice_number}.pdf', status))
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO extracted_fields (document_id, invoice_number, vendor_name, total_amount, currency) "
            "VALUES (%s, %s, 'Acme Corp', 100.0, 'RM')",
            (doc_id, invoice_number))
        conn.commit()
        conn.close()
        self.doc_ids.append(doc_id)
        return doc_id

    def __exit__(self, *exc):
        conn = get_db_connection()
        cur = conn.cursor()
        if self.doc_ids:
            cur.execute('DELETE FROM document_review_steps WHERE document_id = ANY(%s)', (self.doc_ids,))
            cur.execute('DELETE FROM extracted_fields WHERE document_id = ANY(%s)', (self.doc_ids,))
            cur.execute('DELETE FROM documents WHERE document_id = ANY(%s)', (self.doc_ids,))
        conn.commit()
        conn.close()


def _skip_if_db_unavailable(fn):
    def wrapped():
        try:
            conn = get_db_connection()
            conn.close()
        except Exception as e:
            print(f'  SKIP {fn.__name__} (no DB available: {type(e).__name__})')
            return
        fn()
    return wrapped


_test_app = Flask(__name__)
_test_app.config['JWT_SECRET_KEY'] = 'review-steps-timeline-test-secret-not-for-prod'
JWTManager(_test_app)
_test_app.register_blueprint(documents_bp, url_prefix='/documents')
_test_app.register_blueprint(reviews_bp, url_prefix='/reviews')
_test_client = _test_app.test_client()


def _auditor_headers(fx):
    with _test_app.app_context():
        token = create_access_token(identity=str(fx.uid_auditor))
    return {'Authorization': f'Bearer {token}'}


@_skip_if_db_unavailable
def run_case_timeline_review_steps_empty_before_any_step_marked():
    print('Case: a fresh invoice with no review steps marked -> GET timeline returns an empty review_steps dict, events untouched')
    with _Fixture() as fx:
        inv = fx.invoice('INV-RS-EMPTY')
        headers = _auditor_headers(fx)

        resp = _test_client.get(f'/documents/{inv}/timeline', headers=headers)
        check('200 OK', resp.status_code == 200, resp.get_json())
        body = resp.get_json()
        check('review_steps is present and empty', body.get('review_steps') == {}, body)
        check('events array still present and non-empty (unchanged for Finance Correction Detail)',
              isinstance(body.get('events'), list) and len(body['events']) > 0, body)


@_skip_if_db_unavailable
def run_case_timeline_reflects_marked_steps_with_reviewer_and_timestamp():
    print('Case: marking three_way_matching then exception_review via the real endpoint -> GET timeline reflects both, in document_review_steps, reviewer name included')
    with _Fixture() as fx:
        inv = fx.invoice('INV-RS-MARKED')
        headers = _auditor_headers(fx)

        r1 = _test_client.post(f'/reviews/review-steps/{inv}/three_way_matching', json={}, headers=headers)
        check('mark three_way_matching succeeds', r1.status_code == 200, r1.get_json())
        r2 = _test_client.post(f'/reviews/review-steps/{inv}/exception_review', json={}, headers=headers)
        check('mark exception_review succeeds (three_way_matching already done)', r2.status_code == 200, r2.get_json())

        resp = _test_client.get(f'/documents/{inv}/timeline', headers=headers)
        body = resp.get_json()
        steps = body.get('review_steps') or {}
        check('three_way_matching present in review_steps', 'three_way_matching' in steps, steps)
        check('exception_review present in review_steps', 'exception_review' in steps, steps)
        check('authenticity_review NOT present (never marked)', 'authenticity_review' not in steps, steps)
        check('reviewer_name is the fixture auditor', steps['three_way_matching']['reviewer_name'] == 'Review Steps Fixture Auditor', steps)
        check('reviewed_at is a real timestamp string', bool(steps['three_way_matching']['reviewed_at']), steps)

        # document_review_steps in the DB agrees exactly with the API response.
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT step FROM document_review_steps WHERE document_id = %s ORDER BY step', (inv,))
        db_steps = sorted(r[0] for r in cur.fetchall())
        conn.close()
        check('DB rows match the API-reported steps exactly',
              db_steps == sorted(steps.keys()), (db_steps, list(steps.keys())))


@_skip_if_db_unavailable
def run_case_sequential_gate_enforced_through_the_real_endpoint():
    print('Case: the real endpoint rejects marking anomaly_review before its prior steps, and the timeline never shows it as reviewed')
    with _Fixture() as fx:
        inv = fx.invoice('INV-RS-GATED')
        headers = _auditor_headers(fx)

        resp = _test_client.post(f'/reviews/review-steps/{inv}/anomaly_review', json={}, headers=headers)
        check('400 Bad Request (skipped ahead)', resp.status_code == 400, resp.get_json())

        timeline = _test_client.get(f'/documents/{inv}/timeline', headers=headers).get_json()
        check('anomaly_review is not in review_steps', 'anomaly_review' not in (timeline.get('review_steps') or {}), timeline)


if __name__ == '__main__':
    run_case_timeline_review_steps_empty_before_any_step_marked()
    run_case_timeline_reflects_marked_steps_with_reviewer_and_timestamp()
    run_case_sequential_gate_enforced_through_the_real_endpoint()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
