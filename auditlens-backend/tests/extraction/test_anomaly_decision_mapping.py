"""Regression tests for the "one anomaly source of truth" fix:

  - routes/anomalies.py::get_anomaly_stats — the summary counts every
    anomaly record regardless of severity/type/status query-string
    filters (Section A: the list filter must never make the summary
    read as empty).
  - routes/reviews.py::approve_document / return_document /
    need_review_document — the Auditor's three final-decision controls
    map onto anomaly status exactly as required (Section E): Approve
    resolves pending anomalies to reviewed without touching already
    reviewed/dismissed rows; Send Back and Need Review both leave every
    anomaly status untouched (the issue isn't resolved yet).

Exercised through REAL Flask test clients hitting the REAL route/view
functions and JWT machinery, against a fully fake, in-memory database —
no real Postgres connection, no reliance on app.py. No real AI calls.

Usage:
    python tests/extraction/test_anomaly_decision_mapping.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

import routes.reviews as reviews_module
import routes.anomalies as anomalies_module
from routes.reviews import reviews_bp
from routes.anomalies import anomalies_bp

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


# ── Fake DB / cursors ──────────────────────────────────────────────
# Two cursor shapes, matching the two call styles used by the real
# routes: reviews.py's approve/return/need-review use a plain
# conn.cursor() (tuple rows); anomalies.py's stats endpoint uses
# conn.cursor(cursor_factory=RealDictCursor) (dict rows).

class ReviewsCursor:
    def __init__(self, db):
        self.db = db
        self._last = None

    def execute(self, sql, params=None):
        params = params or ()
        s = ' '.join(sql.split())

        if s.startswith('SELECT status FROM documents WHERE document_id'):
            doc = self.db['documents'].get(params[0])
            self._last = (doc['status'],) if doc else None

        elif s.startswith('INSERT INTO review_records'):
            self.db['next_review_id'] += 1
            review_id = self.db['next_review_id']
            self.db['review_records'].append({
                'review_id': review_id, 'document_id': params[0], 'reviewed_by': params[1],
                'action': params[2], 'remarks': params[3],
            })
            self._last = (review_id,)

        elif s.startswith("UPDATE documents SET status = 'approved'"):
            self.db['documents'][params[0]]['status'] = 'approved'
        elif s.startswith("UPDATE documents SET status = 'returned'"):
            self.db['documents'][params[0]]['status'] = 'returned'

        elif s.startswith('UPDATE exceptions'):
            pass

        elif s.startswith("UPDATE anomalies SET status = 'reviewed'"):
            reviewer, doc_id = params[0], params[1]
            for a in self.db['anomalies']:
                if a['invoice_document_id'] == doc_id and a['status'] == 'pending':
                    a['status'] = 'reviewed'
                    a['reviewed_by'] = reviewer
                    a['reviewed_at'] = 'just_now'

        elif s.startswith('SELECT cycle_id FROM send_back_cycles') and 'resubmitted' in s:
            self._last = None  # no open send-back cycle in these fixtures

        else:
            raise AssertionError(f'ReviewsCursor: unhandled SQL: {s}  params={params}')

    def fetchone(self):
        return self._last

    def close(self):
        pass


class AnomalyStatsCursor:
    """Simulates the real JOIN anomalies a JOIN documents d ON
    a.invoice_document_id = d.document_id — _valid_anomalies() below is
    the fake-DB equivalent of that join, so an orphan anomaly (an
    invoice_document_id with no matching row in db['documents']) is
    excluded from every count exactly like the real INNER JOIN would."""
    def __init__(self, db):
        self.db = db
        self._last = None
        self._rows = None

    def _valid_anomalies(self):
        """Excludes both an orphan anomaly (no matching documents row)
        and one whose document has been withdrawn as a confirmed
        duplicate (status='withdrawn_duplicate') — the fake-DB
        equivalent of the real `JOIN documents d ... WHERE d.status !=
        'withdrawn_duplicate'` every stats query now runs."""
        docs = self.db.get('documents', {})
        return [a for a in self.db['anomalies']
                if a['invoice_document_id'] in docs
                and docs[a['invoice_document_id']].get('status') != 'withdrawn_duplicate']

    def _active_doc_ids(self):
        return {doc_id for doc_id, d in self.db.get('documents', {}).items()
                if d.get('status') != 'withdrawn_duplicate'}

    def execute(self, sql, params=None):
        s = ' '.join(sql.split())

        if s == ("SELECT COUNT(*) AS cnt FROM anomalies a JOIN documents d "
                  "ON a.invoice_document_id = d.document_id WHERE d.status != 'withdrawn_duplicate'"):
            self._last = {'cnt': len(self._valid_anomalies())}
        elif s == ("SELECT a.severity, COUNT(*) AS cnt FROM anomalies a "
                    "JOIN documents d ON a.invoice_document_id = d.document_id "
                    "WHERE d.status != 'withdrawn_duplicate' GROUP BY a.severity"):
            self._rows = _group_counts(self._valid_anomalies(), 'severity')
        elif s == ("SELECT a.anomaly_type, COUNT(*) AS cnt FROM anomalies a "
                    "JOIN documents d ON a.invoice_document_id = d.document_id "
                    "WHERE d.status != 'withdrawn_duplicate' GROUP BY a.anomaly_type"):
            self._rows = _group_counts(self._valid_anomalies(), 'anomaly_type')
        elif s == ("SELECT a.status, COUNT(*) AS cnt FROM anomalies a "
                    "JOIN documents d ON a.invoice_document_id = d.document_id "
                    "WHERE d.status != 'withdrawn_duplicate' GROUP BY a.status"):
            self._rows = _group_counts(self._valid_anomalies(), 'status')
        elif s == ("SELECT COUNT(DISTINCT adr.invoice_document_id) AS cnt, MAX(adr.run_at) AS last_run "
                    "FROM anomaly_detection_runs adr JOIN documents d ON adr.invoice_document_id = d.document_id "
                    "WHERE d.status != 'withdrawn_duplicate'"):
            if self.db.get('anomaly_detection_runs_raises'):
                raise RuntimeError('simulated anomaly_detection_runs failure (e.g. missing/broken table)')
            active_ids = self._active_doc_ids()
            runs = [r for r in self.db['anomaly_detection_runs'] if r['invoice_document_id'] in active_ids]
            doc_ids = {r['invoice_document_id'] for r in runs}
            self._last = {
                'cnt': len(doc_ids),
                'last_run': max((r['run_at'] for r in runs), default=None),
            }
        elif s == ("SELECT COUNT(DISTINCT a.invoice_document_id) AS cnt FROM anomalies a "
                    "JOIN documents d ON a.invoice_document_id = d.document_id "
                    "WHERE d.status != 'withdrawn_duplicate'"):
            self._last = {'cnt': len({a['invoice_document_id'] for a in self._valid_anomalies()})}
        else:
            raise AssertionError(f'AnomalyStatsCursor: unhandled SQL: {s}')

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._rows or []

    def close(self):
        pass


def _group_counts(rows, field):
    counts = {}
    for r in rows:
        counts[r[field]] = counts.get(r[field], 0) + 1
    return [{field: k, 'cnt': v} for k, v in counts.items()]


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, cursor_factory=None):
        return AnomalyStatsCursor(self.db) if cursor_factory is not None else ReviewsCursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def make_app(db):
    app = Flask(__name__)
    app.config['JWT_SECRET_KEY'] = 'test-secret-key-at-least-32-bytes-long-for-hs256'
    app.config['TESTING'] = True
    JWTManager(app)
    app.register_blueprint(reviews_bp, url_prefix='/reviews')
    app.register_blueprint(anomalies_bp, url_prefix='/anomalies')

    reviews_module.get_db_connection = lambda: FakeConn(db)
    reviews_module.get_user_by_id = lambda uid: (
        {'user_id': 9, 'role': 'auditor', 'full_name': 'Auditor One'} if uid == '9'
        else {'user_id': 2, 'role': 'finance_executive', 'full_name': 'Finance One'}
    )
    reviews_module.log_audit = lambda *a, **k: None

    anomalies_module.get_db_connection = lambda: FakeConn(db)
    anomalies_module.get_user_by_id = reviews_module.get_user_by_id

    with app.app_context():
        auditor_token = create_access_token(identity='9')
        finance_token = create_access_token(identity='2')

    return app, auditor_token, finance_token


# ── Section A: stats independent of filters, matches required numbers ──

def stats_db():
    """Mirrors the task's own worked example exactly: one existing
    Round Amount anomaly, already reviewed — nothing pending, nothing
    dismissed. Required result: Anomalies Detected: 1, Round Amount: 1,
    Pending Review: 0, Reviewed list: 1 record."""
    return {
        'documents': {3753: {'document_id': 3753, 'status': 'under_review'}},
        'anomalies': [
            {'anomaly_id': 42, 'invoice_document_id': 3753, 'status': 'reviewed',
             'severity': 'medium', 'anomaly_type': 'round', 'reviewed_by': 9, 'reviewed_at': 'prior'},
        ],
        'extracted_fields': [{'document_id': 3753}],
        'anomaly_detection_runs': [{'invoice_document_id': 3753, 'run_at': datetime(2026, 2, 11, 10, 0, 0)}],
    }


def run_case_stats_matches_required_numbers_for_the_reviewed_round_amount_case():
    print('Case: /anomalies/stats on the INV-NBI-2026-0017 scenario reports Detected:1, Round Amount:1, Pending:0')
    db = stats_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.get('/anomalies/stats', headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    check('total (Anomalies Detected) is 1', body['total'] == 1, body)
    check('by_type.round (Round Amount) is 1', body['by_type']['round'] == 1, body)
    check('pending (Pending Review) is 0', body['pending'] == 0, body)
    check('by_status.reviewed is 1', body['by_status']['reviewed'] == 1, body)
    check('transactions_analysed counts the screened invoice', body['transactions_analysed'] == 1, body)
    check('last_analysed is a real timestamp, not Never/null', body['last_analysed'] is not None, body)


def run_case_stats_identical_regardless_of_active_list_filter_query_params():
    print('Case: stats totals never change no matter which list filter (status/severity/type) is active')
    db = stats_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()
    headers = {'Authorization': f'Bearer {auditor_token}'}

    baseline = client.get('/anomalies/stats', headers=headers).get_json()
    for query in ('?status=pending', '?status=dismissed', '?severity=high',
                  '?type=duplicate', '?status=pending&severity=low&type=amount'):
        filtered = client.get(f'/anomalies/stats{query}', headers=headers).get_json()
        check(f'stats unchanged with {query} active', filtered == baseline, (query, filtered, baseline))


def run_case_stats_pending_filter_empty_state_data_is_available():
    print('Case: with only a reviewed anomaly on record, by_status gives the frontend what it needs for the "no pending, 1 reviewed available" empty-state message')
    db = stats_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    body = client.get('/anomalies/stats', headers={'Authorization': f'Bearer {auditor_token}'}).get_json()
    check('by_status.pending is 0 (Pending filter would show none)', body['by_status']['pending'] == 0, body)
    check('by_status.reviewed is 1 (the reviewed anomaly the empty-state message should mention)',
          body['by_status']['reviewed'] == 1, body)


def run_case_stats_returns_200_when_anomaly_detection_runs_is_empty():
    print('Case: anomaly_detection_runs has zero rows -> still 200, transactions_analysed falls back to distinct invoice_document_id from anomalies')
    db = stats_db()
    db['anomaly_detection_runs'] = []
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.get('/anomalies/stats', headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK even with an empty anomaly_detection_runs table', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    check('total is still 1 (unaffected by the empty runs table)', body['total'] == 1, body)
    check('transactions_analysed falls back to distinct invoice_document_id from anomalies (1)',
          body['transactions_analysed'] == 1, body)
    check('last_analysed stays None/"Never" — no fallback source for that field', body['last_analysed'] is None, body)


def run_case_stats_degrades_gracefully_when_anomaly_detection_runs_query_fails():
    print('Case: anomaly_detection_runs query itself raises (e.g. missing/broken table) -> still 200, falls back, never a 500')
    db = stats_db()
    db['anomaly_detection_runs_raises'] = True
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.get('/anomalies/stats', headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK even when the anomaly_detection_runs query raises', resp.status_code == 200, resp.get_json())
    body = resp.get_json()
    check('total is still correct (the failure is isolated to the runs query)', body['total'] == 1, body)
    check('transactions_analysed falls back to distinct invoice_document_id from anomalies (1)',
          body['transactions_analysed'] == 1, body)


def run_case_stats_excludes_orphan_anomaly_records():
    print('Case: an anomaly whose invoice_document_id has no matching document row is excluded from every count')
    db = stats_db()
    db['anomalies'].append({
        'anomaly_id': 99, 'invoice_document_id': 99999, 'status': 'pending',
        'severity': 'high', 'anomaly_type': 'duplicate', 'reviewed_by': None, 'reviewed_at': None,
    })
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    body = client.get('/anomalies/stats', headers={'Authorization': f'Bearer {auditor_token}'}).get_json()
    check('total still 1 — the orphan anomaly (no matching document) is excluded', body['total'] == 1, body)
    check('by_status.pending still 0 — the orphan\'s pending status is not counted', body['by_status']['pending'] == 0, body)
    check('by_type.duplicate still 0 — the orphan\'s type is not counted', body['by_type']['duplicate'] == 0, body)


# ── Section E: final decision -> anomaly status mapping ──────────────

def decision_db():
    return {
        'documents': {1: {'document_id': 1, 'status': 'under_review'}},
        'review_records': [],
        'anomalies': [
            {'anomaly_id': 100, 'invoice_document_id': 1, 'status': 'pending',
             'severity': 'medium', 'anomaly_type': 'round', 'reviewed_by': None, 'reviewed_at': None},
            {'anomaly_id': 101, 'invoice_document_id': 1, 'status': 'reviewed',
             'severity': 'low', 'anomaly_type': 'weekend', 'reviewed_by': 5, 'reviewed_at': 'prior'},
            {'anomaly_id': 102, 'invoice_document_id': 1, 'status': 'dismissed',
             'severity': 'high', 'anomaly_type': 'duplicate', 'reviewed_by': 5, 'reviewed_at': 'prior'},
        ],
        'next_review_id': 0,
    }


def run_case_approve_resolves_pending_anomalies_without_touching_reviewed_or_dismissed():
    print('Case: Approve marks the still-pending anomaly reviewed, and never overwrites an already reviewed/dismissed one')
    db = decision_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/approve/1', json={}, headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    check('document approved', db['documents'][1]['status'] == 'approved')

    by_id = {a['anomaly_id']: a for a in db['anomalies']}
    check('previously-pending anomaly is now reviewed', by_id[100]['status'] == 'reviewed', by_id[100])
    check('previously-pending anomaly records the approving auditor', by_id[100]['reviewed_by'] == 9, by_id[100])
    check('already-reviewed anomaly is untouched (reviewer/timestamp not overwritten)',
          by_id[101]['status'] == 'reviewed' and by_id[101]['reviewed_by'] == 5, by_id[101])
    check('already-dismissed anomaly is untouched', by_id[102]['status'] == 'dismissed', by_id[102])
    check('every anomaly record still exists (no deletion)', len(db['anomalies']) == 3, db['anomalies'])


def run_case_send_back_leaves_every_anomaly_status_untouched():
    print('Case: Send Back keeps every anomaly exactly as it was — the issue is not resolved')
    db = decision_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/return/1', json={'remarks': 'Please re-verify the amount.'},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    check('document status returned', db['documents'][1]['status'] == 'returned')

    by_id = {a['anomaly_id']: a for a in db['anomalies']}
    check('pending anomaly stays pending', by_id[100]['status'] == 'pending', by_id[100])
    check('reviewed anomaly stays reviewed', by_id[101]['status'] == 'reviewed', by_id[101])
    check('dismissed anomaly stays dismissed', by_id[102]['status'] == 'dismissed', by_id[102])


def run_case_need_review_keeps_document_and_anomalies_untouched_but_logs_the_flag():
    print('Case: Need Review records the flag in review_records without changing document status or any anomaly status')
    db = decision_db()
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/need-review/1', json={},
                        headers={'Authorization': f'Bearer {auditor_token}'})
    check('200 OK', resp.status_code == 200, resp.get_json())
    check('document status unchanged (stays actionable, not a final disposition)',
          db['documents'][1]['status'] == 'under_review', db['documents'][1])

    by_id = {a['anomaly_id']: a for a in db['anomalies']}
    check('pending anomaly stays pending', by_id[100]['status'] == 'pending', by_id[100])
    check('reviewed anomaly stays reviewed', by_id[101]['status'] == 'reviewed', by_id[101])
    check('dismissed anomaly stays dismissed', by_id[102]['status'] == 'dismissed', by_id[102])

    check('one review_records row logged with action=need_review',
          len(db['review_records']) == 1 and db['review_records'][0]['action'] == 'need_review',
          db['review_records'])


def run_case_finance_cannot_call_need_review():
    print('Case: a finance_executive calling need-review gets 403 (auditor-only, same as approve/return)')
    db = decision_db()
    app, _, finance_token = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/need-review/1', json={}, headers={'Authorization': f'Bearer {finance_token}'})
    check('403 Forbidden', resp.status_code == 403, resp.get_json())


def run_case_need_review_rejected_when_document_not_under_review():
    print('Case: need-review on a document that is not under_review/resubmitted (e.g. already approved) is rejected with 400')
    db = decision_db()
    db['documents'][1]['status'] = 'approved'
    app, auditor_token, _ = make_app(db)
    client = app.test_client()

    resp = client.post('/reviews/need-review/1', json={}, headers={'Authorization': f'Bearer {auditor_token}'})
    check('400 Bad Request', resp.status_code == 400, resp.get_json())


if __name__ == '__main__':
    run_case_stats_matches_required_numbers_for_the_reviewed_round_amount_case()
    run_case_stats_identical_regardless_of_active_list_filter_query_params()
    run_case_stats_pending_filter_empty_state_data_is_available()
    run_case_stats_returns_200_when_anomaly_detection_runs_is_empty()
    run_case_stats_degrades_gracefully_when_anomaly_detection_runs_query_fails()
    run_case_stats_excludes_orphan_anomaly_records()
    run_case_approve_resolves_pending_anomalies_without_touching_reviewed_or_dismissed()
    run_case_send_back_leaves_every_anomaly_status_untouched()
    run_case_need_review_keeps_document_and_anomalies_untouched_but_logs_the_flag()
    run_case_finance_cannot_call_need_review()
    run_case_need_review_rejected_when_document_not_under_review()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
