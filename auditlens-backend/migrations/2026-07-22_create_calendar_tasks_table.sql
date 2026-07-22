-- Audit Workflow Calendar's ONLY new table — manual audit tasks
-- (title/description/date/assigned_to/priority) an auditor or finance
-- user creates by hand. Every OTHER calendar event type (pending review,
-- finance correction due, exception/anomaly follow-up) is computed on the
-- fly from existing tables (documents, send_back_cycles, review_records-
-- backed exception classification, anomalies) — nothing about those is
-- stored here or duplicated.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_calendar_tasks_table()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
CREATE TABLE IF NOT EXISTS calendar_tasks (
    task_id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    event_date DATE NOT NULL,
    assigned_to INTEGER REFERENCES users(user_id),
    priority VARCHAR(10) NOT NULL DEFAULT 'normal',   -- normal | medium | high (same vocabulary as send_back_cycles.priority)
    status VARCHAR(10) NOT NULL DEFAULT 'open',        -- open | done
    created_by INTEGER NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_calendar_tasks_date ON calendar_tasks(event_date);
CREATE INDEX IF NOT EXISTS idx_calendar_tasks_assigned ON calendar_tasks(assigned_to);
