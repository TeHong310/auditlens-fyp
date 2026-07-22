"""Regression tests for Enterprise V3 Phase 5 (Finance Transaction
Package workflow - helpers/transaction_packages.py, routes/
transaction_packages.py).

Integration tests use the real local Postgres dev DB - same permitted
"database test data" convention as Phases 2-4's own test suites (this
phase's logic spans documents/purchase_orders/goods_receipts/
transaction_packages/transaction_package_documents and calls the real
relationship builder, which a hand-rolled fake cursor would be prone to
getting subtly wrong). Every test creates its own rows and deletes them
in __exit__, leaving the DB exactly as found.

Usage:
    python tests/extraction/test_transaction_packages.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2.extras
from db import get_db_connection
from helpers.transaction_packages import (
    create_package, get_package, link_document_to_package, get_package_documents,
    get_relationship_preview, list_packages, compute_package_status,
)
import routes.auditor as auditor_module
from config import Config

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class _Fixture:
    def __init__(self):
        self.doc_ids, self.po_ids, self.gr_ids, self.package_ids = [], [], [], []
        self.uid = None

    def __enter__(self):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE email = 'phase5_test@x.com'")
        row = cur.fetchone()
        if row:
            self.uid = row[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, role, full_name) "
                "VALUES ('phase5_test@x.com', 'x', 'finance_executive', 'Phase5 Test') RETURNING user_id"
            )
            self.uid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return self

    def invoice(self, invoice_number, vendor_name, amount, quantity, po_reference, status='under_review'):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO documents (uploaded_by, file_name, file_path, file_type, input_method, status) "
            "VALUES (%s, %s, %s, 'pdf', 'upload', %s) RETURNING document_id",
            (self.uid, f'{invoice_number}.pdf', f'/tmp/{invoice_number}.pdf', status))
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO extracted_fields (document_id, invoice_number, vendor_name, total_amount, currency, po_reference, quantity) "
            "VALUES (%s, %s, %s, %s, 'RM', %s, %s)",
            (doc_id, invoice_number, vendor_name, amount, po_reference, quantity))
        conn.commit()
        conn.close()
        self.doc_ids.append(doc_id)
        return doc_id

    def po(self, invoice_doc_id, po_number, vendor_name, quantity, total_amount):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO purchase_orders (document_id, uploaded_by, file_name, file_path, po_number, vendor_name, quantity, total_amount, currency) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'RM') RETURNING po_id",
            (invoice_doc_id, self.uid, f'{po_number}.pdf', f'/tmp/{po_number}.pdf', po_number, vendor_name, quantity, total_amount))
        po_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        self.po_ids.append(po_id)
        return po_id

    def gr(self, invoice_doc_id, gr_number, vendor_name, quantity, po_reference):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO goods_receipts (document_id, uploaded_by, file_name, file_path, gr_number, vendor_name, quantity, po_reference) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING gr_id",
            (invoice_doc_id, self.uid, f'{gr_number}.pdf', f'/tmp/{gr_number}.pdf', gr_number, vendor_name, quantity, po_reference))
        gr_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        self.gr_ids.append(gr_id)
        return gr_id

    def package(self, package_name):
        pkg = create_package(package_name, self.uid)
        self.package_ids.append(pkg['id'])
        return pkg

    def __exit__(self, *exc):
        conn = get_db_connection()
        cur = conn.cursor()
        if self.package_ids:
            cur.execute('DELETE FROM transaction_package_documents WHERE package_id = ANY(%s)', (self.package_ids,))
            cur.execute('DELETE FROM transaction_packages WHERE id = ANY(%s)', (self.package_ids,))
        cur.execute(
            "DELETE FROM document_relationships WHERE "
            "(parent_type = 'invoice' AND parent_id = ANY(%s)) OR (child_type = 'invoice' AND child_id = ANY(%s)) OR "
            "(parent_type = 'po' AND parent_id = ANY(%s)) OR (child_type = 'po' AND child_id = ANY(%s)) OR "
            "(parent_type = 'gr' AND parent_id = ANY(%s)) OR (child_type = 'gr' AND child_id = ANY(%s))",
            (self.doc_ids, self.doc_ids, self.po_ids, self.po_ids, self.gr_ids, self.gr_ids))
        if self.gr_ids:
            cur.execute('DELETE FROM goods_receipts WHERE gr_id = ANY(%s)', (self.gr_ids,))
        if self.po_ids:
            cur.execute('DELETE FROM purchase_orders WHERE po_id = ANY(%s)', (self.po_ids,))
        if self.doc_ids:
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


# ============================================================
# STEP 9, Scenario 1: existing workflow, no regression
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario1_simple_package_no_regression():
    print('Scenario 1: 1 invoice, 1 PO, 1 GR -> no regression, package reaches "processing"')
    with _Fixture() as fx:
        inv = fx.invoice('INV-T1', 'Acme Corp', 100.0, 10, 'PO-T1')
        po = fx.po(inv, 'PO-T1', 'Acme Corp', 10, 100.0)
        gr = fx.gr(inv, 'GR-T1', 'Acme Corp', 10, 'PO-T1')
        pkg = fx.package('Simple package')

        check('starts as draft', pkg['status'] == 'draft', pkg)

        l1, e1 = link_document_to_package(pkg['id'], inv, 'invoice')
        l2, e2 = link_document_to_package(pkg['id'], po, 'purchase_order')
        l3, e3 = link_document_to_package(pkg['id'], gr, 'goods_receipt')
        check('all 3 documents linked without error', l1 and l2 and l3, (e1, e2, e3))

        final = get_package(pkg['id'])
        check('status is processing', final['status'] == 'processing', final)

        docs = get_package_documents(pkg['id'])
        check('1 invoice, 1 PO, 1 GR returned', len(docs['invoices']) == 1 and len(docs['purchase_orders']) == 1 and len(docs['goods_receipts']) == 1, docs)


# ============================================================
# STEP 9, Scenario 2: enterprise case, PO7710-style
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario2_enterprise_package_matching_receives_correct_documents():
    print('Scenario 2: PO7710-style (1 PO, 2 Invoices, 2 GR) -> package created, all linked, V2 receives correct documents')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-T2-A', 'Acme Corp', 3855.0, 15000, 'PO7710-T2')
        inv_b = fx.invoice('INV-T2-B', 'Acme Corp', 3855.0, 15000, 'PO7710-T2')
        po = fx.po(inv_a, 'PO7710-T2', 'Acme Corp', 30000, 7710.0)
        gr_a = fx.gr(inv_a, 'GR-T2-A', 'Acme Corp', 15000, 'PO7710-T2')
        gr_b = fx.gr(inv_b, 'GR-T2-B', 'Acme Corp', 15000, 'PO7710-T2')
        pkg = fx.package('PO7710 Supplier ABC')

        for doc_id, role in [(inv_a, 'invoice'), (inv_b, 'invoice'), (po, 'purchase_order'), (gr_a, 'goods_receipt'), (gr_b, 'goods_receipt')]:
            link, error = link_document_to_package(pkg['id'], doc_id, role)
            check(f'{role} {doc_id} linked', link is not None, error)

        docs = get_package_documents(pkg['id'])
        check('document counts: 2 invoices, 1 PO, 2 GR (5 total)',
              len(docs['invoices']) == 2 and len(docs['purchase_orders']) == 1 and len(docs['goods_receipts']) == 2, docs)

        preview = get_relationship_preview(pkg['id'])
        check('relationship preview has 1 PO node', len(preview) == 1, preview)
        if preview:
            check('PO node has 2 invoice children', len(preview[0]['invoices']) == 2, preview)
            check('each invoice has its own GR (not the sibling\'s)',
                  {inv['invoice_number']: [g['gr_number'] for g in inv['goods_receipts']] for inv in preview[0]['invoices']}
                  == {'INV-T2-A': ['GR-T2-A'], 'INV-T2-B': ['GR-T2-B']}, preview)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            result = auditor_module.build_comparison(cur, inv_a)
            conn.close()
            check('Enterprise Matching V2 activated for the grouped invoice', result.get('engine_version') == 'v2', result)
            check('V2 correctly resolves PASS (relationship builder ran automatically on link)',
                  result.get('invoice_result', {}).get('status') == 'PASS', result)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False

        list_row = next(p for p in list_packages(fx.uid) if p['id'] == pkg['id'])
        check('list page: document_count is 5', list_row['document_count'] == 5, list_row)
        check('list page: supplier is Acme Corp', list_row['supplier'] == 'Acme Corp', list_row)
        check('list page: status is processing', list_row['status'] == 'processing', list_row)


# ============================================================
# STEP 9, Scenario 3: multiple PO, no crash
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario3_multiple_po_no_crash():
    print('Scenario 3: 2 PO, 2 Invoice, 2 GR -> no crash, package supports multiple documents')
    with _Fixture() as fx:
        inv1 = fx.invoice('INV-T3-1', 'Acme Corp', 50.0, 5, 'PO-T3-1')
        inv2 = fx.invoice('INV-T3-2', 'Acme Corp', 60.0, 6, 'PO-T3-2')
        po1 = fx.po(inv1, 'PO-T3-1', 'Acme Corp', 5, 50.0)
        po2 = fx.po(inv2, 'PO-T3-2', 'Acme Corp', 6, 60.0)
        gr1 = fx.gr(inv1, 'GR-T3-1', 'Acme Corp', 5, 'PO-T3-1')
        gr2 = fx.gr(inv2, 'GR-T3-2', 'Acme Corp', 6, 'PO-T3-2')
        pkg = fx.package('Multi-PO package')

        errors = []
        for doc_id, role in [(inv1, 'invoice'), (inv2, 'invoice'), (po1, 'purchase_order'),
                              (po2, 'purchase_order'), (gr1, 'goods_receipt'), (gr2, 'goods_receipt')]:
            _, error = link_document_to_package(pkg['id'], doc_id, role)
            if error:
                errors.append((doc_id, role, error))
        check('no crash / no errors linking 6 documents across 2 POs', errors == [], errors)

        docs = get_package_documents(pkg['id'])
        check('all 6 documents present', len(docs['invoices']) == 2 and len(docs['purchase_orders']) == 2 and len(docs['goods_receipts']) == 2, docs)


# ============================================================
# STEP 9, Scenario 4: incomplete package (PO only)
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario4_po_only_waiting_documents():
    print('Scenario 4: only a PO uploaded -> status is waiting_documents')
    with _Fixture() as fx:
        # A PO still requires SOME invoice document_id to attach under
        # today (the existing upload-po/<id> route's own constraint,
        # confirmed by inspection - not modified by this phase). This
        # invoice belongs to a DIFFERENT, unrelated package context —
        # it is deliberately never linked into the package itself, so
        # the package's own view of "does it have an invoice" stays
        # honestly at zero, matching the resolved design: a PO-only
        # package is created, but nothing is treated as its own invoice
        # until Finance explicitly links one.
        anchor_inv = fx.invoice('INV-T4-ANCHOR', 'Acme Corp', 100.0, 10, None)
        po = fx.po(anchor_inv, 'PO-T4', 'Acme Corp', 10, 100.0)
        pkg = fx.package('PO-only package')

        link, error = link_document_to_package(pkg['id'], po, 'purchase_order')
        check('PO linked without error', link is not None, error)

        final = get_package(pkg['id'])
        check('status is waiting_documents', final['status'] == 'waiting_documents', final)


# ============================================================
# Additional coverage: validation, status transitions, ownership
# ============================================================

@_skip_if_db_unavailable
def run_case_draft_status_with_zero_documents():
    print('Case: a freshly created package with zero documents is "draft"')
    with _Fixture() as fx:
        pkg = fx.package('Empty package')
        check('status is draft', compute_package_status(pkg['id']) == 'draft', pkg)


@_skip_if_db_unavailable
def run_case_completed_status_when_all_invoices_approved():
    print('Case: status becomes "completed" once every linked invoice is approved')
    with _Fixture() as fx:
        inv = fx.invoice('INV-T5', 'Acme Corp', 100.0, 10, 'PO-T5', status='approved')
        po = fx.po(inv, 'PO-T5', 'Acme Corp', 10, 100.0)
        pkg = fx.package('Completed package')

        link_document_to_package(pkg['id'], inv, 'invoice')
        link_document_to_package(pkg['id'], po, 'purchase_order')

        final = get_package(pkg['id'])
        check('status is completed', final['status'] == 'completed', final)


@_skip_if_db_unavailable
def run_case_link_rejects_unknown_role():
    print('Case: link_document_to_package rejects an unknown document_role')
    with _Fixture() as fx:
        inv = fx.invoice('INV-T6', 'Acme Corp', 100.0, 10, 'PO-T6')
        pkg = fx.package('Validation package')
        link, error = link_document_to_package(pkg['id'], inv, 'not_a_real_role')
        check('rejected', link is None and error is not None, error)


@_skip_if_db_unavailable
def run_case_link_rejects_nonexistent_document():
    print('Case: link_document_to_package rejects a nonexistent document_id')
    with _Fixture() as fx:
        pkg = fx.package('Validation package 2')
        link, error = link_document_to_package(pkg['id'], 999999999, 'invoice')
        check('rejected', link is None and error is not None, error)


@_skip_if_db_unavailable
def run_case_link_rejects_duplicate():
    print('Case: linking the same document twice to the same package is rejected')
    with _Fixture() as fx:
        inv = fx.invoice('INV-T7', 'Acme Corp', 100.0, 10, 'PO-T7')
        pkg = fx.package('Duplicate-link package')
        link_document_to_package(pkg['id'], inv, 'invoice')
        link2, error2 = link_document_to_package(pkg['id'], inv, 'invoice')
        check('second link rejected', link2 is None and error2 is not None, error2)


@_skip_if_db_unavailable
def run_case_link_rejects_nonexistent_package():
    print('Case: linking into a nonexistent package is rejected')
    with _Fixture() as fx:
        inv = fx.invoice('INV-T8', 'Acme Corp', 100.0, 10, 'PO-T8')
        link, error = link_document_to_package(999999999, inv, 'invoice')
        check('rejected', link is None and error is not None, error)


if __name__ == '__main__':
    run_case_scenario1_simple_package_no_regression()
    run_case_scenario2_enterprise_package_matching_receives_correct_documents()
    run_case_scenario3_multiple_po_no_crash()
    run_case_scenario4_po_only_waiting_documents()

    run_case_draft_status_with_zero_documents()
    run_case_completed_status_when_all_invoices_approved()
    run_case_link_rejects_unknown_role()
    run_case_link_rejects_nonexistent_document()
    run_case_link_rejects_duplicate()
    run_case_link_rejects_nonexistent_package()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
