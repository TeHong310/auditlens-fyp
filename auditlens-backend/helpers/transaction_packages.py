"""Enterprise V3 Phase 5 — Finance Transaction Package workflow service
layer. Lets Finance group related AP documents (invoices/POs/GRs) into
one package for organizational visibility before auditor review.

Purely organizational bookkeeping: does NOT replace or duplicate
document_relationships (Phase 1's matching-relevant graph), does not
perform any matching calculation of its own, and never calls Claude/
Gemini. The only "processing" this module triggers is calling the
EXISTING, unmodified deterministic relationship builder (helpers/
relationship_builder.py::build_relationships_for_invoice) so Enterprise
Matching V2 (Phase 2, also unmodified) picks up newly-grouped documents
automatically at read time — nothing here recomputes or duplicates that
logic.

Documents are uploaded via the EXISTING, unmodified upload endpoints
(POST /documents/upload, /documents/upload-po/<id>, /documents/
upload-gr/<id>) — this module only links already-uploaded document_ids
into a package. parent_id-style polymorphism (document_id meaning
differs by document_role) mirrors Phase 1's document_relationships
design exactly, for the same reason: purchase_orders/goods_receipts
rows have no independent row in `documents`.
"""
import re
from decimal import Decimal
import psycopg2.extras
from db import get_db_connection
from helpers.document_relationships import _entity_exists, get_related_invoices, get_related_goods_receipts
from helpers.relationship_builder import build_relationships_for_invoice, AMOUNT_TOLERANCE
from helpers.entity_normalizer import is_same_company

VALID_ROLES = ('invoice', 'purchase_order', 'goods_receipt')

# Phase 1's document_relationships uses short type codes ('invoice'|
# 'po'|'gr'); this table uses the task-specified role names — mapped
# here rather than duplicating _ENTITY_TABLES's table/column lookup.
_ROLE_TO_ENTITY_TYPE = {'invoice': 'invoice', 'purchase_order': 'po', 'goods_receipt': 'gr'}


def create_package(package_name, created_by):
    """Creates a new transaction_packages row with status='draft'.
    Returns the created package as a dict."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            "INSERT INTO transaction_packages (package_name, created_by, status) "
            "VALUES (%s, %s, 'draft') RETURNING *",
            (package_name, created_by)
        )
        row = dict(cursor.fetchone())
        conn.commit()
        return row
    finally:
        conn.close()


def get_package(package_id):
    """Returns one package as a dict, or None if it doesn't exist."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute('SELECT * FROM transaction_packages WHERE id = %s', (package_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def compute_package_status(package_id):
    """Deterministic status, no AI/matching call:
      - 'draft': no documents linked yet.
      - 'waiting_documents': has documents, but no invoice yet — a PO/GR
        alone cannot be OCR-processed without an invoice to anchor it
        (see the upload endpoints' own document_id-scoped design), so
        the package is genuinely incomplete until an invoice is added.
      - 'processing': has at least one invoice, not all of them approved.
      - 'completed': has at least one invoice and every linked invoice's
        documents.status is 'approved' (the existing auditor decision
        field — nothing new computed here).
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            "SELECT document_role, document_id FROM transaction_package_documents WHERE package_id = %s",
            (package_id,)
        )
        rows = cursor.fetchall()
        if not rows:
            return 'draft'

        invoice_ids = [r['document_id'] for r in rows if r['document_role'] == 'invoice']
        if not invoice_ids:
            return 'waiting_documents'

        cursor.execute('SELECT status FROM documents WHERE document_id = ANY(%s)', (invoice_ids,))
        statuses = [r['status'] for r in cursor.fetchall()]
        if statuses and all(s == 'approved' for s in statuses):
            return 'completed'
        return 'processing'
    finally:
        conn.close()


def _recompute_and_persist_status(package_id):
    status = compute_package_status(package_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('UPDATE transaction_packages SET status = %s, updated_at = NOW() WHERE id = %s', (status, package_id))
        conn.commit()
    finally:
        conn.close()
    return status


def _rebuild_relationships_for_package(package_id):
    """Calls the EXISTING, unmodified deterministic relationship
    builder for every invoice currently in this package — never
    computes a relationship itself. A per-invoice failure is logged and
    never propagated, matching this codebase's established "a
    background/best-effort step must never block the primary action"
    philosophy (e.g. Phase 2's own upload-time guidance)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT document_id FROM transaction_package_documents WHERE package_id = %s AND document_role = 'invoice'",
            (package_id,)
        )
        invoice_ids = [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()

    for invoice_id in invoice_ids:
        try:
            build_relationships_for_invoice(invoice_id, dry_run=False)
        except Exception as e:
            print(f"WARNING: relationship builder failed for invoice {invoice_id} "
                  f"in transaction package {package_id}: {type(e).__name__}: {e}")


def _normalize_invoice_number(value):
    if not value:
        return ''
    return re.sub(r'[^a-z0-9]', '', value.lower())


_INVOICE_DATE_TOLERANCE_DAYS = 3


def find_existing_package_for_invoice(document_id):
    """Bug 2 fix — before linking an invoice into a package, checks
    whether an invoice matching by invoice_number + vendor + amount +
    invoice_date already belongs to a DIFFERENT existing package. Without
    this, the same real invoice could be linked into two separate
    packages (nothing enforced document_id uniqueness across packages),
    with one package showing it correctly matched against its real PO/GR
    and the other wrongly showing "Missing PO and GR" for the exact same
    invoice — the relationship builder only ever runs for invoices that
    are members of THAT specific package (see
    _rebuild_relationships_for_package), so a fragmented, PO/GR-less
    package for the same invoice never resolves relationships on its own.

    All four signals must agree (invoice_number normalized-exact,
    vendor fuzzy via the existing is_same_company(), amount within the
    existing AMOUNT_TOLERANCE, invoice_date within a few days) —
    deliberately strict to avoid ever blocking two genuinely different
    invoices that happen to share one field. No new matching engine:
    reuses the exact same vendor/amount comparators already trusted
    elsewhere in this app (helpers/entity_normalizer.py, helpers/
    relationship_builder.py's own tolerance constant).

    Returns {'package_id', 'package_name', 'document_id'} of the
    existing match, or None if this invoice isn't already grouped
    under any other package."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            'SELECT invoice_number, vendor_name, total_amount, invoice_date FROM extracted_fields WHERE document_id = %s',
            (document_id,)
        )
        this_inv = cursor.fetchone()
        if not this_inv or not this_inv['invoice_number']:
            return None

        cursor.execute(
            '''SELECT tpd.package_id, tp.package_name, ef.document_id, ef.invoice_number,
                      ef.vendor_name, ef.total_amount, ef.invoice_date
               FROM transaction_package_documents tpd
               JOIN transaction_packages tp ON tp.id = tpd.package_id
               JOIN extracted_fields ef ON ef.document_id = tpd.document_id
               WHERE tpd.document_role = 'invoice' AND tpd.document_id != %s''',
            (document_id,)
        )
        candidates = cursor.fetchall()
    finally:
        conn.close()

    this_no = _normalize_invoice_number(this_inv['invoice_number'])
    for c in candidates:
        if not c['invoice_number'] or _normalize_invoice_number(c['invoice_number']) != this_no:
            continue
        if this_inv['vendor_name'] and c['vendor_name']:
            if not is_same_company(this_inv['vendor_name'], c['vendor_name'])['match']:
                continue
        if this_inv['total_amount'] is not None and c['total_amount'] is not None:
            if abs(Decimal(str(this_inv['total_amount'])) - Decimal(str(c['total_amount']))) > AMOUNT_TOLERANCE:
                continue
        if this_inv['invoice_date'] and c['invoice_date']:
            if abs((this_inv['invoice_date'] - c['invoice_date']).days) > _INVOICE_DATE_TOLERANCE_DAYS:
                continue
        return {'package_id': c['package_id'], 'package_name': c['package_name'], 'document_id': c['document_id']}
    return None


def link_document_to_package(package_id, document_id, document_role):
    """Links an already-uploaded document (by whichever id document_
    role implies) into a package, then re-runs the existing relationship
    builder for every invoice in the package and recomputes the
    package's status. Returns (link: dict|None, error: str|None)."""
    if document_role not in VALID_ROLES:
        return None, f'document_role must be one of {VALID_ROLES}'

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute('SELECT id FROM transaction_packages WHERE id = %s', (package_id,))
        if not cursor.fetchone():
            return None, f'transaction package {package_id} does not exist'

        entity_type = _ROLE_TO_ENTITY_TYPE[document_role]
        if not _entity_exists(cursor, entity_type, document_id):
            return None, f'{document_role} {document_id} does not exist'

        cursor.execute(
            'SELECT id FROM transaction_package_documents WHERE package_id = %s AND document_role = %s AND document_id = %s',
            (package_id, document_role, document_id)
        )
        if cursor.fetchone():
            return None, 'this document is already linked to this package'

        # Bug 2 fix: one invoice = one transaction membership. A match
        # in a DIFFERENT package blocks this link rather than silently
        # creating a fragmented duplicate grouping.
        if document_role == 'invoice':
            existing = find_existing_package_for_invoice(document_id)
            if existing and existing['package_id'] != package_id:
                return None, (
                    f"This invoice already belongs to transaction package "
                    f"\"{existing['package_name']}\" (id {existing['package_id']}) via document "
                    f"{existing['document_id']}. Attach further documents to that package instead "
                    f"of creating a duplicate."
                )

        cursor.execute(
            'INSERT INTO transaction_package_documents (package_id, document_id, document_role) '
            'VALUES (%s, %s, %s) RETURNING *',
            (package_id, document_id, document_role)
        )
        link = dict(cursor.fetchone())
        conn.commit()
    finally:
        conn.close()

    _rebuild_relationships_for_package(package_id)
    _recompute_and_persist_status(package_id)
    return link, None


def get_package_documents(package_id):
    """Returns {'invoices': [...], 'purchase_orders': [...],
    'goods_receipts': [...]} — every linked document enriched with its
    own already-extracted fields (no recalculation)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            'SELECT document_id, document_role, created_at FROM transaction_package_documents '
            'WHERE package_id = %s ORDER BY created_at',
            (package_id,)
        )
        links = cursor.fetchall()

        invoice_ids = [r['document_id'] for r in links if r['document_role'] == 'invoice']
        po_ids = [r['document_id'] for r in links if r['document_role'] == 'purchase_order']
        gr_ids = [r['document_id'] for r in links if r['document_role'] == 'goods_receipt']

        invoices = []
        if invoice_ids:
            cursor.execute(
                '''SELECT d.document_id, d.file_name, d.status,
                          ef.invoice_number, ef.vendor_name, ef.total_amount, ef.currency
                   FROM documents d LEFT JOIN extracted_fields ef ON ef.document_id = d.document_id
                   WHERE d.document_id = ANY(%s)''',
                (invoice_ids,)
            )
            invoices = [dict(r) for r in cursor.fetchall()]

        purchase_orders = []
        if po_ids:
            cursor.execute(
                'SELECT po_id AS document_id, document_id AS host_document_id, file_name, po_number, vendor_name, total_amount, currency '
                'FROM purchase_orders WHERE po_id = ANY(%s)',
                (po_ids,)
            )
            purchase_orders = [dict(r) for r in cursor.fetchall()]

        goods_receipts = []
        if gr_ids:
            cursor.execute(
                'SELECT gr_id AS document_id, document_id AS host_document_id, file_name, gr_number, vendor_name, quantity '
                'FROM goods_receipts WHERE gr_id = ANY(%s)',
                (gr_ids,)
            )
            goods_receipts = [dict(r) for r in cursor.fetchall()]

        return {'invoices': invoices, 'purchase_orders': purchase_orders, 'goods_receipts': goods_receipts}
    finally:
        conn.close()


def get_relationship_preview(package_id):
    """Read-only tree view of document_relationships (Phase 1) for the
    documents in this package: PO -> its related invoices -> each
    invoice's related GRs. Reuses Phase 1's get_related_invoices/
    get_related_goods_receipts verbatim — no calculation happens here,
    exactly as required."""
    docs = get_package_documents(package_id)
    tree = []
    for po in docs['purchase_orders']:
        po_id = po['document_id']
        invoice_nodes = []
        for inv in get_related_invoices('po', po_id):
            inv_id = inv['document_id']
            grs = get_related_goods_receipts('invoice', inv_id)
            invoice_nodes.append({
                'document_id': inv_id,
                'invoice_number': inv.get('invoice_number'),
                'goods_receipts': [
                    {'document_id': gr.get('gr_id'), 'gr_number': gr.get('gr_number')} for gr in grs
                ],
            })
        tree.append({
            'document_id': po_id,
            'po_number': po.get('po_number'),
            'invoices': invoice_nodes,
        })
    return tree


def list_packages(created_by):
    """Returns every package owned by created_by, each annotated with
    document_count and a best-effort supplier (vendor) name — the first
    linked invoice's vendor_name, falling back to the first linked PO's
    vendor_name. Never fabricated: None when no vendor data exists yet."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute('SELECT * FROM transaction_packages WHERE created_by = %s ORDER BY created_at DESC', (created_by,))
        packages = [dict(r) for r in cursor.fetchall()]
        if not packages:
            return []

        package_ids = [p['id'] for p in packages]
        cursor.execute(
            'SELECT package_id, document_role, document_id FROM transaction_package_documents WHERE package_id = ANY(%s)',
            (package_ids,)
        )
        links_by_package = {}
        for row in cursor.fetchall():
            links_by_package.setdefault(row['package_id'], []).append(row)

        invoice_ids_all = [l['document_id'] for links in links_by_package.values() for l in links if l['document_role'] == 'invoice']
        po_ids_all = [l['document_id'] for links in links_by_package.values() for l in links if l['document_role'] == 'purchase_order']

        vendor_by_invoice = {}
        if invoice_ids_all:
            cursor.execute('SELECT document_id, vendor_name FROM extracted_fields WHERE document_id = ANY(%s)', (invoice_ids_all,))
            vendor_by_invoice = {r['document_id']: r['vendor_name'] for r in cursor.fetchall()}

        vendor_by_po = {}
        if po_ids_all:
            cursor.execute('SELECT po_id, vendor_name FROM purchase_orders WHERE po_id = ANY(%s)', (po_ids_all,))
            vendor_by_po = {r['po_id']: r['vendor_name'] for r in cursor.fetchall()}

        for p in packages:
            links = links_by_package.get(p['id'], [])
            p['document_count'] = len(links)
            supplier = next((vendor_by_invoice.get(l['document_id']) for l in links
                              if l['document_role'] == 'invoice' and vendor_by_invoice.get(l['document_id'])), None)
            if not supplier:
                supplier = next((vendor_by_po.get(l['document_id']) for l in links
                                  if l['document_role'] == 'purchase_order' and vendor_by_po.get(l['document_id'])), None)
            p['supplier'] = supplier

        return packages
    finally:
        conn.close()


# ============================================================
# Enterprise V3 Phase 6 — Transaction-Centric Auditor Workflow
# Integration. Every function below is a read-only lookup/aggregation
# over data Phases 1/2/5 already computed and stored — none of them
# perform matching, authenticity, or any other calculation of their
# own, and none of them call Claude/Gemini. Functions that need the
# Enterprise Matching V2 result (routes.auditor.build_comparison) live
# in routes/auditor.py instead of here, to keep this module free of a
# dependency on the routes layer (helpers/ never imports from routes/
# in this codebase — see helpers/relationship_builder.py's own
# docstring for the same established convention).
# ============================================================

def get_transaction_context_for_document(document_id, document_role='invoice'):
    """Enterprise V3 Phase 6 (STEP 2) — the transaction package context
    for ONE document, or None if it isn't part of any package (a
    legacy/standalone document — the explicit backward-compatibility
    fallback STEP 10 requires, and the ONLY thing every existing
    invoice-based consumer needs to check before using this data)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            'SELECT package_id FROM transaction_package_documents WHERE document_role = %s AND document_id = %s',
            (document_role, document_id)
        )
        row = cursor.fetchone()
        if not row:
            return None
        package_id = row['package_id']
        cursor.execute('SELECT * FROM transaction_packages WHERE id = %s', (package_id,))
        package = cursor.fetchone()
        if not package:
            return None
    finally:
        conn.close()

    docs = get_package_documents(package_id)
    return {
        'transaction_package_id': package_id,
        'package_name': package['package_name'],
        'status': package['status'],
        'documents_count': len(docs['invoices']) + len(docs['purchase_orders']) + len(docs['goods_receipts']),
        'purchase_orders': len(docs['purchase_orders']),
        'invoices': len(docs['invoices']),
        'goods_receipts': len(docs['goods_receipts']),
    }


def list_all_packages_with_documents():
    """Every transaction package system-wide (NOT scoped to a single
    Finance user, unlike list_packages() — the auditor queue needs
    global visibility across every Finance user's packages), each
    annotated with its documents by role and a best-effort supplier
    name. Enterprise V3 Phase 6 (STEP 3)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute('SELECT * FROM transaction_packages ORDER BY created_at DESC')
        packages = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    for p in packages:
        docs = get_package_documents(p['id'])
        p['documents'] = docs
        supplier = docs['invoices'][0].get('vendor_name') if docs['invoices'] else None
        if not supplier and docs['purchase_orders']:
            supplier = docs['purchase_orders'][0].get('vendor_name')
        p['supplier'] = supplier
    return packages


def list_standalone_invoices():
    """Every invoice-role document NOT linked to any transaction
    package — the backward-compatibility fallback for documents
    uploaded before Phase 5 existed, or never grouped into a package
    (STEP 10). Returns raw document/extracted_fields rows only; the
    caller (routes/auditor.py, which already imports build_comparison)
    computes matching status itself, keeping this helper free of a
    routes-layer dependency."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            '''SELECT d.document_id, d.file_name, d.status, d.uploaded_at,
                      ef.invoice_number, ef.vendor_name, ef.total_amount, ef.currency
               FROM documents d
               LEFT JOIN extracted_fields ef ON ef.document_id = d.document_id
               WHERE d.document_id NOT IN (
                   SELECT document_id FROM transaction_package_documents WHERE document_role = 'invoice'
               )
               ORDER BY d.uploaded_at DESC'''
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_transaction_authenticity_summary(package_id):
    """Enterprise V3 Phase 6 Additional Requirement — aggregates
    EXISTING authenticity_checks rows (the unmodified authenticity
    engine's own output — same table, same authenticity_status values
    it already computes elsewhere in this app) for every document in
    this package. No new authenticity calculation, no AI call: a pure
    read + count. A document that hasn't been checked yet simply isn't
    counted toward documents_checked or overall_status — it does not
    force a failure, matching the existing engine's own "not yet
    checked" semantics used everywhere else in this app."""
    docs = get_package_documents(package_id)
    role_to_type = {'invoices': 'invoice', 'purchase_orders': 'po', 'goods_receipts': 'gr'}
    all_ids = [doc['document_id'] for role_key in role_to_type for doc in docs[role_key]]
    # authenticity_checks.document_id always references documents.document_id
    # (never po_id/gr_id) — for PO/GR rows that is purchase_orders.document_id/
    # goods_receipts.document_id (the invoice they were uploaded alongside),
    # which get_package_documents() surfaces separately as host_document_id.
    auth_lookup_ids = [doc.get('host_document_id', doc['document_id']) for role_key in role_to_type for doc in docs[role_key]]

    checks_by_key = {}
    if auth_lookup_ids:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute(
                'SELECT document_id, document_type, authenticity_status, risk_level '
                'FROM authenticity_checks WHERE document_id = ANY(%s)',
                (auth_lookup_ids,)
            )
            for row in cursor.fetchall():
                checks_by_key[(row['document_type'], row['document_id'])] = dict(row)
        finally:
            conn.close()

    completed_by_role = {}
    documents_by_role = {}
    documents_checked = 0
    has_warning = False
    for role_key, doc_type in role_to_type.items():
        checked = 0
        role_docs = []
        for doc in docs[role_key]:
            check = checks_by_key.get((doc_type, doc.get('host_document_id', doc['document_id'])))
            role_docs.append({
                **doc,
                'authenticity_status': check['authenticity_status'] if check else None,
                'risk_level':          check['risk_level'] if check else None,
            })
            if check:
                checked += 1
                documents_checked += 1
                if check['authenticity_status'] == 'warning':
                    has_warning = True
        completed_by_role[role_key] = {'checked': checked, 'total': len(docs[role_key])}
        documents_by_role[role_key] = role_docs

    return {
        'documents_total': len(all_ids),
        'documents_checked': documents_checked,
        'completed_by_role': completed_by_role,
        'overall_status': 'REVIEW REQUIRED' if has_warning else 'PASS',
        # Per-document detail (STEP 8 / Additional Requirement's
        # transaction-grouped Authenticity page — "▼ Invoice A /
        # Authenticity: PASS") — additive, the aggregate fields above
        # are unaffected and remain what Record Detail's Transaction
        # Authenticity Summary card (STEP 4/5) already consumes.
        'documents': documents_by_role,
    }
