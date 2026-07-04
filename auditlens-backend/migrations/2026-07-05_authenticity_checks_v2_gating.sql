ALTER TABLE authenticity_checks
  ADD COLUMN IF NOT EXISTS has_signature BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS document_type VARCHAR(20),
  ADD COLUMN IF NOT EXISTS upload_source VARCHAR(30),
  ADD COLUMN IF NOT EXISTS authenticity_status VARCHAR(20) NOT NULL DEFAULT 'passed';

CREATE INDEX IF NOT EXISTS idx_authenticity_status ON authenticity_checks(authenticity_status);

-- Backfill (no-op in practice since the column above defaults to 'passed',
-- kept for parity with the original request).
UPDATE authenticity_checks SET authenticity_status = 'passed' WHERE authenticity_status IS NULL;

-- DEVIATION FROM WHAT WAS GIVEN: the original schema had UNIQUE(document_id),
-- one authenticity row per parent document. But invoice/PO/GR uploads for the
-- same record all share the SAME documents.document_id (PO/GR uploads don't
-- get their own `documents` row — they attach to the invoice's document_id).
-- Since this feature now needs one check per document_type per document_id
-- (an invoice check, a PO check, and a GR check on the same document_id must
-- coexist), the UNIQUE constraint is widened to (document_id, document_type)
-- instead. Approved by the user after this conflict was flagged.
ALTER TABLE authenticity_checks DROP CONSTRAINT IF EXISTS authenticity_checks_document_id_key;
ALTER TABLE authenticity_checks ADD CONSTRAINT authenticity_checks_document_id_doctype_key UNIQUE (document_id, document_type);
