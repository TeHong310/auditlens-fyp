CREATE TABLE authenticity_checks (
    check_id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    has_company_chop BOOLEAN NOT NULL DEFAULT FALSE,
    has_company_logo BOOLEAN NOT NULL DEFAULT FALSE,
    has_company_name BOOLEAN NOT NULL DEFAULT FALSE,
    ai_notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(document_id)
);

CREATE INDEX idx_authenticity_document_id ON authenticity_checks(document_id);
