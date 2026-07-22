"""Regression tests for Enterprise V3 Phase 1 (helpers/document_
relationships.py, routes/document_relationships.py) — the new many-to-
many document_relationships table and its service/API layer.

No real DB, no real Claude/Gemini calls, no real Flask request/JWT
dispatch — get_db_connection is monkey-patched with an in-memory
_FakeCursor/_FakeConn that mimics just the SQL shapes this module's
functions actually issue, same _Patched-monkeypatch style as
tests/extraction/test_document_timeline.py. These functions were also
verified once against a real local Postgres dev DB during development
(constraints, ANY(), RETURNING, JOIN) — this suite re-verifies the
service-layer branching logic offline, repeatably, in CI.

Covers the 4 scenarios the task explicitly asks for:
  1. One PO / One Invoice / One GR
  2. One PO / Two Invoices / Two GR
  3. Two PO / One Invoice / One GR
  4. Existing (pre-Phase-1) documents still work via the legacy fallback

Usage:
    python tests/extraction/test_document_relationships.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask

import helpers.document_relationships as dr
import routes.document_relationships as rdr

FAILURES = []

# _require_node_access() calls jsonify() on the rejection path, which
# needs a Flask application context — a minimal throwaway app is enough.
_test_app = Flask(__name__)


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class _Patched:
    """Context manager that monkey-patches a module's attributes for the
    duration of the `with` block and restores them afterward — same
    pattern used throughout tests/extraction/."""

    def __init__(self, module, **overrides):
        self.module = module
        self.overrides = overrides
        self._originals = {}

    def __enter__(self):
        for name, value in self.overrides.items():
            self._originals[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(self.module, name, value)


# ============================================================
# In-memory fake DB — just enough SQL-shape matching to run
# helpers/document_relationships.py's actual queries offline.
# ============================================================

def _fresh_tables():
    return {
        'documents': [],
        'purchase_orders': [],
        'goods_receipts': [],
        'document_relationships': [],
    }


def _mk_invoice(tables, doc_id, uploaded_by=1):
    tables['documents'].append({
        'document_id': doc_id, 'uploaded_by': uploaded_by, 'invoice_number': f'INV-{doc_id}',
        'vendor_name': 'Vendor', 'total_amount': 100.0, 'currency': 'RM',
    })


def _mk_po(tables, po_id, doc_id, uploaded_by=1, uploaded_at=0):
    tables['purchase_orders'].append({
        'po_id': po_id, 'document_id': doc_id, 'uploaded_by': uploaded_by, 'uploaded_at': uploaded_at,
    })


def _mk_gr(tables, gr_id, doc_id, uploaded_by=1, uploaded_at=0):
    tables['goods_receipts'].append({
        'gr_id': gr_id, 'document_id': doc_id, 'uploaded_by': uploaded_by, 'uploaded_at': uploaded_at,
    })


class _FakeCursor:
    def __init__(self, tables):
        self.tables = tables
        self._result = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        params = params or ()
        s = ' '.join(sql.split())

        if s.startswith('SELECT 1 FROM documents WHERE document_id'):
            self._result = [(1,)] if any(r['document_id'] == params[0] for r in self.tables['documents']) else []
        elif s.startswith('SELECT 1 FROM purchase_orders WHERE po_id'):
            self._result = [(1,)] if any(r['po_id'] == params[0] for r in self.tables['purchase_orders']) else []
        elif s.startswith('SELECT 1 FROM goods_receipts WHERE gr_id'):
            self._result = [(1,)] if any(r['gr_id'] == params[0] for r in self.tables['goods_receipts']) else []
        elif s.startswith('SELECT uploaded_by FROM documents WHERE document_id'):
            rows = [r for r in self.tables['documents'] if r['document_id'] == params[0]]
            self._result = [(rows[0]['uploaded_by'],)] if rows else []
        elif s.startswith('SELECT uploaded_by FROM purchase_orders WHERE po_id'):
            rows = [r for r in self.tables['purchase_orders'] if r['po_id'] == params[0]]
            self._result = [(rows[0]['uploaded_by'],)] if rows else []
        elif s.startswith('SELECT uploaded_by FROM goods_receipts WHERE gr_id'):
            rows = [r for r in self.tables['goods_receipts'] if r['gr_id'] == params[0]]
            self._result = [(rows[0]['uploaded_by'],)] if rows else []
        elif 'SELECT 1 FROM document_relationships WHERE parent_type' in s:
            pt, pid, ct, cid, rt = params
            match = any(r['parent_type'] == pt and r['parent_id'] == pid and r['child_type'] == ct
                        and r['child_id'] == cid and r['relationship_type'] == rt
                        for r in self.tables['document_relationships'])
            self._result = [(1,)] if match else []
        elif s.startswith('INSERT INTO document_relationships'):
            pt, pid, ct, cid, rt, mq, ma, cs = params
            new_id = len(self.tables['document_relationships']) + 1
            row = {'id': new_id, 'parent_type': pt, 'parent_id': pid, 'child_type': ct, 'child_id': cid,
                   'relationship_type': rt, 'matched_quantity': mq, 'matched_amount': ma,
                   'confidence_score': cs, 'created_at': new_id, 'updated_at': new_id}
            self.tables['document_relationships'].append(row)
            self._result = [row]
        elif s.startswith('DELETE FROM document_relationships WHERE id'):
            before = len(self.tables['document_relationships'])
            self.tables['document_relationships'] = [r for r in self.tables['document_relationships'] if r['id'] != params[0]]
            self.rowcount = before - len(self.tables['document_relationships'])
        elif s.startswith('SELECT * FROM document_relationships WHERE id'):
            self._result = [r for r in self.tables['document_relationships'] if r['id'] == params[0]]
        elif 'FROM document_relationships WHERE (parent_type' in s:
            dt1, did1, dt2, did2 = params
            rows = [r for r in self.tables['document_relationships']
                    if (r['parent_type'] == dt1 and r['parent_id'] == did1)
                    or (r['child_type'] == dt2 and r['child_id'] == did2)]
            self._result = sorted(rows, key=lambda r: r['created_at'])
        elif s.startswith('SELECT * FROM purchase_orders WHERE po_id = ANY'):
            self._result = [r for r in self.tables['purchase_orders'] if r['po_id'] in params[0]]
        elif s.startswith('SELECT * FROM purchase_orders WHERE document_id'):
            rows = [r for r in self.tables['purchase_orders'] if r['document_id'] == params[0]]
            self._result = sorted(rows, key=lambda r: r['uploaded_at'], reverse=True)[:1]
        elif s.startswith('SELECT * FROM goods_receipts WHERE gr_id = ANY'):
            self._result = [r for r in self.tables['goods_receipts'] if r['gr_id'] in params[0]]
        elif s.startswith('SELECT * FROM goods_receipts WHERE document_id'):
            rows = [r for r in self.tables['goods_receipts'] if r['document_id'] == params[0]]
            self._result = sorted(rows, key=lambda r: r['uploaded_at'], reverse=True)[:1]
        elif s.startswith('SELECT document_id FROM purchase_orders WHERE po_id'):
            rows = [r for r in self.tables['purchase_orders'] if r['po_id'] == params[0]]
            self._result = [{'document_id': rows[0]['document_id']}] if rows else []
        elif s.startswith('SELECT document_id FROM goods_receipts WHERE gr_id'):
            rows = [r for r in self.tables['goods_receipts'] if r['gr_id'] == params[0]]
            self._result = [{'document_id': rows[0]['document_id']}] if rows else []
        elif s.startswith('SELECT d.*, ef.invoice_number') and 'document_id = ANY' in s:
            self._result = [r for r in self.tables['documents'] if r['document_id'] in params[0]]
        elif s.startswith('SELECT d.*, ef.invoice_number') and 'd.document_id = %s' in s:
            self._result = [r for r in self.tables['documents'] if r['document_id'] == params[0]]
        else:
            raise NotImplementedError(f'Unhandled fake SQL: {s[:100]}')

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, tables):
        self._cursor = _FakeCursor(tables)

    def cursor(self, **kwargs):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


def _patched_dr(tables):
    return _Patched(dr, get_db_connection=lambda: _FakeConn(tables))


# ============================================================
# Scenario 1/4: One PO / One Invoice / One GR
# ============================================================

def run_case_one_po_one_invoice_one_gr():
    print('Case 1/4: One PO, one Invoice, one GR')
    tables = _fresh_tables()
    _mk_invoice(tables, 100)
    _mk_po(tables, 1, doc_id=100)
    _mk_gr(tables, 1, doc_id=100)

    with _patched_dr(tables):
        rel1, err1 = dr.create_relationship('po', 1, 'invoice', 100, 'po_invoice')
        rel2, err2 = dr.create_relationship('invoice', 100, 'gr', 1, 'invoice_gr')
        check('po_invoice relationship created', rel1 is not None, err1)
        check('invoice_gr relationship created', rel2 is not None, err2)

        pos = dr.get_related_purchase_orders('invoice', 100)
        grs = dr.get_related_goods_receipts('invoice', 100)
        invs = dr.get_related_invoices('po', 1)

        check('invoice has exactly 1 related PO', [p['po_id'] for p in pos] == [1], pos)
        check('invoice has exactly 1 related GR', [g['gr_id'] for g in grs] == [1], grs)
        check('PO has exactly 1 related invoice', [i['document_id'] for i in invs] == [100], invs)


# ============================================================
# Scenario 2/4: One PO / Two Invoices / Two GR
# ============================================================

def run_case_one_po_two_invoices_two_gr():
    print('Case 2/4: One PO, two Invoices, two GR (PO7710 example from the task)')
    tables = _fresh_tables()
    _mk_invoice(tables, 200)  # Invoice A
    _mk_invoice(tables, 201)  # Invoice B
    _mk_po(tables, 10, doc_id=200)  # PO originally uploaded under Invoice A (legacy attachment irrelevant here)
    _mk_gr(tables, 20, doc_id=200)  # GR A
    _mk_gr(tables, 21, doc_id=201)  # GR B

    with _patched_dr(tables):
        dr.create_relationship('po', 10, 'invoice', 200, 'po_invoice')  # PO -> Invoice A
        dr.create_relationship('po', 10, 'invoice', 201, 'po_invoice')  # PO -> Invoice B
        dr.create_relationship('invoice', 200, 'gr', 20, 'invoice_gr')  # Invoice A -> GR A
        dr.create_relationship('invoice', 201, 'gr', 21, 'invoice_gr')  # Invoice B -> GR B

        invs_for_po = dr.get_related_invoices('po', 10)
        check('PO is linked to both invoices', sorted(i['document_id'] for i in invs_for_po) == [200, 201], invs_for_po)

        grs_for_a = dr.get_related_goods_receipts('invoice', 200)
        grs_for_b = dr.get_related_goods_receipts('invoice', 201)
        check('Invoice A sees only GR A (not GR B)', [g['gr_id'] for g in grs_for_a] == [20], grs_for_a)
        check('Invoice B sees only GR B (not GR A)', [g['gr_id'] for g in grs_for_b] == [21], grs_for_b)

        pos_for_a = dr.get_related_purchase_orders('invoice', 200)
        pos_for_b = dr.get_related_purchase_orders('invoice', 201)
        check('Invoice A sees the shared PO', [p['po_id'] for p in pos_for_a] == [10], pos_for_a)
        check('Invoice B sees the shared PO', [p['po_id'] for p in pos_for_b] == [10], pos_for_b)


# ============================================================
# Scenario 3/4: Two PO / One Invoice / One GR
# ============================================================

def run_case_two_po_one_invoice_one_gr():
    print('Case 3/4: Two PO, one Invoice, one GR')
    tables = _fresh_tables()
    _mk_invoice(tables, 300)
    _mk_po(tables, 30, doc_id=300)
    _mk_po(tables, 31, doc_id=300)
    _mk_gr(tables, 40, doc_id=300)

    with _patched_dr(tables):
        dr.create_relationship('po', 30, 'invoice', 300, 'po_invoice')
        dr.create_relationship('po', 31, 'invoice', 300, 'po_invoice')
        dr.create_relationship('invoice', 300, 'gr', 40, 'invoice_gr')

        pos = dr.get_related_purchase_orders('invoice', 300)
        check('invoice has both POs', sorted(p['po_id'] for p in pos) == [30, 31], pos)


# ============================================================
# Scenario 4/4: Existing (pre-Phase-1) documents still work —
# the STEP 4 compatibility/legacy-fallback layer.
# ============================================================

def run_case_existing_documents_still_work():
    print('Case 4/4: Existing documents (zero document_relationships rows) still work via legacy fallback')
    tables = _fresh_tables()
    _mk_invoice(tables, 400)
    _mk_po(tables, 50, doc_id=400)   # legacy one-to-one attachment only
    _mk_gr(tables, 60, doc_id=400)   # legacy one-to-one attachment only
    # No create_relationship() calls at all — mirrors data untouched by Phase 1.

    with _patched_dr(tables):
        pos = dr.get_related_purchase_orders('invoice', 400)
        grs = dr.get_related_goods_receipts('invoice', 400)
        inv_for_po = dr.get_related_invoices('po', 50)
        inv_for_gr = dr.get_related_invoices('gr', 60)

        check('legacy PO surfaced via fallback', [p['po_id'] for p in pos] == [50], pos)
        check('legacy GR surfaced via fallback', [g['gr_id'] for g in grs] == [60], grs)
        check('legacy invoice surfaced via fallback (from PO)', [i['document_id'] for i in inv_for_po] == [400], inv_for_po)
        check('legacy invoice surfaced via fallback (from GR)', [i['document_id'] for i in inv_for_gr] == [400], inv_for_gr)
        check('fallback rows report relationship=None (not a real link)', pos[0]['relationship'] is None, pos)


# ============================================================
# create_relationship() validation branches
# ============================================================

def run_case_create_relationship_rejects_unknown_type():
    print('Case: unknown relationship_type is rejected')
    tables = _fresh_tables()
    with _patched_dr(tables):
        rel, err = dr.create_relationship('po', 1, 'invoice', 1, 'not_a_real_type')
    check('rejected', rel is None and err is not None, err)


def run_case_create_relationship_rejects_mismatched_pair():
    print('Case: relationship_type not matching parent/child type is rejected')
    tables = _fresh_tables()
    _mk_invoice(tables, 1)
    with _patched_dr(tables):
        rel, err = dr.create_relationship('invoice', 1, 'po', 1, 'po_invoice')  # backwards
    check('rejected', rel is None and err is not None, err)


def run_case_create_relationship_rejects_nonexistent_entity():
    print('Case: linking to a nonexistent PO is rejected')
    tables = _fresh_tables()
    _mk_invoice(tables, 1)
    with _patched_dr(tables):
        rel, err = dr.create_relationship('po', 999, 'invoice', 1, 'po_invoice')
    check('rejected', rel is None and err is not None, err)


def run_case_create_relationship_rejects_duplicate():
    print('Case: creating the same relationship twice is rejected')
    tables = _fresh_tables()
    _mk_invoice(tables, 1)
    _mk_po(tables, 1, doc_id=1)
    with _patched_dr(tables):
        dr.create_relationship('po', 1, 'invoice', 1, 'po_invoice')
        rel, err = dr.create_relationship('po', 1, 'invoice', 1, 'po_invoice')
    check('second create rejected', rel is None and err is not None, err)


def run_case_delete_relationship():
    print('Case: delete_relationship removes an existing row, no-ops on a missing one')
    tables = _fresh_tables()
    _mk_invoice(tables, 1)
    _mk_po(tables, 1, doc_id=1)
    with _patched_dr(tables):
        rel, _ = dr.create_relationship('po', 1, 'invoice', 1, 'po_invoice')
        deleted = dr.delete_relationship(rel['id'])
        deleted_again = dr.delete_relationship(rel['id'])
    check('first delete succeeds', deleted is True, deleted)
    check('second delete is a no-op (already gone)', deleted_again is False, deleted_again)


# ============================================================
# routes/document_relationships.py — _require_node_access()
# ============================================================

def run_case_access_auditor_unrestricted():
    print('Case: an auditor is granted access without any ownership lookup')
    tables = _fresh_tables()
    _mk_invoice(tables, 1, uploaded_by=999)
    db_calls = []
    with _test_app.app_context(), _Patched(
            rdr,
            get_jwt_identity=lambda: 1,
            get_user_by_id=lambda uid: {'user_id': 1, 'role': 'auditor'},
            get_db_connection=lambda: db_calls.append(1) or _FakeConn(tables)):
        user, err = rdr._require_node_access('invoice', 1)
    check('auditor accepted (no error)', err is None, err)
    check('no DB ownership lookup performed for an auditor', db_calls == [], db_calls)


def run_case_access_finance_owner_accepted():
    print('Case: a finance_executive who uploaded this PO is accepted')
    tables = _fresh_tables()
    _mk_invoice(tables, 1, uploaded_by=5)
    _mk_po(tables, 1, doc_id=1, uploaded_by=5)
    with _test_app.app_context(), _Patched(
            rdr,
            get_jwt_identity=lambda: 5,
            get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
            get_db_connection=lambda: _FakeConn(tables)):
        user, err = rdr._require_node_access('po', 1)
    check('owner accepted (no error)', err is None, err)


def run_case_access_finance_non_owner_rejected():
    print('Case: a finance_executive who did NOT upload this GR is rejected (403)')
    tables = _fresh_tables()
    _mk_invoice(tables, 1, uploaded_by=99)
    _mk_gr(tables, 1, doc_id=1, uploaded_by=99)
    with _test_app.app_context(), _Patched(
            rdr,
            get_jwt_identity=lambda: 5,
            get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
            get_db_connection=lambda: _FakeConn(tables)):
        user, err = rdr._require_node_access('gr', 1)
    check('rejected with 403', err is not None and err[1] == 403, err)


def run_case_access_missing_node_404():
    print('Case: a nonexistent PO (for a finance user) returns 404')
    tables = _fresh_tables()
    with _test_app.app_context(), _Patched(
            rdr,
            get_jwt_identity=lambda: 5,
            get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
            get_db_connection=lambda: _FakeConn(tables)):
        user, err = rdr._require_node_access('po', 999)
    check('rejected with 404', err is not None and err[1] == 404, err)


def run_case_access_rejects_unknown_role():
    print('Case: a role that is neither auditor nor finance_executive is rejected (403)')
    tables = _fresh_tables()
    with _test_app.app_context(), _Patched(
            rdr,
            get_jwt_identity=lambda: 9,
            get_user_by_id=lambda uid: {'user_id': 9, 'role': 'admin'}):
        user, err = rdr._require_node_access('invoice', 1)
    check('rejected with 403', err is not None and err[1] == 403, err)


if __name__ == '__main__':
    run_case_one_po_one_invoice_one_gr()
    run_case_one_po_two_invoices_two_gr()
    run_case_two_po_one_invoice_one_gr()
    run_case_existing_documents_still_work()

    run_case_create_relationship_rejects_unknown_type()
    run_case_create_relationship_rejects_mismatched_pair()
    run_case_create_relationship_rejects_nonexistent_entity()
    run_case_create_relationship_rejects_duplicate()
    run_case_delete_relationship()

    run_case_access_auditor_unrestricted()
    run_case_access_finance_owner_accepted()
    run_case_access_finance_non_owner_rejected()
    run_case_access_missing_node_404()
    run_case_access_rejects_unknown_role()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
