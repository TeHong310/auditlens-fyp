"""Regression tests for Enterprise V3 Phase 4 (Enable Enterprise
Matching V2 and Auditor Experience Integration):
  - routes/auditor.py: build_comparison()'s 4 dispatch scenarios,
    _matching_status_for_comparison(), _classify_exception()'s V2-aware
    branch, get_report_summary()'s enterprise_matching_coverage stat.
  - routes/ai_assistant.py: _build_case_context()'s new V2 fields and
    V2-aware audit_status/matching_status.
  - routes/documents.py: _build_timeline_events()'s V2-aware detail text.

Integration tests use the real local Postgres dev DB - same permitted
"database test data" convention as Phase 2/3's own test suites. Every
test creates its own rows and deletes them in __exit__, leaving the DB
exactly as found.

Usage:
    python tests/extraction/test_phase4_integration.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2.extras
from db import get_db_connection
from helpers.relationship_builder import build_relationships_for_invoice
import routes.auditor as auditor_module
from routes.auditor import build_comparison, _matching_status_for_comparison, _classify_exception
from routes.ai_assistant import _build_case_context
import routes.documents as documents_module
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
        cur.execute("SELECT user_id FROM users WHERE email = 'phase4_test@x.com'")
        row = cur.fetchone()
        if row:
            self.uid = row[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, role, full_name) "
                "VALUES ('phase4_test@x.com', 'x', 'auditor', 'Phase4 Test') RETURNING user_id"
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


# ============================================================
# STEP 1: build_comparison() dispatcher - 4 required scenarios
# ============================================================

@_skip_if_db_unavailable
def run_case_dispatcher_flag_off_returns_legacy():
    print('STEP 1, Case 1: flag OFF -> legacy result returned')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-D1-A', 'Acme Corp', 3855.0, 15000, 'PO-D1')
        inv_b = fx.invoice('INV-D1-B', 'Acme Corp', 3855.0, 15000, 'PO-D1', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO-D1', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv_a, 'GR-D1-A', 'Acme Corp', 15000, 'PO-D1')
        fx.gr(inv_b, 'GR-D1-B', 'Acme Corp', 15000, 'PO-D1', receipt_date='2026-06-06')
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = False
        result = _comparison(inv_a)
        check('legacy shape returned (no engine_version key)', 'engine_version' not in result, result)


@_skip_if_db_unavailable
def run_case_dispatcher_flag_on_returns_v2():
    print('STEP 1, Case 2: flag ON (with relationships) -> V2 result returned')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-D2-A', 'Acme Corp', 3855.0, 15000, 'PO-D2')
        inv_b = fx.invoice('INV-D2-B', 'Acme Corp', 3855.0, 15000, 'PO-D2', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO-D2', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv_a, 'GR-D2-A', 'Acme Corp', 15000, 'PO-D2')
        fx.gr(inv_b, 'GR-D2-B', 'Acme Corp', 15000, 'PO-D2', receipt_date='2026-06-06')
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            result = _comparison(inv_a)
            check('V2 shape returned (engine_version == v2)', result.get('engine_version') == 'v2', result)
            check('V2 status PASS (fully allocated across both invoices)', result['invoice_result']['status'] == 'PASS', result)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_dispatcher_flag_on_no_relationships_legacy_fallback():
    print('STEP 1, Case 3: flag ON but no relationships -> legacy fallback')
    with _Fixture() as fx:
        inv = fx.invoice('INV-D3', 'Acme Corp', 100.0, 10, 'PO-D3')
        fx.po(inv, 'PO-D3', 'Acme Corp', 10, 100.0)
        # Deliberately never call build_relationships_for_invoice.

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            result = _comparison(inv)
            check('legacy shape returned (no relationships to activate V2)', 'engine_version' not in result, result)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_dispatcher_flag_on_v2_error_legacy_fallback():
    print('STEP 1, Case 4: flag ON but V2 raises -> legacy fallback (no 5xx)')
    with _Fixture() as fx:
        inv = fx.invoice('INV-D4', 'Acme Corp', 100.0, 10, 'PO-D4')
        fx.po(inv, 'PO-D4', 'Acme Corp', 10, 100.0)
        fx.gr(inv, 'GR-D4', 'Acme Corp', 10, 'PO-D4')
        build_relationships_for_invoice(inv, dry_run=False)

        def _broken_v2(cursor, document_id):
            raise RuntimeError('simulated V2 failure')

        original = auditor_module._build_comparison_v2
        auditor_module._build_comparison_v2 = _broken_v2
        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            result = _comparison(inv)
            check('legacy shape returned despite V2 raising', 'engine_version' not in result, result)
            check('legacy result is still usable (has invoice/match_result)',
                  result is not None and 'match_result' in result, result)
        finally:
            auditor_module._build_comparison_v2 = original
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


# ============================================================
# STEP 3: exception classification is V2-aware
# ============================================================

@_skip_if_db_unavailable
def run_case_exception_no_false_positive_under_v2_pass():
    print('STEP 3: V2 PASS produces NO matching-related exception, even though legacy alone would flag one')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-E1-A', 'Acme Corp', 3855.0, 15000, 'PO-E1')
        inv_b = fx.invoice('INV-E1-B', 'Acme Corp', 3855.0, 15000, 'PO-E1', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO-E1', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv_a, 'GR-E1-A', 'Acme Corp', 15000, 'PO-E1')
        fx.gr(inv_b, 'GR-E1-B', 'Acme Corp', 15000, 'PO-E1', receipt_date='2026-06-06')
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        legacy_comparison = _comparison(inv_a)  # V2 disabled at this point
        check('sanity: legacy alone reports FAIL (the known one-to-one limitation)',
              legacy_comparison['match_result']['overall_status'] == 'FAIL', legacy_comparison)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            v2_comparison = _comparison(inv_a)
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute('SELECT document_id, status FROM documents WHERE document_id = %s', (inv_a,))
            doc_row = cur.fetchone()
            classified = _classify_exception(cur, doc_row, v2_comparison)
            conn.close()
            check('no exception classified under V2 PASS', classified is None, classified)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_exception_v2_review_required_includes_evidence():
    print('STEP 3: V2 REVIEW_REQUIRED produces a mismatch exception with allocation evidence')
    with _Fixture() as fx:
        # Each invoice individually looks like a fine match against the
        # PO's own quantity (so the builder confidently links both), but
        # their COMBINED allocation (6+6=12) exceeds the PO's ordered
        # quantity (10) - invoice B is the one that overshoots the
        # remaining capacity once invoice A is already accounted for. A
        # single invoice whose OWN quantity exceeds the PO from the
        # start is deliberately NOT used here: the builder correctly
        # refuses to link that (too weak evidence to auto-create a
        # relationship at all), so V2 would never even activate.
        inv_a = fx.invoice('INV-E2-A', 'Acme Corp', 60.0, 6, 'PO-E2')
        inv_b = fx.invoice('INV-E2-B', 'Acme Corp', 60.0, 6, 'PO-E2', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO-E2', 'Acme Corp', 10, 100.0)
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            comparison = _comparison(inv_b)
            check('V2 activated (relationship exists)', comparison.get('engine_version') == 'v2', comparison)
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute('SELECT document_id, status FROM documents WHERE document_id = %s', (inv_b,))
            doc_row = cur.fetchone()
            classified = _classify_exception(cur, doc_row, comparison)
            conn.close()
            check('an exception was classified', classified is not None, comparison)
            if classified:
                _, exc_type, label, detail, severity = classified
                check('exception type is "mismatch" (matches existing filter chip)', exc_type == 'mismatch', exc_type)
                check('label names Enterprise Matching', 'Enterprise Matching' in label, label)
                check('detail includes allocation evidence (PO number)', 'PO-E2' in detail, detail)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


# ============================================================
# STEP 4: AI Assistant context - V2 fields + audit_status correctness
# ============================================================

@_skip_if_db_unavailable
def run_case_ai_context_includes_v2_fields():
    print('STEP 4: _build_case_context includes the new V2 fields when V2 is active')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-C1-A', 'Acme Corp', 3855.0, 15000, 'PO-C1')
        inv_b = fx.invoice('INV-C1-B', 'Acme Corp', 3855.0, 15000, 'PO-C1', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO-C1', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv_a, 'GR-C1-A', 'Acme Corp', 15000, 'PO-C1')
        fx.gr(inv_b, 'GR-C1-B', 'Acme Corp', 15000, 'PO-C1', receipt_date='2026-06-06')
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            context = _case_context(inv_a)
            check('matching_engine_version is v2', context['matching_engine_version'] == 'v2', context)
            check('relationship_mode is True', context['relationship_mode'] is True, context)
            check('related_invoice_count is 1 (sibling invoice B)', context['related_invoice_count'] == 1, context)
            check('cumulative_po_quantity is 30000', context['cumulative_po_quantity'] == 30000, context)
            check('cumulative_invoice_quantity is 30000 (both invoices)', context['cumulative_invoice_quantity'] == 30000, context)
            check('fulfilment_status is FULLY_FULFILLED', context['fulfilment_status'] == 'FULLY_FULFILLED', context)
            check('matching_status is PASS (V2-aware, not legacy FAIL)', context['matching_status'] == 'PASS', context)
            check('audit_status is PASS (not falsely REVIEW REQUIRED)', context['audit_status'] == 'PASS', context)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_ai_context_legacy_fields_when_v2_inactive():
    print('STEP 4: _build_case_context reports legacy defaults when V2 never ran')
    with _Fixture() as fx:
        inv = fx.invoice('INV-C2', 'Acme Corp', 100.0, 10, 'PO-C2')
        fx.po(inv, 'PO-C2', 'Acme Corp', 10, 100.0)
        fx.gr(inv, 'GR-C2', 'Acme Corp', 10, 'PO-C2')
        # No relationships built, V2 stays disabled (default).

        context = _case_context(inv)
        check('matching_engine_version is legacy', context['matching_engine_version'] == 'legacy', context)
        check('relationship_mode is False', context['relationship_mode'] is False, context)
        check('fulfilment_status is None', context['fulfilment_status'] is None, context)


@_skip_if_db_unavailable
def run_case_ai_context_missing_gr_warning_not_blocking_under_v2():
    print('STEP 4: a missing-GR-only warning under V2 does not force REVIEW REQUIRED')
    with _Fixture() as fx:
        inv = fx.invoice('INV-C3', 'Acme Corp', 100.0, 10, 'PO-C3')
        fx.po(inv, 'PO-C3', 'Acme Corp', 10, 100.0)
        # No GR at all.
        build_relationships_for_invoice(inv, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            context = _case_context(inv)
            check('audit_status is still PASS (missing GR is a warning, not blocking, under V2)',
                  context['audit_status'] == 'PASS', context)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


# ============================================================
# STEP 5: Report enterprise_matching_coverage stat
# ============================================================

@_skip_if_db_unavailable
def run_case_report_coverage_stat_reflects_real_relationship_mode():
    print('STEP 5: get_report_summary-style coverage counting reflects real relationship_mode, never fabricated')
    with _Fixture() as fx:
        inv_multi = fx.invoice('INV-R1', 'Acme Corp', 3855.0, 15000, 'PO-R1')
        inv_sibling = fx.invoice('INV-R2', 'Acme Corp', 3855.0, 15000, 'PO-R1', invoice_date='2026-06-05')
        fx.po(inv_multi, 'PO-R1', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv_multi, 'GR-R1', 'Acme Corp', 15000, 'PO-R1')
        fx.gr(inv_sibling, 'GR-R2', 'Acme Corp', 15000, 'PO-R1', receipt_date='2026-06-06')
        build_relationships_for_invoice(inv_multi, dry_run=False)
        build_relationships_for_invoice(inv_sibling, dry_run=False)

        inv_single = fx.invoice('INV-R3', 'Beta Corp', 50.0, 5, 'PO-R3')
        fx.po(inv_single, 'PO-R3', 'Beta Corp', 5, 50.0)
        fx.gr(inv_single, 'GR-R3', 'Beta Corp', 5, 'PO-R3')
        # No relationships built for inv_single -> legacy/single-document.

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            multi_comparison = _comparison(inv_multi)
            single_comparison = _comparison(inv_single)
            check('multi-document invoice reports relationship_mode True', multi_comparison.get('relationship_mode') is True, multi_comparison)
            check('single/legacy invoice reports relationship_mode falsy', not single_comparison.get('relationship_mode'), single_comparison)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


# ============================================================
# STEP 6: Workflow Timeline detail text is V2-aware
# ============================================================

@_skip_if_db_unavailable
def run_case_timeline_detail_reflects_v2_when_active():
    print('STEP 6: Workflow Timeline three_way_matching detail names the Enterprise engine when relationship_mode is true')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-T1-A', 'Acme Corp', 3855.0, 15000, 'PO-T1')
        inv_b = fx.invoice('INV-T1-B', 'Acme Corp', 3855.0, 15000, 'PO-T1', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO-T1', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv_a, 'GR-T1-A', 'Acme Corp', 15000, 'PO-T1')
        fx.gr(inv_b, 'GR-T1-B', 'Acme Corp', 15000, 'PO-T1', receipt_date='2026-06-06')
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            context = _case_context(inv_a)
            events = documents_module._build_timeline_events(context)
            three_way = next(e for e in events if e['event'] == 'three_way_matching')
            check('status is completed', three_way['status'] == 'completed', three_way)
            check('detail names Enterprise matching', 'Enterprise' in three_way['detail'], three_way)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_timeline_detail_unchanged_when_v2_inactive():
    print('STEP 6: Workflow Timeline detail text is unchanged (no regression) when V2 never ran')
    with _Fixture() as fx:
        inv = fx.invoice('INV-T2', 'Acme Corp', 100.0, 10, 'PO-T2')
        fx.po(inv, 'PO-T2', 'Acme Corp', 10, 100.0)
        fx.gr(inv, 'GR-T2', 'Acme Corp', 10, 'PO-T2')

        context = _case_context(inv)
        events = documents_module._build_timeline_events(context)
        three_way = next(e for e in events if e['event'] == 'three_way_matching')
        check('detail is the original "Status: PASS" wording', three_way['detail'] == 'Status: PASS', three_way)


# ============================================================
# STEP 7, Scenarios required by the task
# ============================================================

@_skip_if_db_unavailable
def run_case_scenario1_normal_pass():
    print('STEP 7, Scenario 1: normal 1 invoice/1 PO/1 GR -> PASS')
    with _Fixture() as fx:
        inv = fx.invoice('INV-N1', 'Acme Corp', 100.0, 10, 'PO-N1')
        fx.po(inv, 'PO-N1', 'Acme Corp', 10, 100.0)
        fx.gr(inv, 'GR-N1', 'Acme Corp', 10, 'PO-N1')
        build_relationships_for_invoice(inv, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            result = _comparison(inv)
            check('PASS', result['invoice_result']['status'] == 'PASS', result)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_scenario3_partial_fulfilment_invoice_passes_po_partial():
    print('STEP 7, Scenario 3: partial fulfilment -> invoice PASSes, PO remains partially fulfilled')
    with _Fixture() as fx:
        inv = fx.invoice('INV-N3', 'Acme Corp', 3855.0, 15000, 'PO-N3')
        fx.po(inv, 'PO-N3', 'Acme Corp', 30000, 7710.0)
        fx.gr(inv, 'GR-N3', 'Acme Corp', 15000, 'PO-N3')
        build_relationships_for_invoice(inv, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            result = _comparison(inv)
            check('invoice PASSes (fully supported for its own allocation)', result['invoice_result']['status'] == 'PASS', result)
            check('PO remains OPEN_PARTIALLY_INVOICED', result['po_fulfilment'][0]['status'] == 'OPEN_PARTIALLY_INVOICED', result)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_scenario4_over_invoicing_review_required():
    print('STEP 7, Scenario 4: over-invoicing (cumulative, across 2 invoices) -> REVIEW REQUIRED')
    with _Fixture() as fx:
        inv_a = fx.invoice('INV-N4-A', 'Acme Corp', 60.0, 6, 'PO-N4')
        inv_b = fx.invoice('INV-N4-B', 'Acme Corp', 60.0, 6, 'PO-N4', invoice_date='2026-06-05')
        fx.po(inv_a, 'PO-N4', 'Acme Corp', 10, 100.0)
        build_relationships_for_invoice(inv_a, dry_run=False)
        build_relationships_for_invoice(inv_b, dry_run=False)

        Config.ENTERPRISE_MATCHING_V2_ENABLED = True
        try:
            result = _comparison(inv_b)
            check('REVIEW_REQUIRED (invoice B overshoots remaining PO capacity)',
                  result['invoice_result']['status'] == 'REVIEW_REQUIRED', result)
            check('PO fulfilment reports OVER_INVOICED', result['po_fulfilment'][0]['status'] == 'OVER_INVOICED', result)
        finally:
            Config.ENTERPRISE_MATCHING_V2_ENABLED = False


@_skip_if_db_unavailable
def run_case_scenario6_ai_assistant_receives_v2_context_no_ai_calls():
    print('STEP 7, Scenario 6: AI Assistant receives V2 context; verify no Claude/Gemini calls')

    def _raise_if_called(*args, **kwargs):
        raise AssertionError('AI helper was called building case context - it must never call AI')

    import helpers.claude_extractor as claude_extractor
    import helpers.gemini_extractor as gemini_extractor

    originals = {}
    for mod, names in ((claude_extractor, ['extract_with_claude']), (gemini_extractor, ['call_gemini_sdk'])):
        for name in names:
            if hasattr(mod, name):
                originals[(mod, name)] = getattr(mod, name)
                setattr(mod, name, _raise_if_called)

    try:
        with _Fixture() as fx:
            inv_a = fx.invoice('INV-N6-A', 'Acme Corp', 3855.0, 15000, 'PO-N6')
            inv_b = fx.invoice('INV-N6-B', 'Acme Corp', 3855.0, 15000, 'PO-N6', invoice_date='2026-06-05')
            fx.po(inv_a, 'PO-N6', 'Acme Corp', 30000, 7710.0)
            fx.gr(inv_a, 'GR-N6-A', 'Acme Corp', 15000, 'PO-N6')
            fx.gr(inv_b, 'GR-N6-B', 'Acme Corp', 15000, 'PO-N6', receipt_date='2026-06-06')
            build_relationships_for_invoice(inv_a, dry_run=False)
            build_relationships_for_invoice(inv_b, dry_run=False)

            Config.ENTERPRISE_MATCHING_V2_ENABLED = True
            try:
                context = _case_context(inv_a)
                check('context reflects V2', context['matching_engine_version'] == 'v2', context)
                check('building context never called an AI helper', True)
            except AssertionError as e:
                check('building context never called an AI helper', False, str(e))
            finally:
                Config.ENTERPRISE_MATCHING_V2_ENABLED = False
    finally:
        for (mod, name), original in originals.items():
            setattr(mod, name, original)


if __name__ == '__main__':
    run_case_dispatcher_flag_off_returns_legacy()
    run_case_dispatcher_flag_on_returns_v2()
    run_case_dispatcher_flag_on_no_relationships_legacy_fallback()
    run_case_dispatcher_flag_on_v2_error_legacy_fallback()

    run_case_exception_no_false_positive_under_v2_pass()
    run_case_exception_v2_review_required_includes_evidence()

    run_case_ai_context_includes_v2_fields()
    run_case_ai_context_legacy_fields_when_v2_inactive()
    run_case_ai_context_missing_gr_warning_not_blocking_under_v2()

    run_case_report_coverage_stat_reflects_real_relationship_mode()

    run_case_timeline_detail_reflects_v2_when_active()
    run_case_timeline_detail_unchanged_when_v2_inactive()

    run_case_scenario1_normal_pass()
    run_case_scenario3_partial_fulfilment_invoice_passes_po_partial()
    run_case_scenario4_over_invoicing_review_required()
    run_case_scenario6_ai_assistant_receives_v2_context_no_ai_calls()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
