"""Regression tests for Enterprise V3 Phase 3 (routes/auditor.py::
build_shadow_comparison/_shape_shadow_comparison/_log_shadow_comparison,
routes/document_relationships.py::get_matching_comparison).

Integration tests here use the real local Postgres dev DB - this
feature family's Safety Requirement (carried over from Phase 2) permits
"database test data"; only live Claude/Gemini calls are prohibited. Same
_Fixture pattern as tests/extraction/test_relationship_builder.py, kept
self-contained in this file per this suite's own convention. Every test
creates its own rows and deletes them in __exit__, leaving the DB
exactly as found.

Usage:
    python tests/extraction/test_shadow_comparison.py
Exits 0 if all cases pass, 1 if any fail.
"""
import io
import os
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2.extras
from db import get_db_connection
from helpers.relationship_builder import build_relationships_for_invoice
import routes.auditor as auditor_module
from routes.auditor import build_shadow_comparison, build_comparison, _log_shadow_comparison
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
        self.doc_ids, self.po_ids, self.gr_ids = [], [], []
        self.uid = None

    def __enter__(self):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE email = 'phase3_shadow_test@x.com'")
        row = cur.fetchone()
        if row:
            self.uid = row[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, role, full_name) "
                "VALUES ('phase3_shadow_test@x.com', 'x', 'auditor', 'Phase3 Shadow Test') RETURNING user_id"
            )
            self.uid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return self

    def invoice(self, invoice_number, vendor_name, amount, quantity, po_reference, invoice_date='2026-06-01'):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO documents (uploaded_by, file_name, file_path, file_type, input_method, status) "
            "VALUES (%s, %s, %s, 'pdf', 'upload', 'uploaded') RETURNING document_id",
            (self.uid, f'{invoice_number}.pdf', f'/tmp/{invoice_number}.pdf'))
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO extracted_fields (document_id, invoice_number, vendor_name, invoice_date, total_amount, currency, po_reference, quantity) "
            "VALUES (%s, %s, %s, %s, %s, 'RM', %s, %s)",
            (doc_id, invoice_number, vendor_name, invoice_date, amount, po_reference, quantity))
        conn.commit()
        conn.close()
        self.doc_ids.append(doc_id)
        return doc_id

    def po(self, invoice_doc_id, po_number, vendor_name, quantity, total_amount, po_date='2026-05-01'):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO purchase_orders (document_id, uploaded_by, file_name, file_path, po_number, vendor_name, po_date, total_amount, currency, quantity) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'RM', %s) RETURNING po_id",
            (invoice_doc_id, self.uid, f'{po_number}.pdf', f'/tmp/{po_number}.pdf', po_number, vendor_name, po_date, total_amount, quantity))
        po_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        self.po_ids.append(po_id)
        return po_id

    def gr(self, invoice_doc_id, gr_number, vendor_name, quantity, po_reference, receipt_date='2026-06-02'):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO goods_receipts (document_id, uploaded_by, file_name, file_path, gr_number, vendor_name, receipt_date, po_reference, quantity) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING gr_id",
            (invoice_doc_id, self.uid, f'{gr_number}.pdf', f'/tmp/{gr_number}.pdf', gr_number, vendor_name, receipt_date, po_reference, quantity))
        gr_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        self.gr_ids.append(gr_id)
        return gr_id

    def __exit__(self, *exc):
        conn = get_db_connection()
        cur = conn.cursor()
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


def _shadow_comparison(document_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        return build_shadow_comparison(cur, document_id)
    finally:
        conn.close()


# ============================================================
# Scenario 1: simple case, 1 PO / 1 Invoice / 1 GR -> both engines agree
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario1_simple_case_engines_agree():
    print('Scenario 1: 1 PO, 1 Invoice, 1 GR -> legacy and V2 agree, no differences')
    with _Fixture() as fx:
        inv = fx.invoice('INV-S1', 'Acme Corp', 500.0, 100, 'PO-S1')
        fx.po(inv, 'PO-S1', 'Acme Corp', 100, 500.0)
        fx.gr(inv, 'GR-S1', 'Acme Corp', 100, 'PO-S1')
        build_relationships_for_invoice(inv, dry_run=False)

        comparison = _shadow_comparison(inv)
        check('legacy status PASS', comparison['legacy']['status'] == 'PASS', comparison)
        check('enterprise_v2 status PASS', comparison['enterprise_v2']['status'] == 'PASS', comparison)
        check('legacy == V2 (both PASS, no recorded differences)', comparison['differences'] == [], comparison)


# ============================================================
# Scenario 2: enterprise case, 1 PO / 2 Invoices / 2 GR (PO3006231-style)
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario2_enterprise_case_v2_passes_difference_recorded():
    print('Scenario 2: 1 PO, 2 Invoices, 2 GR (PO3006231-style) -> V2 PASS, legacy may differ, difference recorded')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-S2-A', 'Acme Corp', 3855.0, 15000, 'PO3006231')
        inv_b = fx.invoice('INV-S2-B', 'Acme Corp', 3855.0, 15000, 'PO3006231', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO3006231', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv_a, 'GR-S2-A', 'Acme Corp', 15000, 'PO3006231')
        fx.gr(inv_b, 'GR-S2-B', 'Acme Corp', 15000, 'PO3006231', receipt_date='2026-06-06')

        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        comparison = _shadow_comparison(inv_a)
        check('V2 status PASS', comparison['enterprise_v2']['status'] == 'PASS', comparison)
        check('a difference was recorded', len(comparison['differences']) > 0, comparison)
        check('a difference names matching_status', any(d['field'] == 'matching_status' for d in comparison['differences']), comparison)
        check('reason mentions multiple related invoices',
              any('multiple related invoices' in d['reason'] for d in comparison['differences']), comparison)


# ============================================================
# Scenario 3: invoice without relationships -> legacy fallback, no error
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario3_no_relationships_legacy_fallback_no_error():
    print('Scenario 3: invoice without relationships -> legacy fallback works, no error')
    with _Fixture() as fx:
        inv = fx.invoice('INV-S3', 'Acme Corp', 200.0, 40, 'PO-S3')
        fx.po(inv, 'PO-S3', 'Acme Corp', 40, 200.0)
        # Deliberately never call build_relationships_for_invoice.

        comparison = _shadow_comparison(inv)
        check('no error (comparison returned)', comparison is not None, comparison)
        check('relationship_mode is False', comparison['relationship_mode'] is False, comparison)
        # No GR was created in this fixture, so legacy correctly reports
        # PARTIAL (its own documented "po or gr missing" rule) - the
        # point of this scenario is that it resolves via the legacy
        # fallback with NO ERROR, not a specific status value.
        check('legacy result present (fallback resolved, no error)', comparison['legacy']['status'] in ('PASS', 'PARTIAL', 'REVIEW', 'FAIL'), comparison)
        check('enterprise_v2 is None (nothing to compare)', comparison['enterprise_v2'] is None, comparison)
        check('differences is empty', comparison['differences'] == [], comparison)

        # build_comparison() dispatcher (the normal request path) also
        # works without error for this same invoice with both flags off.
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        result = build_comparison(cur, inv)
        conn.close()
        check('build_comparison() dispatcher works without error', result is not None, result)


# ============================================================
# Scenario 4: multiple PO allocation -> V2 calculates correctly
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario4_multiple_po_allocation_calculated_correctly():
    print('Scenario 4: multiple PO allocation -> V2 calculates correctly')
    with _Fixture() as fx:
        inv = fx.invoice('INV-S4', 'Acme Corp', 100.0, 20, 'PO-S4-X')
        po_x = fx.po(inv, 'PO-S4-X', 'Acme Corp', 20, 100.0)
        build_relationships_for_invoice(inv, dry_run=False)

        # A genuinely multi-PO invoice (the builder alone can't discover
        # this from a single po_reference field) via the manual API path,
        # exactly as Phase 2's own multi-PO test does.
        from helpers.document_relationships import create_relationship, get_related_purchase_orders
        po_y = fx.po(inv, 'PO-S4-Y', 'Acme Corp', 20, 100.0)
        create_relationship('po', po_y, 'invoice', inv, 'po_invoice', matched_quantity=None, matched_amount=None)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        v2_result = auditor_module._build_comparison_v2(cur, inv)
        conn.close()

        check('V2 sees both POs', v2_result['invoice_result']['matched_po_count'] == 2, v2_result['invoice_result'])
        po_ids_in_fulfilment = {pf['po_id'] for pf in v2_result['po_fulfilment']}
        check('po_fulfilment covers both POs', po_ids_in_fulfilment == {po_x, po_y}, v2_result['po_fulfilment'])

        comparison = _shadow_comparison(inv)
        check('shadow comparison reflects the 2-PO matched_po_count',
              comparison['enterprise_v2']['matching_summary']['matched_po_count'] == 2, comparison)


# ============================================================
# Scenario 5: AI Assistant compatibility -> shadow mode never calls AI
# ============================================================

def _raise_if_called(*args, **kwargs):
    raise AssertionError('AI helper was called - shadow mode must never call Claude/Gemini')


@_skip_if_db_unavailable
def run_case_scenario5_shadow_mode_never_calls_ai():
    print('Scenario 5: shadow mode does NOT trigger Claude/Gemini calls')
    import helpers.claude_extractor as claude_extractor
    import helpers.gemini_extractor as gemini_extractor
    import helpers.ai_assistant as ai_assistant_helper

    originals = {}
    for mod, names in (
        (claude_extractor, ['extract_with_claude']),
        (gemini_extractor, ['call_gemini_sdk']),
        (ai_assistant_helper, ['ask_ai_assistant']),
    ):
        for name in names:
            if hasattr(mod, name):
                originals[(mod, name)] = getattr(mod, name)
                setattr(mod, name, _raise_if_called)

    try:
        with _Fixture() as fx:
            inv = fx.invoice('INV-S5', 'Acme Corp', 500.0, 100, 'PO-S5')
            fx.po(inv, 'PO-S5', 'Acme Corp', 100, 500.0)
            fx.gr(inv, 'GR-S5', 'Acme Corp', 100, 'PO-S5')
            build_relationships_for_invoice(inv, dry_run=False)

            Config.ENTERPRISE_MATCHING_V2_SHADOW_MODE = True
            try:
                conn = get_db_connection()
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                build_comparison(cur, inv)
                conn.close()
                _shadow_comparison(inv)
                check('shadow mode ran without ever touching an AI helper', True)
            except AssertionError as e:
                check('shadow mode ran without ever touching an AI helper', False, str(e))
            finally:
                Config.ENTERPRISE_MATCHING_V2_SHADOW_MODE = False
    finally:
        for (mod, name), original in originals.items():
            setattr(mod, name, original)


# ============================================================
# Safe logging content check (STEP 3) - pure, no DB
# ============================================================

def run_case_safe_logging_never_leaks_sensitive_content():
    print('Case: _log_shadow_comparison only prints document_id/timestamp/statuses/diff types')
    comparison = {
        'document_id': 999,
        'relationship_mode': True,
        'legacy': {'status': 'FAIL', 'matching_summary': {
            'vendor_match': True, 'amount_match': False, 'po_reference_match': True,
            'line_items_match': None, 'po_present': True, 'gr_present': True,
        }},
        'enterprise_v2': {'status': 'PASS', 'matching_summary': {
            'matched_po_count': 1, 'matched_gr_count': 1,
            'allocated_quantity': 15000.0, 'allocated_amount': 3855.0, 'related_invoice_count': 1,
        }},
        'differences': [
            {'field': 'matching_status', 'legacy_value': 'FAIL', 'v2_value': 'PASS',
             'reason': 'Enterprise engine detected multiple related invoices'},
        ],
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _log_shadow_comparison(comparison)
    output = buf.getvalue()

    check('logs the document_id', '999' in output, output)
    check('logs legacy status', 'FAIL' in output, output)
    check('logs enterprise status', 'PASS' in output, output)
    check('logs the difference TYPE (field name)', 'matching_status' in output, output)
    check('does NOT log the actual amount value (3855)', '3855' not in output, 'leaked amount!' if '3855' in output else '')
    check('does NOT log a vendor name (this fixture has none, sanity check only)', 'Acme' not in output, output)


if __name__ == '__main__':
    run_case_scenario1_simple_case_engines_agree()
    run_case_scenario2_enterprise_case_v2_passes_difference_recorded()
    run_case_scenario3_no_relationships_legacy_fallback_no_error()
    run_case_scenario4_multiple_po_allocation_calculated_correctly()
    run_case_scenario5_shadow_mode_never_calls_ai()
    run_case_safe_logging_never_leaks_sensitive_content()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
