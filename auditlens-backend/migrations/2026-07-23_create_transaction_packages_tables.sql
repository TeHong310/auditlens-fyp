-- Enterprise V3 Phase 5: Finance Transaction Package workflow — lets
-- Finance group related AP documents (PO/Invoice/GR) into one package
-- before auditor review, instead of uploading each document with no
-- organizational context. Purely additive/organizational: does not
-- replace or duplicate document_relationships (Phase 1, the matching-
-- relevant graph) or any upload/OCR/extraction/matching logic — this
-- is bookkeeping only, describing which documents a Finance user
-- intended to group together.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_transaction_packages_table() and _ensure_
-- transaction_package_documents_table()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.

CREATE TABLE IF NOT EXISTS transaction_packages (
    id SERIAL PRIMARY KEY,
    package_name VARCHAR(200) NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(user_id),
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_transaction_packages_status
        CHECK (status IN ('draft', 'waiting_documents', 'processing', 'completed'))
);

CREATE INDEX IF NOT EXISTS idx_transaction_packages_created_by ON transaction_packages(created_by);

-- Polymorphic association, same pattern and same trade-off as Phase 1's
-- document_relationships table: purchase_orders/goods_receipts rows
-- have no independent row in `documents`, so document_id means
-- documents.document_id / purchase_orders.po_id / goods_receipts.gr_id
-- depending on document_role. A single-column SQL FOREIGN KEY can't
-- target three different tables, so referential integrity for
-- document_id is enforced at the application layer, in
-- helpers/transaction_packages.py::link_document_to_package(), which
-- validates the referenced row exists before inserting.
CREATE TABLE IF NOT EXISTS transaction_package_documents (
    id SERIAL PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES transaction_packages(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL,              -- documents.document_id / purchase_orders.po_id / goods_receipts.gr_id
    document_role VARCHAR(20) NOT NULL,        -- 'invoice' | 'purchase_order' | 'goods_receipt'
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_transaction_package_documents_role
        CHECK (document_role IN ('invoice', 'purchase_order', 'goods_receipt')),
    CONSTRAINT uq_transaction_package_documents UNIQUE (package_id, document_role, document_id)
);

CREATE INDEX IF NOT EXISTS idx_transaction_package_documents_package ON transaction_package_documents(package_id);
CREATE INDEX IF NOT EXISTS idx_transaction_package_documents_doc ON transaction_package_documents(document_role, document_id);
