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
import psycopg2.extras
from db import get_db_connection
from helpers.document_relationships import _entity_exists, get_related_invoices, get_related_goods_receipts
from helpers.relationship_builder import build_relationships_for_invoice

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
                'SELECT po_id AS document_id, file_name, po_number, vendor_name, total_amount, currency '
                'FROM purchase_orders WHERE po_id = ANY(%s)',
                (po_ids,)
            )
            purchase_orders = [dict(r) for r in cursor.fetchall()]

        goods_receipts = []
        if gr_ids:
            cursor.execute(
                'SELECT gr_id AS document_id, file_name, gr_number, vendor_name, quantity '
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
