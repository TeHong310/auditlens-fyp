"""Enterprise V3 Phase 1 relationship service layer — flexible many-to-many
links between invoices/POs/GRs, stored in document_relationships (see
app.py's _ensure_document_relationships_table() and the matching
migrations/*.sql file). Purely additive: the existing one-to-one attachment
model (purchase_orders.document_id / goods_receipts.document_id) and the
matching engine (routes/auditor.py::_build_comparison) are untouched and
keep working exactly as before.

Compatibility layer (STEP 4): the typed getters below (get_related_
purchase_orders / get_related_goods_receipts / get_related_invoices) check
document_relationships first; when a document has no explicit relationship
rows yet (i.e. it predates this feature, or was never linked through the
new API), they fall back to the legacy one-to-one attachment so old data
keeps returning sensible results with no backfill migration required.

parent_id/child_id are polymorphic (mean documents.document_id /
purchase_orders.po_id / goods_receipts.gr_id depending on type), so they
cannot carry a single-table SQL FOREIGN KEY. create_relationship() below is
the application-layer enforcement point for referential integrity.
"""
import psycopg2.extras
from db import get_db_connection

VALID_TYPES = ('invoice', 'po', 'gr')

RELATIONSHIP_TYPE_PAIRS = {
    'po_invoice': ('po', 'invoice'),
    'po_gr': ('po', 'gr'),
    'invoice_gr': ('invoice', 'gr'),
}

_ENTITY_TABLES = {
    'invoice': ('documents', 'document_id'),
    'po': ('purchase_orders', 'po_id'),
    'gr': ('goods_receipts', 'gr_id'),
}


def _entity_exists(cursor, doc_type, doc_id):
    table, pk = _ENTITY_TABLES[doc_type]
    cursor.execute(f'SELECT 1 FROM {table} WHERE {pk} = %s', (doc_id,))
    return cursor.fetchone() is not None


def create_relationship(parent_type, parent_id, child_type, child_id, relationship_type,
                         matched_quantity=None, matched_amount=None, confidence_score=None,
                         relationship_source='manual', matching_reason=None):
    """Creates a new document_relationships row. Returns (relationship: dict|
    None, error: str|None) — relationship is None whenever error is set.
    relationship_source defaults to 'manual' (Phase 1's POST /documents/
    relationships never passes it, so every existing call site keeps
    creating 'manual' rows unchanged) — helpers/relationship_builder.py
    (Phase 2) is the only caller that passes 'auto'."""
    if relationship_type not in RELATIONSHIP_TYPE_PAIRS:
        return None, f'relationship_type must be one of {tuple(RELATIONSHIP_TYPE_PAIRS)}'
    expected_parent_type, expected_child_type = RELATIONSHIP_TYPE_PAIRS[relationship_type]
    if parent_type != expected_parent_type or child_type != expected_child_type:
        return None, (f'relationship_type "{relationship_type}" requires '
                       f'parent_type="{expected_parent_type}" and child_type="{expected_child_type}"')
    if parent_type == child_type and parent_id == child_id:
        return None, 'a document cannot be related to itself'

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if not _entity_exists(cursor, parent_type, parent_id):
            return None, f'{parent_type} {parent_id} does not exist'
        if not _entity_exists(cursor, child_type, child_id):
            return None, f'{child_type} {child_id} does not exist'

        cursor.execute(
            '''SELECT 1 FROM document_relationships
               WHERE parent_type = %s AND parent_id = %s AND child_type = %s AND child_id = %s
                 AND relationship_type = %s''',
            (parent_type, parent_id, child_type, child_id, relationship_type)
        )
        if cursor.fetchone():
            return None, 'this relationship already exists'

        cursor.execute(
            '''INSERT INTO document_relationships
               (parent_type, parent_id, child_type, child_id, relationship_type,
                matched_quantity, matched_amount, confidence_score,
                relationship_source, matching_reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, parent_type, parent_id, child_type, child_id, relationship_type,
                         matched_quantity, matched_amount, confidence_score,
                         relationship_source, matching_reason, created_at, updated_at''',
            (parent_type, parent_id, child_type, child_id, relationship_type,
             matched_quantity, matched_amount, confidence_score,
             relationship_source, matching_reason)
        )
        row = dict(cursor.fetchone())
        conn.commit()
        return row, None
    finally:
        conn.close()


def upsert_relationship(parent_type, parent_id, child_type, child_id, relationship_type,
                         matched_quantity=None, matched_amount=None, confidence_score=None,
                         matching_reason=None):
    """Idempotent insert-or-update for the deterministic relationship
    builder (helpers/relationship_builder.py) ONLY — always writes
    relationship_source='auto'. If a relationship already exists for this
    exact (parent, child, relationship_type):
      - relationship_source='manual' (Phase 1 API / a human) -> left
        untouched, returned as-is with skipped=True. Auto-generated
        relationships must NEVER overwrite a manually confirmed one.
      - relationship_source='auto' (a previous builder run) -> its
        matched_quantity/matched_amount/confidence_score/matching_reason/
        updated_at are refreshed, so re-running the builder after new
        documents arrive keeps allocations current without ever
        duplicating the row (the same idempotency guarantee create_
        relationship's UNIQUE-constraint check already gives Phase 1).
    Returns (relationship: dict, skipped: bool, error: str|None)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            '''SELECT * FROM document_relationships
               WHERE parent_type = %s AND parent_id = %s AND child_type = %s AND child_id = %s
                 AND relationship_type = %s''',
            (parent_type, parent_id, child_type, child_id, relationship_type)
        )
        existing = cursor.fetchone()

        if existing and existing['relationship_source'] == 'manual':
            return dict(existing), True, None

        if existing:
            cursor.execute(
                '''UPDATE document_relationships
                   SET matched_quantity = %s, matched_amount = %s, confidence_score = %s,
                       matching_reason = %s, updated_at = NOW()
                   WHERE id = %s
                   RETURNING id, parent_type, parent_id, child_type, child_id, relationship_type,
                             matched_quantity, matched_amount, confidence_score,
                             relationship_source, matching_reason, created_at, updated_at''',
                (matched_quantity, matched_amount, confidence_score, matching_reason, existing['id'])
            )
            row = dict(cursor.fetchone())
            conn.commit()
            return row, False, None
    finally:
        conn.close()

    relationship, error = create_relationship(
        parent_type, parent_id, child_type, child_id, relationship_type,
        matched_quantity=matched_quantity, matched_amount=matched_amount,
        confidence_score=confidence_score, relationship_source='auto', matching_reason=matching_reason,
    )
    return relationship, False, error


def delete_relationship(relationship_id):
    """Deletes a document_relationships row by id. Returns True if a row was
    deleted, False if relationship_id didn't exist."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM document_relationships WHERE id = %s', (relationship_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def get_relationship_by_id(relationship_id):
    """Returns a single document_relationships row as a dict, or None."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute('SELECT * FROM document_relationships WHERE id = %s', (relationship_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_related_documents(doc_type, doc_id):
    """Returns every document_relationships row where (doc_type, doc_id) is
    either the parent or the child, each tagged with the OTHER side's
    type/id plus the relationship's own fields. The base function the three
    typed getters below filter and enrich."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            '''SELECT id, parent_type, parent_id, child_type, child_id, relationship_type,
                      matched_quantity, matched_amount, confidence_score, created_at, updated_at
               FROM document_relationships
               WHERE (parent_type = %s AND parent_id = %s) OR (child_type = %s AND child_id = %s)
               ORDER BY created_at''',
            (doc_type, doc_id, doc_type, doc_id)
        )
        rows = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    results = []
    for row in rows:
        is_parent = row['parent_type'] == doc_type and row['parent_id'] == doc_id
        other_type = row['child_type'] if is_parent else row['parent_type']
        other_id = row['child_id'] if is_parent else row['parent_id']
        results.append({
            'relationship_id': row['id'],
            'other_type': other_type,
            'other_id': other_id,
            'relationship_type': row['relationship_type'],
            'matched_quantity': row['matched_quantity'],
            'matched_amount': row['matched_amount'],
            'confidence_score': row['confidence_score'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
        })
    return results


def get_related_purchase_orders(doc_type, doc_id):
    """Enriched PO summaries related to (doc_type, doc_id). Falls back to
    the legacy one-to-one purchase_orders.document_id attachment when this
    invoice has no explicit document_relationships rows yet."""
    related = [r for r in get_related_documents(doc_type, doc_id) if r['other_type'] == 'po']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if related:
            po_ids = [r['other_id'] for r in related]
            cursor.execute('SELECT * FROM purchase_orders WHERE po_id = ANY(%s)', (po_ids,))
            # file_bytes (BYTEA) comes back from psycopg2 as a memoryview,
            # which jsonify() can't serialize — this dict is returned
            # straight into GET /auditor/record/<id>/comparison's
            # related_purchase_orders field. The file endpoint
            # (GET /documents/po/<po_id>/file) already serves the actual
            # bytes; this is metadata-only, same convention already used
            # by routes/documents.py's get_po_list/get_gr_list.
            pos_by_id = {row['po_id']: {k: v for k, v in dict(row).items() if k != 'file_bytes'} for row in cursor.fetchall()}
            return [{**pos_by_id[r['other_id']], 'relationship': r}
                    for r in related if r['other_id'] in pos_by_id]

        if doc_type != 'invoice':
            return []
        # Legacy fallback: mirrors _build_comparison()'s own PO selection
        # (routes/auditor.py) so old, un-migrated data keeps working.
        cursor.execute(
            'SELECT * FROM purchase_orders WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (doc_id,)
        )
        row = cursor.fetchone()
        if not row:
            return []
        row = {k: v for k, v in dict(row).items() if k != 'file_bytes'}
        return [{**row, 'relationship': None}]
    finally:
        conn.close()


def get_related_goods_receipts(doc_type, doc_id):
    """Enriched GR summaries related to (doc_type, doc_id). Falls back to
    the legacy one-to-one goods_receipts.document_id attachment when this
    invoice has no explicit document_relationships rows yet."""
    related = [r for r in get_related_documents(doc_type, doc_id) if r['other_type'] == 'gr']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if related:
            gr_ids = [r['other_id'] for r in related]
            cursor.execute('SELECT * FROM goods_receipts WHERE gr_id = ANY(%s)', (gr_ids,))
            # See get_related_purchase_orders' comment above — file_bytes
            # is BYTEA (memoryview), not JSON-serializable, and the file
            # endpoint already serves it separately.
            grs_by_id = {row['gr_id']: {k: v for k, v in dict(row).items() if k != 'file_bytes'} for row in cursor.fetchall()}
            return [{**grs_by_id[r['other_id']], 'relationship': r}
                    for r in related if r['other_id'] in grs_by_id]

        if doc_type != 'invoice':
            return []
        cursor.execute(
            'SELECT * FROM goods_receipts WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (doc_id,)
        )
        row = cursor.fetchone()
        if not row:
            return []
        row = {k: v for k, v in dict(row).items() if k != 'file_bytes'}
        return [{**row, 'relationship': None}]
    finally:
        conn.close()


def get_related_invoices(doc_type, doc_id):
    """Enriched invoice summaries related to (doc_type, doc_id). Falls back
    to the legacy attachment (the invoice a PO/GR was originally uploaded
    under) when no explicit relationships exist yet."""
    related = [r for r in get_related_documents(doc_type, doc_id) if r['other_type'] == 'invoice']
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if related:
            invoice_ids = [r['other_id'] for r in related]
            cursor.execute(
                '''SELECT d.*, ef.invoice_number, ef.vendor_name, ef.total_amount, ef.currency
                   FROM documents d
                   LEFT JOIN extracted_fields ef ON ef.document_id = d.document_id
                   WHERE d.document_id = ANY(%s)''',
                (invoice_ids,)
            )
            # See get_related_purchase_orders' comment above — d.* pulls
            # in documents.file_bytes (BYTEA/memoryview), not JSON-
            # serializable and already served separately by the file
            # endpoint.
            invoices_by_id = {row['document_id']: {k: v for k, v in dict(row).items() if k != 'file_bytes'} for row in cursor.fetchall()}
            return [{**invoices_by_id[r['other_id']], 'relationship': r}
                    for r in related if r['other_id'] in invoices_by_id]

        if doc_type not in ('po', 'gr'):
            return []
        table, pk = _ENTITY_TABLES[doc_type]
        cursor.execute(f'SELECT document_id FROM {table} WHERE {pk} = %s', (doc_id,))
        row = cursor.fetchone()
        if not row or not row['document_id']:
            return []
        cursor.execute(
            '''SELECT d.*, ef.invoice_number, ef.vendor_name, ef.total_amount, ef.currency
               FROM documents d
               LEFT JOIN extracted_fields ef ON ef.document_id = d.document_id
               WHERE d.document_id = %s''',
            (row['document_id'],)
        )
        inv_row = cursor.fetchone()
        if not inv_row:
            return []
        inv_row = {k: v for k, v in dict(inv_row).items() if k != 'file_bytes'}
        return [{**inv_row, 'relationship': None}]
    finally:
        conn.close()
