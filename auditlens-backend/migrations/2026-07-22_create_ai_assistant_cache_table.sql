-- AI Audit Assistant response cache (helpers/ai_assistant.py,
-- routes/ai_assistant.py) — same DB-backed pattern as claude_extraction_
-- cache / gemini_extraction_cache / authenticity_result_cache (Gunicorn
-- workers don't share memory, Render's free-tier disk is ephemeral), so
-- an unchanged case + the same button/question never re-spends a
-- Claude/Gemini call. context_hash already encodes the full case data +
-- question, so this needs no separate invalidation logic: any change to
-- the underlying case (new matching result, new authenticity/anomaly
-- finding, a different question) naturally produces a new hash.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_ai_assistant_cache_table()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
CREATE TABLE IF NOT EXISTS ai_assistant_cache (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    action VARCHAR(30) NOT NULL,   -- explain_exception | explain_risk | generate_remark | prepare_send_back | ask
    context_hash VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(document_id, action, context_hash)
);

CREATE INDEX IF NOT EXISTS idx_ai_assistant_cache_document ON ai_assistant_cache(document_id);
