-- Line-item level 3-way audit matching: every row of a document's goods/
-- services table (not just the first line item — the prior single-value
-- item_description/quantity limitation meant an invoice with 3 items only
-- ever compared item #1). document_id points at the SAME invoice
-- document_id used across purchase_orders/goods_receipts (not po_id/gr_id).
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_document_line_items_table()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
CREATE TABLE IF NOT EXISTS document_line_items (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    document_type VARCHAR(10) NOT NULL,
    line_no INTEGER,
    item_code VARCHAR(100),
    description TEXT,
    quantity NUMERIC,
    unit_price NUMERIC,
    amount NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_document_line_items_doc
ON document_line_items (document_id, document_type);
