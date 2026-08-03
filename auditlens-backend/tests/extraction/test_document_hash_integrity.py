"""Regression test for the Audit Evidence Passport's document-integrity
feature: app.py::_ensure_document_hash_columns() (the sha256_baseline
migration) and routes/documents.py::_compute_document_integrity()
(recompute-and-compare against that baseline).

Uses the real local Postgres dev DB (same permitted convention as
tests/extraction/test_document_review_steps_migration.py) — a mock
would never exercise the actual ALTER TABLE / hash comparison. Unlike
that reference test, this one does NOT drop sha256_baseline from
documents/purchase_orders/goods_receipts to prove the migration works —
those are core, shared business tables that may already carry real
baselines from real uploads in this dev DB, and a DROP COLUMN would
destroy them for the whole run. Idempotency is proven instead by simply
calling the migration function twice and checking it errors neither
time and the columns are present both times.

Usage:
    python tests/extraction/test_document_hash_integrity.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
import io
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2.extras
from db import get_db_connection

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


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


def _columns_exist():
    conn = get_db_connection()
    cur = conn.cursor()
    ok = True
    for table in ('documents', 'purchase_orders', 'goods_receipts'):
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = 'sha256_baseline'",
            (table,)
        )
        if cur.fetchone() is None:
            ok = False
    conn.close()
    return ok


@_skip_if_db_unavailable
def run_case_migration_creates_columns_and_is_idempotent():
    print('Case: _ensure_document_hash_columns() adds sha256_baseline to all 3 tables, '
          'and running it again is a safe no-op (no DROP — these are shared business tables)')
    import app  # noqa: F401

    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        app._ensure_document_hash_columns()
    output = log.getvalue()

    check('columns present on documents/purchase_orders/goods_receipts', _columns_exist())
    check('startup log confirms success', 'Document hash baseline columns ready' in output, output)
    check('no ERROR/WARNING logged', 'ERROR' not in output and 'WARNING' not in output, output)

    log2 = io.StringIO()
    with contextlib.redirect_stdout(log2):
        app._ensure_document_hash_columns()
    output2 = log2.getvalue()

    check('re-running raises no error', 'WARNING' not in output2, output2)
    check('columns still present after re-running', _columns_exist())


def _require_fixture_user():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM users LIMIT 1')
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _insert_test_document(user_id, file_bytes, sha256_baseline):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO documents
           (uploaded_by, file_name, file_path, file_type, input_method, status, file_bytes, file_mime, sha256_baseline)
           VALUES (%s, 'integrity_test.pdf', 'integrity_test.pdf', 'pdf', 'upload', 'ocr_processing', %s, 'application/pdf', %s)
           RETURNING document_id''',
        (user_id, psycopg2.Binary(file_bytes), sha256_baseline)
    )
    doc_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return doc_id


def _delete_test_document(doc_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM documents WHERE document_id = %s', (doc_id,))
    conn.commit()
    conn.close()


@_skip_if_db_unavailable
def run_case_integrity_verified_when_hash_matches_baseline():
    print('Case: recomputed hash matches the stored baseline -> verified (never for an unrecorded baseline)')
    from routes.documents import _compute_document_integrity
    from helpers.gemini_cache import compute_file_hash

    user_id = _require_fixture_user()
    if not user_id:
        print('  SKIP (no users in this DB to satisfy the documents.uploaded_by FK)')
        return

    file_bytes = b'AUDITLENS INTEGRITY TEST FIXTURE - VERIFIED CASE'
    doc_id = _insert_test_document(user_id, file_bytes, compute_file_hash(file_bytes))
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        comparison = {'invoice': {'document_id': doc_id}, 'po': None, 'gr': None}
        result = _compute_document_integrity(cur, comparison)
        conn.close()

        check('overall_status is verified', result['overall_status'] == 'verified', result)
        check('invoice status is verified', result['documents']['invoice']['status'] == 'verified', result)
        check('po marked not_applicable (not uploaded)', result['documents']['po']['status'] == 'not_applicable', result)
    finally:
        _delete_test_document(doc_id)


@_skip_if_db_unavailable
def run_case_integrity_warning_when_bytes_tampered():
    print('Case: stored bytes no longer match the baseline (tampering/corruption) -> warning, never verified')
    from routes.documents import _compute_document_integrity
    from helpers.gemini_cache import compute_file_hash

    user_id = _require_fixture_user()
    if not user_id:
        print('  SKIP (no users in this DB to satisfy the documents.uploaded_by FK)')
        return

    baseline_hash = compute_file_hash(b'AUDITLENS INTEGRITY TEST FIXTURE - ORIGINAL')
    tampered_bytes = b'AUDITLENS INTEGRITY TEST FIXTURE - TAMPERED'
    doc_id = _insert_test_document(user_id, tampered_bytes, baseline_hash)
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        comparison = {'invoice': {'document_id': doc_id}, 'po': None, 'gr': None}
        result = _compute_document_integrity(cur, comparison)
        conn.close()

        check('overall_status is warning', result['overall_status'] == 'warning', result)
        check('invoice status is warning (a mismatch is never reported as verified)',
              result['documents']['invoice']['status'] == 'warning', result)
    finally:
        _delete_test_document(doc_id)


@_skip_if_db_unavailable
def run_case_integrity_not_recorded_when_no_baseline():
    print('Case: document has no sha256_baseline (uploaded before this feature existed) -> not_recorded, never a false verified')
    from routes.documents import _compute_document_integrity

    user_id = _require_fixture_user()
    if not user_id:
        print('  SKIP (no users in this DB to satisfy the documents.uploaded_by FK)')
        return

    doc_id = _insert_test_document(user_id, b'no baseline recorded for this one', None)
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        comparison = {'invoice': {'document_id': doc_id}, 'po': None, 'gr': None}
        result = _compute_document_integrity(cur, comparison)
        conn.close()

        check('overall_status is not_recorded', result['overall_status'] == 'not_recorded', result)
        check('invoice status is not_recorded (never a false verified without a baseline)',
              result['documents']['invoice']['status'] == 'not_recorded', result)
    finally:
        _delete_test_document(doc_id)


if __name__ == '__main__':
    run_case_migration_creates_columns_and_is_idempotent()
    run_case_integrity_verified_when_hash_matches_baseline()
    run_case_integrity_warning_when_bytes_tampered()
    run_case_integrity_not_recorded_when_no_baseline()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
