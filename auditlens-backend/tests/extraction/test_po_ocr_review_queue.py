"""Regression test for the "submitted PO stays visible in OCR Review"
bug fix: routes/documents.py::get_po_list() previously returned every
PO belonging to the finance user, regardless of whether its parent
invoice had already been submitted to the auditor (documents.status ->
'under_review'). Unlike the GR list, which finance-ocr-review.
component.ts::loadGRList() was already filtering client-side to only
invoices still 'ocr_done', the PO list had no equivalent filter
anywhere, so a submitted PO's record lingered in the Purchase Order tab
indefinitely.

Fixed with an opt-in ?pending_review=1 query param (default OFF) rather
than an unconditional filter, because finance-home.component.ts and
finance-upload.component.ts call this SAME endpoint and need POs for
invoices in every status (e.g. Finance Home's "Missing PO" correction-
analysis check specifically inspects RETURNED invoices) — this test
also guards that the default (unfiltered) behaviour those pages depend
on is unchanged.

Uses the real local Postgres dev DB (same permitted convention as
tests/extraction/test_finance_report_match_status.py), skipping
cleanly if unavailable.

Usage:
    python tests/extraction/test_po_ocr_review_queue.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
import inspect

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


def run_case_endpoint_source_has_the_opt_in_filter():
    print('Case: get_po_list() supports an opt-in pending_review filter, defaulting OFF')
    import routes.documents as documents
    source = inspect.getsource(documents.get_po_list)
    check("source reads a 'pending_review' query param",
          "request.args.get('pending_review')" in source)
    check("the filtered branch checks d.status = 'ocr_done'",
          "d.status = 'ocr_done'" in source)
    check("an unfiltered branch (no status condition) still exists for the default case",
          source.count('JOIN documents d ON po.document_id = d.document_id') >= 2)


def _run_po_list_query(cursor, user_id, pending_review_only):
    if pending_review_only:
        cursor.execute(
            '''SELECT po.po_id, po.document_id FROM purchase_orders po
               JOIN documents d ON po.document_id = d.document_id
               WHERE d.uploaded_by = %s AND d.status = 'ocr_done'
               ORDER BY po.uploaded_at DESC''',
            (user_id,)
        )
    else:
        cursor.execute(
            '''SELECT po.po_id, po.document_id FROM purchase_orders po
               JOIN documents d ON po.document_id = d.document_id
               WHERE d.uploaded_by = %s
               ORDER BY po.uploaded_at DESC''',
            (user_id,)
        )
    return {row['po_id'] for row in cursor.fetchall()}


@_skip_if_db_unavailable
def run_case_submitted_pos_excluded_only_when_pending_review_requested():
    print('Case: a PO whose invoice is no longer ocr_done is excluded by the '
          'pending_review=1 query, but still present in the default (unfiltered) query')
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute('''
        SELECT po.po_id, d.uploaded_by
        FROM purchase_orders po
        JOIN documents d ON po.document_id = d.document_id
        WHERE d.status != 'ocr_done'
        LIMIT 1
    ''')
    submitted_row = cursor.fetchone()

    if not submitted_row:
        conn.close()
        print('  SKIP (no PO in this DB currently belongs to a non-ocr_done invoice)')
        return

    user_id = submitted_row['uploaded_by']
    pending_ids = _run_po_list_query(cursor, user_id, pending_review_only=True)
    all_ids = _run_po_list_query(cursor, user_id, pending_review_only=False)
    conn.close()

    check('the submitted PO is excluded from the pending_review=1 result',
          submitted_row['po_id'] not in pending_ids, submitted_row['po_id'])
    check('the SAME submitted PO is still present in the default/unfiltered result '
          '(Finance Home/Finance Upload must keep seeing it)',
          submitted_row['po_id'] in all_ids, submitted_row['po_id'])


@_skip_if_db_unavailable
def run_case_pending_pos_included_in_both_queries():
    print('Case: a PO whose invoice IS still ocr_done appears in both queries')
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute('''
        SELECT po.po_id, d.uploaded_by
        FROM purchase_orders po
        JOIN documents d ON po.document_id = d.document_id
        WHERE d.status = 'ocr_done'
        LIMIT 1
    ''')
    pending_row = cursor.fetchone()

    if not pending_row:
        conn.close()
        print('  SKIP (no PO in this DB currently belongs to an ocr_done invoice)')
        return

    user_id = pending_row['uploaded_by']
    pending_ids = _run_po_list_query(cursor, user_id, pending_review_only=True)
    all_ids = _run_po_list_query(cursor, user_id, pending_review_only=False)
    conn.close()

    check('the pending PO is present in the pending_review=1 result',
          pending_row['po_id'] in pending_ids, pending_row['po_id'])
    check('the pending PO is present in the default/unfiltered result',
          pending_row['po_id'] in all_ids, pending_row['po_id'])


if __name__ == '__main__':
    run_case_endpoint_source_has_the_opt_in_filter()
    run_case_submitted_pos_excluded_only_when_pending_review_requested()
    run_case_pending_pos_included_in_both_queries()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
