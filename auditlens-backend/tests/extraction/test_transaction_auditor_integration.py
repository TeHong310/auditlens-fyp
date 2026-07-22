"""Regression tests for Enterprise V3 Phase 6 (Transaction-Centric
Auditor Workflow Integration):
  - helpers/transaction_packages.py: get_transaction_context_for_
    document(), list_all_packages_with_documents(), list_standalone_
    invoices(), get_transaction_authenticity_summary().
  - routes/auditor.py: GET /auditor/transactions, GET /auditor/
    transactions/<id>, transaction_context enrichment on GET /auditor/
    record/<id>/comparison and GET /auditor/exceptions.
  - routes/ai_assistant.py: _build_case_context()'s transaction_context
    field.

Integration tests use the real local Postgres dev DB and a real Flask
test client (same permitted "database test data" convention as
Phases 2-5's own test suites — only live Claude/Gemini calls are
prohibited). Every test creates its own rows and deletes them in
__exit__, leaving the DB exactly as found.

Usage:
    python tests/extraction/test_transaction_auditor_integration.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2.extras
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from db import get_db_connection
from helpers.transaction_packages import (
    create_package, link_document_to_package, get_transaction_context_for_document,
    get_transaction_authenticity_summary,
)
import routes.auditor as auditor_module
from routes.auditor import build_comparison
from routes.ai_assistant import _build_case_context
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
        self.uid_finance = None
        self.uid_auditor = None

    def __enter__(self):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE email = 'phase6_finance@x.com'")
        row = cur.fetchone()
        if row:
            self.uid_finance = row[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, role, full_name) "
                "VALUES ('phase6_finance@x.com', 'x', 'finance_executive', 'Phase6 Finance') RETURNING user_id"
            )
            self.uid_finance = cur.fetchone()[0]
        cur.execute("SELECT user_id FROM users WHERE email = 'phase6_auditor@x.com'")
        row = cur.fetchone()
        if row:
            self.uid_auditor = row[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, role, full_name) "
                "VALUES ('phase6_auditor@x.com', 'x', 'auditor', 'Phase6 Auditor') RETURNING user_id"
            )
            self.uid_auditor = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return self

    def invoice(self, invoice_number, vendor_name, amount, quantity, po_reference, status='under_review'):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO documents (uploaded_by, file_name, file_path, file_type, input_method, status) "
            "VALUES (%s, %s, %s, 'pdf', 'upload', %s) RETURNING document_id",
            (self.uid_finance, f'{invoice_number}.pdf', f'/tmp/{invoice_number}.pdf', status))
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
            (invoice_doc_id, self.uid_finance, f'{po_number}.pdf', f'/tmp/{po_number}.pdf', po_number, vendor_name, quantity, total_amount))
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
            (invoice_doc_id, self.uid_finance, f'{gr_number}.pdf', f'/tmp/{gr_number}.pdf', gr_number, vendor_name, quantity, po_reference))
        gr_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        self.gr_ids.append(gr_id)
        return gr_id

    def package(self, package_name):
        pkg = create_package(package_name, self.uid_finance)
        self.package_ids.append(pkg['id'])
        return pkg

    def __exit__(self, *exc):
        conn = get_db_connection()
        cur = conn.cursor()
        if self.doc_ids:
            cur.execute('DELETE FROM authenticity_checks WHERE document_id = ANY(%s)', (self.doc_ids,))
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


def _comparison(document_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        return build_comparison(cur, document_id)
    finally:
        conn.close()


def _case_context(document_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        return _build_case_context(cur, document_id)
    finally:
        conn.close()


_test_app = Flask(__name__)
_test_app.config['JWT_SECRET_KEY'] = 'phase6-test-secret-key-not-for-prod'
JWTManager(_test_app)
_test_app.register_blueprint(auditor_module.auditor_bp, url_prefix='/auditor')
_test_client = _test_app.test_client()


def _auditor_headers(fx):
    with _test_app.app_context():
        token = create_access_token(identity=str(fx.uid_auditor))
    return {'Authorization': f'Bearer {token}'}


# ============================================================
# Test Case 1: normal workflow (1 Invoice/1 PO/1 GR) — unchanged
# ============================================================

@_skip_if_db_unavailable
def run_case1_normal_workflow_unchanged():
    print('Test Case 1: normal (1 Invoice/1 PO/1 GR) -> existing workflow unchanged')
    with _Fixture() as fx:
        inv = fx.invoice('INV-C1', 'Acme Corp', 100.0, 10, 'PO-C1')
        po = fx.po(inv, 'PO-C1', 'Acme Corp', 10, 100.0)
        gr = fx.gr(inv, 'GR-C1', 'Acme Corp', 10, 'PO-C1')
        pkg = fx.package('Simple Transaction')
        for doc_id, role in [(inv, 'invoice'), (po, 'purchase_order'), (gr, 'goods_receipt')]:
            link, err = link_document_to_package(pkg['id'], doc_id, role)
            check(f'{role} linked', link is not None, err)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            # build_comparison() itself is untouched (no transaction_
            # context key) — the enrichment happens in the ROUTE
            # handler, so this must go through the real HTTP endpoint.
            comparison = _comparison(inv)
            check('comparison still has legacy-shaped keys (invoice/po/gr/match_result)',
                  'invoice' in comparison and 'match_result' in comparison, comparison)

            resp = _test_client.get(f'/auditor/record/{inv}/comparison', headers=_auditor_headers(fx))
            api_result = resp.get_json()
            check('API response includes transaction_context', api_result.get('transaction_context') is not None, api_result)
            check('transaction_context documents_count is 3', api_result['transaction_context']['documents_count'] == 3, api_result)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


# ============================================================
# Test Case 2 (star): PO7710 enterprise case
# ============================================================

@_skip_if_db_unavailable
def run_case2_enterprise_one_transaction_pass_allocated_ai_understands():
    print('Test Case 2 (star): PO7710 (2 invoices, 2 GR) -> ONE transaction, PASS, allocated correctly, AI understands partial invoices')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-C2-A', 'Acme Corp', 3855.0, 15000, 'PO7710-C2')
        inv_b = fx.invoice('INV-C2-B', 'Acme Corp', 3855.0, 15000, 'PO7710-C2')
        po = fx.po(inv_a, 'PO7710-C2', 'Acme Corp', 30000, 7710.0)
        gr_a = fx.gr(inv_a, 'GR-C2-A', 'Acme Corp', 15000, 'PO7710-C2')
        gr_b = fx.gr(inv_b, 'GR-C2-B', 'Acme Corp', 15000, 'PO7710-C2')
        pkg = fx.package('PO7710 Supplier ABC')
        for doc_id, role in [(inv_a, 'invoice'), (inv_b, 'invoice'), (po, 'purchase_order'),
                              (gr_a, 'goods_receipt'), (gr_b, 'goods_receipt')]:
            link_document_to_package(pkg['id'], doc_id, role)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            headers = _auditor_headers(fx)

            # Auditor sees ONE transaction in the dashboard queue.
            resp = _test_client.get('/auditor/transactions', headers=headers)
            rows = resp.get_json()
            txn_rows = [r for r in rows if r.get('transaction_package_id') == pkg['id']]
            check('exactly ONE transaction row for this package', len(txn_rows) == 1, txn_rows)
            check('matching status is PASS', txn_rows[0]['matching_status'] == 'PASS', txn_rows[0])
            check('document_count is 5', txn_rows[0]['document_count'] == 5, txn_rows[0])

            # Comparison: allocated correctly.
            detail_resp = _test_client.get(f"/auditor/transactions/{pkg['id']}", headers=headers)
            detail = detail_resp.get_json()
            ms = detail['matching_summary']
            check('final_status is MATCHED', ms['final_status'] == 'MATCHED', ms)
            check('PO fully invoiced/received (30000/30000)',
                  ms['po_fulfilment'][0]['invoiced_quantity_cumulative'] == 30000.0 and
                  ms['po_fulfilment'][0]['received_quantity_cumulative'] == 30000.0, ms)

            # AI understands partial invoices — transaction_context
            # present with an allocation_summary, audit_status PASS
            # (not falsely REVIEW REQUIRED).
            context = _case_context(inv_a)
            check('AI context has transaction_context', context.get('transaction_context') is not None, context)
            check('AI transaction_context has 1 related invoice sibling worth of data (2 total in package)',
                  len(context['transaction_context']['related_invoices']) == 2, context['transaction_context'])
            check('AI transaction_context has allocation_summary', context['transaction_context']['allocation_summary'] is not None, context['transaction_context'])
            check('AI audit_status is PASS (no false mismatch)', context['audit_status'] == 'PASS', context)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


# ============================================================
# Test Case 3: returned correction workflow retains transaction context
# ============================================================

@_skip_if_db_unavailable
def run_case3_returned_correction_keeps_transaction_context():
    print('Test Case 3: returned/correction workflow -> transaction context remains after finance correction')
    with _Fixture() as fx:
        inv = fx.invoice('INV-C3', 'Acme Corp', 100.0, 10, 'PO-C3', status='returned')
        po = fx.po(inv, 'PO-C3', 'Acme Corp', 10, 100.0)
        pkg = fx.package('Returned Transaction')
        link_document_to_package(pkg['id'], inv, 'invoice')
        link_document_to_package(pkg['id'], po, 'purchase_order')

        # Simulate Finance resubmitting after correction.
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE documents SET status = 'resubmitted' WHERE document_id = %s", (inv,))
        conn.commit()
        conn.close()

        context = get_transaction_context_for_document(inv, 'invoice')
        check('transaction context still resolvable after correction', context is not None, context)
        check('package name unchanged', context['package_name'] == 'Returned Transaction', context)


# ============================================================
# Test Case 4: legacy invoice without a package
# ============================================================

@_skip_if_db_unavailable
def run_case4_legacy_invoice_no_package_still_works():
    print('Test Case 4: old existing invoice without a package -> legacy view still works, no errors')
    with _Fixture() as fx:
        inv = fx.invoice('INV-C4', 'Acme Corp', 100.0, 10, 'PO-C4')
        fx.po(inv, 'PO-C4', 'Acme Corp', 10, 100.0)
        # Deliberately never create/link a package.

        context = get_transaction_context_for_document(inv, 'invoice')
        check('transaction context is None (legacy fallback)', context is None, context)

        comparison = _comparison(inv)
        check('comparison still works', comparison is not None, comparison)

        headers = _auditor_headers(fx)
        api_resp = _test_client.get(f'/auditor/record/{inv}/comparison', headers=headers)
        api_result = api_resp.get_json()
        check('API response transaction_context is None for a legacy invoice', api_result.get('transaction_context') is None, api_result)

        resp = _test_client.get('/auditor/transactions', headers=headers)
        check('dashboard queue call succeeds (no crash)', resp.status_code == 200, resp.status_code)
        rows = resp.get_json()
        standalone_row = next((r for r in rows if r.get('primary_document_id') == inv), None)
        check('legacy invoice appears as a standalone row', standalone_row is not None, rows)
        if standalone_row:
            check('kind is standalone_invoice', standalone_row['kind'] == 'standalone_invoice', standalone_row)

        ai_context = _case_context(inv)
        check('AI context transaction_context is None for legacy invoice', ai_context.get('transaction_context') is None, ai_context)


# ============================================================
# Additional Requirement: transaction-level authenticity summary
# ============================================================

@_skip_if_db_unavailable
def run_case_authenticity_summary_no_false_failures():
    print('Additional Requirement: transaction authenticity summary -> no false failures from supporting documents')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-AUTH-A', 'Acme Corp', 3855.0, 15000, 'PO-AUTH')
        inv_b = fx.invoice('INV-AUTH-B', 'Acme Corp', 3855.0, 15000, 'PO-AUTH')
        po = fx.po(inv_a, 'PO-AUTH', 'Acme Corp', 30000, 7710.0)
        gr_a = fx.gr(inv_a, 'GR-AUTH-A', 'Acme Corp', 15000, 'PO-AUTH')
        gr_b = fx.gr(inv_b, 'GR-AUTH-B', 'Acme Corp', 15000, 'PO-AUTH')
        pkg = fx.package('PO-AUTH Transaction')
        for doc_id, role in [(inv_a, 'invoice'), (inv_b, 'invoice'), (po, 'purchase_order'),
                              (gr_a, 'goods_receipt'), (gr_b, 'goods_receipt')]:
            link_document_to_package(pkg['id'], doc_id, role)

        # Both invoices pass their authenticity check; the GR (a
        # supporting document) has NOT been checked at all yet — this
        # must NOT force the transaction into REVIEW REQUIRED.
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO authenticity_checks (document_id, document_type, authenticity_status, risk_level) VALUES (%s,'invoice','passed','low')", (inv_a,))
        cur.execute("INSERT INTO authenticity_checks (document_id, document_type, authenticity_status, risk_level) VALUES (%s,'invoice','passed','low')", (inv_b,))
        cur.execute("INSERT INTO authenticity_checks (document_id, document_type, authenticity_status, risk_level) VALUES (%s,'po','passed','low')", (inv_a,))
        conn.commit()
        conn.close()

        summary = get_transaction_authenticity_summary(pkg['id'])
        check('documents_total is 5', summary['documents_total'] == 5, summary)
        check('documents_checked is 3 (2 invoices + 1 PO; GRs not checked yet)', summary['documents_checked'] == 3, summary)
        check('overall_status is PASS (unchecked GRs do not force a failure)', summary['overall_status'] == 'PASS', summary)
        check('completed_by_role.invoices is 2/2', summary['completed_by_role']['invoices'] == {'checked': 2, 'total': 2}, summary)
        check('completed_by_role.goods_receipts is 0/2 (not yet checked, not a failure)',
              summary['completed_by_role']['goods_receipts'] == {'checked': 0, 'total': 2}, summary)
        check('per-document detail included (documents key)', 'documents' in summary, summary)
        check('invoice A detail shows passed', summary['documents']['invoices'][0]['authenticity_status'] == 'passed', summary)

        # Now a genuine warning on one invoice DOES flip overall_status.
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE authenticity_checks SET authenticity_status = 'warning' WHERE document_id = %s", (inv_b,))
        conn.commit()
        conn.close()
        summary2 = get_transaction_authenticity_summary(pkg['id'])
        check('overall_status becomes REVIEW REQUIRED with a genuine warning', summary2['overall_status'] == 'REVIEW REQUIRED', summary2)


if __name__ == '__main__':
    run_case1_normal_workflow_unchanged()
    run_case2_enterprise_one_transaction_pass_allocated_ai_understands()
    run_case3_returned_correction_keeps_transaction_context()
    run_case4_legacy_invoice_no_package_still_works()
    run_case_authenticity_summary_no_false_failures()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
