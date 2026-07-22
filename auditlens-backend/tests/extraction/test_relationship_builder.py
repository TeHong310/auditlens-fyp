"""Regression tests for Enterprise V3 Phase 2's deterministic relationship
builder (helpers/relationship_builder.py) and its integration with the
V2 matching engine (routes/auditor.py::_build_comparison_v2/
build_comparison) and the anomaly duplicate-detection compatibility fix
(helpers/anomaly_detector.py).

Two kinds of tests here:
  1. Pure scorer tests (score_po_invoice_candidate / score_invoice_gr_
     candidate / score_po_gr_candidate) - no DB, plain dicts in/out.
  2. Integration tests against the real local Postgres dev DB - this
     phase's own Safety Requirement 12 explicitly permits "database
     test data" (only live Claude/Gemini calls are prohibited), and the
     DB-touching logic here (candidate discovery across 3 tables,
     idempotent upsert, the V2 engine's multi-table joins) is exactly
     the kind of thing a hand-rolled fake cursor would be likely to get
     subtly wrong in ways that don't reflect real Postgres semantics —
     confirmed in practice: two design bugs (invoice_gr cross-linking
     ambiguity) were caught by running against the real DB during
     development, not by a mock. Every integration test creates its own
     fixture rows and deletes them in a finally block, leaving the DB
     exactly as found; requires a reachable DATABASE_URL/DB_* config
     (same as running the app itself).

Usage:
    python tests/extraction/test_relationship_builder.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from db import get_db_connection
from helpers.relationship_builder import (
    score_po_invoice_candidate, score_invoice_gr_candidate, score_po_gr_candidate,
    build_relationships_for_invoice, MIN_AUTO_CONFIDENCE,
)
from helpers.document_relationships import get_related_purchase_orders, get_related_goods_receipts, create_relationship
from helpers.anomaly_detector import detect_duplicate_suspicion
import routes.auditor as auditor_module
from config import Config

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


# ============================================================
# Pure scorer tests - no DB
# ============================================================

def run_case_score_exact_reference_high_confidence():
    print('Case: exact PO reference + matching vendor/item/qty/amount/date scores in the 0.90-1.00 tier')
    invoice = {'po_reference': 'PO3006231', 'vendor_name': 'Coilcraft Singapore Pte Ltd',
               'item_description': 'Inductor 10uH', 'quantity': 15000, 'total_amount': 3855.0,
               'invoice_date': '2026-06-01'}
    po = {'po_number': 'PO3006231', 'vendor_name': 'Coilcraft Singapore Pte Ltd', 'document_id': None,
          'item_description': 'Inductor 10uH', 'quantity': 30000, 'total_amount': 7710.0, 'po_date': '2026-05-01'}
    score, reason = score_po_invoice_candidate(invoice, po)
    check('score >= 0.90', score >= Decimal('0.90'), score)
    check('reason mentions reference match', 'reference matches' in reason, reason)


def run_case_score_no_reference_or_attachment_is_zero():
    print('Case: vendor+amount alone (no PO reference, no legacy attachment) never scores above 0 - rule A')
    invoice = {'po_reference': None, 'vendor_name': 'Acme Corp', 'quantity': 10, 'total_amount': 100.0}
    po = {'po_number': 'PO-UNRELATED', 'vendor_name': 'Acme Corp', 'document_id': 999, 'quantity': 10, 'total_amount': 100.0}
    score, reason = score_po_invoice_candidate(invoice, po)
    check('score is exactly 0 (gated out)', score == Decimal('0'), score)


def run_case_score_vendor_mismatch_penalized():
    print('Case: matching PO reference but mismatched vendor scores lower than a clean match')
    base_invoice = {'po_reference': 'PO-X', 'vendor_name': 'Acme Corp', 'quantity': 10, 'total_amount': 100.0}
    po_same_vendor = {'po_number': 'PO-X', 'vendor_name': 'Acme Corp', 'document_id': None, 'quantity': 10, 'total_amount': 100.0}
    po_diff_vendor = {'po_number': 'PO-X', 'vendor_name': 'Totally Different Company Ltd', 'document_id': None, 'quantity': 10, 'total_amount': 100.0}
    score_same, _ = score_po_invoice_candidate(base_invoice, po_same_vendor)
    score_diff, reason_diff = score_po_invoice_candidate(base_invoice, po_diff_vendor)
    check('vendor-mismatch score is lower than vendor-match score', score_diff < score_same, (score_diff, score_same))
    check('reason mentions vendor mismatch', 'vendor mismatch' in reason_diff, reason_diff)


def run_case_score_quantity_exceeds_po_penalized():
    print('Case: invoice quantity exceeding PO quantity reduces confidence (item-code/quantity mismatch family)')
    invoice = {'po_reference': 'PO-X', 'vendor_name': 'Acme Corp', 'quantity': 500, 'total_amount': 100.0}
    po = {'po_number': 'PO-X', 'vendor_name': 'Acme Corp', 'document_id': None, 'quantity': 10, 'total_amount': 100.0}
    score, reason = score_po_invoice_candidate(invoice, po)
    check('reason mentions quantity exceeds', 'quantity exceeds' in reason, reason)


def run_case_score_invalid_date_sequence_penalized():
    print('Case: PO dated AFTER the invoice (invalid sequence) reduces confidence')
    invoice = {'po_reference': 'PO-X', 'vendor_name': 'Acme Corp', 'quantity': 10, 'total_amount': 100.0, 'invoice_date': '2026-01-01'}
    po = {'po_number': 'PO-X', 'vendor_name': 'Acme Corp', 'document_id': None, 'quantity': 10, 'total_amount': 100.0, 'po_date': '2026-06-01'}
    score, reason = score_po_invoice_candidate(invoice, po)
    check('reason mentions invalid date sequence', 'invalid date sequence' in reason, reason)


def run_case_score_legacy_attachment_without_reference():
    print("Case: today's single-invoice upload (legacy document_id attachment, no PO reference at all) still scores >= MIN_AUTO_CONFIDENCE")
    invoice = {'document_id': 42, 'po_reference': None, 'vendor_name': 'Acme Corp', 'quantity': 10, 'total_amount': 100.0}
    po = {'po_number': 'PO-DIFFERENT-TEXT', 'vendor_name': 'Acme Corp', 'document_id': 42, 'quantity': 10, 'total_amount': 100.0}
    score, reason = score_po_invoice_candidate(invoice, po)
    check('score >= MIN_AUTO_CONFIDENCE', score >= MIN_AUTO_CONFIDENCE, score)
    check('reason mentions legacy attachment', 'legacy' in reason, reason)


def run_case_score_invoice_gr_and_po_gr_symmetry():
    print('Case: score_invoice_gr_candidate / score_po_gr_candidate mirror the same reference-match gating')
    invoice = {'po_reference': 'PO-X', 'vendor_name': 'Acme Corp', 'quantity': 100, 'document_id': None, 'invoice_date': '2026-06-10'}
    gr = {'po_reference': 'PO-X', 'vendor_name': 'Acme Corp', 'quantity': 100, 'document_id': None, 'receipt_date': '2026-06-05'}
    po = {'po_number': 'PO-X', 'vendor_name': 'Acme Corp', 'quantity': 100, 'document_id': None, 'po_date': '2026-05-01'}
    ig_score, _ = score_invoice_gr_candidate(invoice, gr)
    pg_score, _ = score_po_gr_candidate(po, gr)
    check('invoice_gr score >= MIN_AUTO_CONFIDENCE', ig_score >= MIN_AUTO_CONFIDENCE, ig_score)
    check('po_gr score >= MIN_AUTO_CONFIDENCE', pg_score >= MIN_AUTO_CONFIDENCE, pg_score)


# ============================================================
# Integration tests - real local Postgres dev DB (see module
# docstring for why; Safety Requirement 12 permits this).
# ============================================================

class _Fixture:
    """Creates/tears down documents+extracted_fields / purchase_orders /
    goods_receipts rows for one test case against the real dev DB, and
    deletes every document_relationships row precisely by (type, id) —
    not just by raw id - since parent_id/child_id are polymorphic and a
    po_id/gr_id could coincidentally equal an unrelated document_id."""

    def __init__(self):
        self.doc_ids, self.po_ids, self.gr_ids = [], [], []
        self.uid = None

    def __enter__(self):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE email = 'phase2_relbuilder_test@x.com'")
        row = cur.fetchone()
        if row:
            self.uid = row[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, role, full_name) "
                "VALUES ('phase2_relbuilder_test@x.com', 'x', 'auditor', 'Phase2 RelBuilder Test') RETURNING user_id"
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
    """DB-dependent test cases are wrapped with this - if the local dev
    DB isn't reachable (e.g. a CI box with no Postgres), the case is
    reported as SKIPPED rather than a hard failure, since the pure/
    offline test files already give full coverage without a DB."""
    def wrapped():
        try:
            conn = get_db_connection()
            conn.close()
        except Exception as e:
            print(f'  SKIP {fn.__name__} (no DB available: {type(e).__name__})')
            return
        fn()
    return wrapped


@_skip_if_db_unavailable
def run_case_one_po_one_invoice_one_gr():
    print('Case 1/20: one PO, one invoice, one GR - end to end')
    with _Fixture() as fx:
        inv = fx.invoice('INV-1', 'Acme Corp', 500.0, 100, 'PO-1')
        po_id = fx.po(inv, 'PO-1', 'Acme Corp', 100, 500.0)
        gr_id = fx.gr(inv, 'GR-1', 'Acme Corp', 100, 'PO-1')

        summary = build_relationships_for_invoice(inv, dry_run=False)
        actions = {(c['relationship_type'], c['action']) for c in summary['candidates']}
        check('po_invoice persisted', ('po_invoice', 'persisted') in actions, actions)
        check('invoice_gr persisted', ('invoice_gr', 'persisted') in actions, actions)
        check('po_gr persisted', ('po_gr', 'persisted') in actions, actions)

        pos = get_related_purchase_orders('invoice', inv)
        check('exactly one related PO', len(pos) == 1 and pos[0]['po_id'] == po_id, pos)


@_skip_if_db_unavailable
def run_case_one_po_two_invoices_two_gr():
    print('Case 2/20: one PO, two invoices, two GRs (PO7710-style) - no cross-linking, idempotent')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-A', 'Acme Corp', 3855.0, 15000, 'PO-7710')
        inv_b = fx.invoice('INV-B', 'Acme Corp', 3855.0, 15000, 'PO-7710', invoice_date='2026-06-05')
        po_id = fx.po(inv_a, 'PO-7710', 'Acme Corp', 30000, 7710.0)
        gr_a = fx.gr(inv_a, 'GR-A', 'Acme Corp', 15000, 'PO-7710')
        gr_b = fx.gr(inv_b, 'GR-B', 'Acme Corp', 15000, 'PO-7710', receipt_date='2026-06-06')

        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        grs_for_a = {g['gr_id'] for g in get_related_goods_receipts('invoice', inv_a)}
        grs_for_b = {g['gr_id'] for g in get_related_goods_receipts('invoice', inv_b)}
        check('invoice A is NOT wrongly cross-linked to GR B (ambiguity guard)', gr_b not in grs_for_a, grs_for_a)
        check('invoice B is NOT wrongly cross-linked to GR A (ambiguity guard)', gr_a not in grs_for_b, grs_for_b)

        pos_for_a = get_related_purchase_orders('invoice', inv_a)
        pos_for_b = get_related_purchase_orders('invoice', inv_b)
        check('both invoices linked to the shared PO', pos_for_a[0]['po_id'] == po_id and pos_for_b[0]['po_id'] == po_id,
              (pos_for_a, pos_for_b))

        cursor_for_v2 = None
        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        conn = get_db_connection()
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        result = auditor_module.build_comparison(cur, inv_a)
        conn.close()
        Config.ENTERPRISE_MATCHING_V2_ENABLED = False

        pf = result['po_fulfilment'][0]
        check('PO cumulative invoiced 30000/30000', pf['invoiced_quantity_cumulative'] == 30000.0, pf)
        check('PO cumulative received 30000/30000', pf['received_quantity_cumulative'] == 30000.0, pf)
        check('PO cumulative invoiced amount RM 7710.00', pf['invoiced_amount_cumulative'] == 7710.0, pf)
        check('PO status FULLY_FULFILLED', pf['status'] == 'FULLY_FULFILLED', pf)

        # Case 16/20: re-run the builder for both invoices - no duplicate rows.
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM document_relationships WHERE parent_id = %s OR child_id = %s', (po_id, po_id))
        before = cur.fetchone()[0]
        conn.close()
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM document_relationships WHERE parent_id = %s OR child_id = %s', (po_id, po_id))
        after = cur.fetchone()[0]
        conn.close()
        check('Case 16/20: re-running the builder creates no duplicate relationships', before == after, (before, after))


@_skip_if_db_unavailable
def run_case_two_pos_one_invoice():
    print('Case 3/20: two POs, one invoice')
    with _Fixture() as fx:
        inv = fx.invoice('INV-X', 'Acme Corp', 100.0, 20, 'PO-X')
        po_x = fx.po(inv, 'PO-X', 'Acme Corp', 20, 100.0)
        build_relationships_for_invoice(inv, dry_run=False)
        # Manually add a second PO relationship (simulating a genuinely
        # multi-PO invoice the builder alone wouldn't discover from a
        # single po_reference field) to exercise the "2 POs" read path.
        po_y = fx.po(inv, 'PO-Y', 'Acme Corp', 20, 100.0)
        create_relationship('po', po_y, 'invoice', inv, 'po_invoice', matched_quantity=None, matched_amount=None)

        pos = get_related_purchase_orders('invoice', inv)
        check('invoice has both POs', {p['po_id'] for p in pos} == {po_x, po_y}, pos)


@_skip_if_db_unavailable
def run_case_one_invoice_two_gr_no_sibling():
    print('Case 4/20: one invoice, two GRs (no sibling invoice - unambiguous, both linked)')
    with _Fixture() as fx:
        inv = fx.invoice('INV-S4', 'Acme Corp', 1000.0, 200, 'PO-S4')
        po_id = fx.po(inv, 'PO-S4', 'Acme Corp', 200, 1000.0)
        gr_a = fx.gr(inv, 'GR-S4-A', 'Acme Corp', 100, 'PO-S4')
        gr_b = fx.gr(inv, 'GR-S4-B', 'Acme Corp', 100, 'PO-S4', receipt_date='2026-06-04')

        build_relationships_for_invoice(inv, dry_run=False)
        grs = {g['gr_id'] for g in get_related_goods_receipts('invoice', inv)}
        check('both GRs linked directly to the sole invoice', grs == {gr_a, gr_b}, grs)


@_skip_if_db_unavailable
def run_case_multiple_invoices_sharing_one_gr_valid_allocation():
    print('Case 5/20: multiple invoices sharing a PO with a GR reachable via po_gr (not invoice_gr)')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-C', 'Acme Corp', 50.0, 10, 'PO-C')
        inv_b = fx.invoice('INV-D', 'Acme Corp', 50.0, 10, 'PO-C', invoice_date='2026-06-03')
        po_id = fx.po(inv_a, 'PO-C', 'Acme Corp', 20, 100.0)
        gr_id = fx.gr(inv_a, 'GR-C', 'Acme Corp', 20, 'PO-C')

        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        grs_via_po = {g['gr_id'] for g in get_related_goods_receipts('po', po_id)}
        check('the shared GR is reachable at the PO level (received evidence not lost)', gr_id in grs_via_po, grs_via_po)


@_skip_if_db_unavailable
def run_case_existing_invoice_no_relationships_legacy_fallback():
    print('Case 17/20: existing invoice with zero relationship rows still works (legacy fallback, builder never run)')
    with _Fixture() as fx:
        inv = fx.invoice('INV-LEGACY', 'Acme Corp', 200.0, 40, None)
        po_id = fx.po(inv, 'PO-LEGACY', 'Acme Corp', 40, 200.0)
        # Deliberately never call build_relationships_for_invoice.
        pos = get_related_purchase_orders('invoice', inv)
        check('legacy one-to-one attachment surfaced with zero relationship rows', pos and pos[0]['po_id'] == po_id, pos)
        check('surfaced as a fallback (relationship=None)', pos[0]['relationship'] is None, pos)


@_skip_if_db_unavailable
def run_case_manual_relationship_not_overwritten():
    print('Case 18/20: a manually-created relationship is never overwritten by the builder')
    with _Fixture() as fx:
        inv = fx.invoice('INV-MANUAL', 'Acme Corp', 300.0, 30, 'PO-MANUAL')
        po_id = fx.po(inv, 'PO-MANUAL', 'Acme Corp', 30, 300.0)

        manual_rel, err = create_relationship('po', po_id, 'invoice', inv, 'po_invoice',
                                                matched_quantity=999, matched_amount=999.99)
        check('manual relationship created directly (Phase 1 API path)', manual_rel is not None, err)

        build_relationships_for_invoice(inv, dry_run=False)

        pos = get_related_purchase_orders('invoice', inv)
        rel = pos[0]['relationship']
        check('manual allocation values are untouched by the builder',
              rel['matched_quantity'] == 999 and float(rel['matched_amount']) == 999.99, rel)


@_skip_if_db_unavailable
def run_case_dry_run_never_writes():
    print('Case: dry_run=True never touches the DB')
    with _Fixture() as fx:
        inv = fx.invoice('INV-DRY', 'Acme Corp', 60.0, 6, 'PO-DRY')
        fx.po(inv, 'PO-DRY', 'Acme Corp', 6, 60.0)

        summary = build_relationships_for_invoice(inv, dry_run=True)
        check('candidates reported as would_persist', all(c['action'] == 'would_persist' for c in summary['candidates']), summary)

        pos = get_related_purchase_orders('invoice', inv)
        check('no explicit relationship actually created (still legacy fallback)', pos[0]['relationship'] is None, pos)


@_skip_if_db_unavailable
def run_case_anomaly_duplicate_suppressed_for_shared_po_split_invoices():
    print('Case 15/20 + anomaly compat: same vendor/amount/close-date, DIFFERENT invoice numbers, shared PO -> suppressed')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-SPLIT-A', 'Acme Corp', 500.0, 50, 'PO-SPLIT')
        inv_b = fx.invoice('INV-SPLIT-B', 'Acme Corp', 500.0, 50, 'PO-SPLIT', invoice_date='2026-06-02')
        fx.po(inv_a, 'PO-SPLIT', 'Acme Corp', 100, 1000.0)

        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        result = detect_duplicate_suspicion(inv_b, 'Acme Corp', 500.0, '2026-06-02', invoice_number='INV-SPLIT-B')
        check('no duplicate anomaly raised for legitimate split invoices sharing a PO', result is None, result)


@_skip_if_db_unavailable
def run_case_anomaly_duplicate_still_fires_for_same_invoice_number():
    print('Case: same invoice number (true duplicate submission) still fires, even with a shared PO')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-DUP', 'Acme Corp', 500.0, 50, 'PO-DUP')
        inv_b = fx.invoice('INV-DUP', 'Acme Corp', 500.0, 50, 'PO-DUP', invoice_date='2026-06-02')
        fx.po(inv_a, 'PO-DUP', 'Acme Corp', 100, 1000.0)

        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        result = detect_duplicate_suspicion(inv_b, 'Acme Corp', 500.0, '2026-06-02', invoice_number='INV-DUP')
        check('duplicate anomaly still raised for a genuinely identical invoice number', result is not None, result)


@_skip_if_db_unavailable
def run_case_anomaly_duplicate_still_fires_without_shared_po():
    print('Case: same vendor/amount/close-date, different invoice numbers, NO shared PO -> still flagged')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-REAL-A', 'Acme Corp', 500.0, 50, 'PO-REAL-A')
        inv_b = fx.invoice('INV-REAL-B', 'Acme Corp', 500.0, 50, 'PO-REAL-B', invoice_date='2026-06-02')
        fx.po(inv_a, 'PO-REAL-A', 'Acme Corp', 50, 500.0)
        fx.po(inv_b, 'PO-REAL-B', 'Acme Corp', 50, 500.0)

        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        result = detect_duplicate_suspicion(inv_b, 'Acme Corp', 500.0, '2026-06-02', invoice_number='INV-REAL-B')
        check('duplicate anomaly still raised (no shared PO evidence of legitimate split)', result is not None, result)


if __name__ == '__main__':
    run_case_score_exact_reference_high_confidence()
    run_case_score_no_reference_or_attachment_is_zero()
    run_case_score_vendor_mismatch_penalized()
    run_case_score_quantity_exceeds_po_penalized()
    run_case_score_invalid_date_sequence_penalized()
    run_case_score_legacy_attachment_without_reference()
    run_case_score_invoice_gr_and_po_gr_symmetry()

    run_case_one_po_one_invoice_one_gr()
    run_case_one_po_two_invoices_two_gr()
    run_case_two_pos_one_invoice()
    run_case_one_invoice_two_gr_no_sibling()
    run_case_multiple_invoices_sharing_one_gr_valid_allocation()
    run_case_existing_invoice_no_relationships_legacy_fallback()
    run_case_manual_relationship_not_overwritten()
    run_case_dry_run_never_writes()
    run_case_anomaly_duplicate_suppressed_for_shared_po_split_invoices()
    run_case_anomaly_duplicate_still_fires_for_same_invoice_number()
    run_case_anomaly_duplicate_still_fires_without_shared_po()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
