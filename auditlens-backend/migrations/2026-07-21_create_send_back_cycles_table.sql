-- Auditor <-> Finance send-back/correction workflow: one row per send-back
-- CYCLE, not one mutable reason that gets overwritten every time — a record
-- sent back multiple times keeps its full history, since each new cycle is
-- a new row (never an UPDATE over a prior cycle's reason/response).
--
-- documents.status keeps its EXISTING values (returned/resubmitted/
-- approved/under_review/...) completely unchanged, so no existing
-- dashboard count, filter, or report that reads documents.status is
-- affected. cycle_status here is a separate, finer-grained sub-state
-- (action_required -> resubmitted -> resolved) used only by the richer
-- send-back UI.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_send_back_cycles_table()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
CREATE TABLE IF NOT EXISTS send_back_cycles (
    cycle_id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    cycle_number INTEGER NOT NULL,

    -- Auditor's structured send-back request (Feature 1)
    return_reason_category VARCHAR(50) NOT NULL,
    reason_other_note TEXT,
    auditor_instruction TEXT NOT NULL,
    required_actions JSONB NOT NULL DEFAULT '[]',
    required_action_other_note TEXT,
    priority VARCHAR(10) NOT NULL DEFAULT 'normal',
    response_due_date DATE,
    sent_back_by INTEGER NOT NULL REFERENCES users(user_id),
    sent_back_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Finance's response (Feature 3)
    finance_response TEXT,
    finance_responded_by INTEGER REFERENCES users(user_id),
    finance_responded_at TIMESTAMP,
    resubmitted_by INTEGER REFERENCES users(user_id),
    resubmitted_at TIMESTAMP,

    -- Cycle lifecycle
    cycle_status VARCHAR(20) NOT NULL DEFAULT 'action_required',  -- action_required | resubmitted | resolved
    resolution VARCHAR(20),                                       -- approved | returned_again (set once resolved)
    resolved_at TIMESTAMP,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE(document_id, cycle_number)
);

CREATE INDEX IF NOT EXISTS idx_send_back_cycles_document ON send_back_cycles(document_id);
CREATE INDEX IF NOT EXISTS idx_send_back_cycles_status ON send_back_cycles(cycle_status);
