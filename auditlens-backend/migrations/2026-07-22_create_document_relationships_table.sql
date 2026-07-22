-- Enterprise V3 Phase 1: flexible many-to-many document relationships
-- (PO <-> Invoice, PO <-> GR, Invoice <-> GR), additive to the existing
-- one-to-one attachment model (purchase_orders.document_id /
-- goods_receipts.document_id). Nothing here is read by the existing
-- matching engine (routes/auditor.py::_build_comparison) — this table
-- is purely new and unread by any existing page in this phase.
--
-- Polymorphic association: purchase_orders/goods_receipts rows have no
-- independent row in `documents` (they only carry a document_id FK to
-- whichever invoice they were originally uploaded under), so a single
-- parent_document_id/child_document_id FK to `documents` cannot express
-- PO or GR endpoints. Instead each side is tagged with its own type
-- ('invoice' | 'po' | 'gr') plus an id meaning documents.document_id /
-- purchase_orders.po_id / goods_receipts.gr_id respectively.
--
-- TRADE-OFF: because parent_id/child_id can reference three different
-- tables depending on type, a single-column SQL FOREIGN KEY is not
-- possible (Postgres FKs target exactly one table). Referential
-- integrity for parent_id/child_id is therefore enforced at the
-- application layer, in helpers/document_relationships.py::
-- create_relationship(), which validates the referenced row exists
-- before inserting. The CHECK constraints below still enforce that
-- type values are valid and that relationship_type is consistent with
-- its parent/child type pair.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_document_relationships_table()) since this repo has
-- no migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
CREATE TABLE IF NOT EXISTS document_relationships (
    id SERIAL PRIMARY KEY,
    parent_type VARCHAR(10) NOT NULL,          -- 'invoice' | 'po' | 'gr'
    parent_id INTEGER NOT NULL,                -- documents.document_id / purchase_orders.po_id / goods_receipts.gr_id
    child_type VARCHAR(10) NOT NULL,           -- 'invoice' | 'po' | 'gr'
    child_id INTEGER NOT NULL,                 -- documents.document_id / purchase_orders.po_id / goods_receipts.gr_id
    relationship_type VARCHAR(20) NOT NULL,    -- 'po_invoice' | 'po_gr' | 'invoice_gr'
    matched_quantity NUMERIC,
    matched_amount NUMERIC,
    confidence_score NUMERIC(5, 2),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_document_relationships_types
        CHECK (parent_type IN ('invoice', 'po', 'gr') AND child_type IN ('invoice', 'po', 'gr')),
    CONSTRAINT chk_document_relationships_type_pair
        CHECK (
            (relationship_type = 'po_invoice' AND parent_type = 'po' AND child_type = 'invoice') OR
            (relationship_type = 'po_gr' AND parent_type = 'po' AND child_type = 'gr') OR
            (relationship_type = 'invoice_gr' AND parent_type = 'invoice' AND child_type = 'gr')
        ),
    CONSTRAINT chk_document_relationships_no_self_link
        CHECK (NOT (parent_type = child_type AND parent_id = child_id)),
    CONSTRAINT uq_document_relationships UNIQUE (parent_type, parent_id, child_type, child_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS idx_document_relationships_parent ON document_relationships(parent_type, parent_id);
CREATE INDEX IF NOT EXISTS idx_document_relationships_child ON document_relationships(child_type, child_id);
