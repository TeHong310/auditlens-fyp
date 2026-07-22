-- Enterprise V3 Phase 2: additive columns on document_relationships so
-- the deterministic relationship builder (helpers/relationship_builder.py)
-- can safely coexist with Phase 1's manual API (routes/document_
-- relationships.py POST /documents/relationships) without ever
-- overwriting a human-confirmed link.
--
-- relationship_source: 'manual' | 'auto'. Existing/Phase-1-created rows
-- default to 'manual' (correct — they were created through the POST API,
-- i.e. a human/client action), so nothing about Phase 1 data changes
-- meaning. Rows created by the Phase 2 builder are tagged 'auto', and
-- are the ONLY rows the builder is allowed to update on a rebuild —
-- see helpers/document_relationships.py::upsert_relationship().
--
-- matching_reason: free-text explanation of why the builder linked two
-- documents (e.g. "PO reference matches PO number; vendor matches").
-- NULL for manually-created (Phase 1) relationships, since there is no
-- deterministic reason to record for a human-initiated link.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_document_relationships_v2_columns()) since this repo
-- has no migration runner and Render never gets a .sql file run against
-- it otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
ALTER TABLE document_relationships
    ADD COLUMN IF NOT EXISTS relationship_source VARCHAR(10) NOT NULL DEFAULT 'manual';

ALTER TABLE document_relationships
    ADD COLUMN IF NOT EXISTS matching_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_document_relationships_source'
    ) THEN
        ALTER TABLE document_relationships
            ADD CONSTRAINT chk_document_relationships_source
            CHECK (relationship_source IN ('manual', 'auto'));
    END IF;
END $$;
