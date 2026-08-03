from flask import Flask, jsonify
from flask.json.provider import DefaultJSONProvider
from datetime import datetime, date
from flask_jwt_extended import JWTManager
from flask_cors import CORS
import bcrypt
from config import Config
from db import get_db_connection
from helpers.time_format import to_utc_iso
from routes.auth import auth_bp
from routes.documents import documents_bp
from routes.matching import matching_bp
from routes.reviews import reviews_bp
from routes.admin import admin_bp
from routes.ocr_review import ocr_review_bp
from routes.auditor import auditor_bp
from routes.anomalies import anomalies_bp
from routes.authenticity import authenticity_bp
from routes.calendar import calendar_bp
from routes.ai_assistant import ai_assistant_bp
from routes.document_relationships import document_relationships_bp
from routes.transaction_packages import transaction_packages_bp


def _ensure_anomalies_table():
    """Auto-create the anomalies table on startup so Render (which has
    no migration runner) doesn't need the .sql file run manually. Safe
    to run on every boot: CREATE ... IF NOT EXISTS is a no-op once the
    table exists. Failure is logged, not raised, so a transient DB
    hiccup at cold start doesn't take down the whole app."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS anomalies (
                anomaly_id SERIAL PRIMARY KEY,
                invoice_document_id INT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                anomaly_type VARCHAR(50) NOT NULL,
                severity VARCHAR(20) NOT NULL,
                detected_pattern JSONB,
                ai_explanation TEXT,
                ai_recommendation TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                reviewed_by INT REFERENCES users(user_id),
                reviewed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_anomalies_status ON anomalies(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_anomalies_invoice ON anomalies(invoice_document_id)')
        conn.commit()
        conn.close()
        print('Anomaly table ready')
    except Exception as e:
        print(f'WARNING: could not ensure anomalies table exists: {type(e).__name__}: {e}')


def _ensure_anomaly_detection_runs_table():
    """Same auto-create-on-startup pattern as _ensure_anomalies_table()
    above. The `anomalies` table alone can't answer "when was detection
    last run" or "how many times has it run" for a document that was
    screened and found clean — a clean result inserts no row there. One
    row is logged per helpers/anomaly_detector.py::run_anomaly_detection()
    call (see that function's own comment), regardless of outcome, so
    the Anomaly Detection page's "Last Analysed" summary field reflects
    reality instead of being inferred from unrelated timestamps."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS anomaly_detection_runs (
                run_id SERIAL PRIMARY KEY,
                invoice_document_id INT REFERENCES documents(document_id) ON DELETE CASCADE,
                anomalies_found INTEGER NOT NULL DEFAULT 0,
                run_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_anomaly_detection_runs_run_at ON anomaly_detection_runs(run_at)')
        conn.commit()
        conn.close()
        print('Anomaly detection runs table ready')
    except Exception as e:
        print(f'WARNING: could not ensure anomaly_detection_runs table exists: {type(e).__name__}: {e}')


def _ensure_authenticity_checks_table():
    """Same auto-create-on-startup pattern as _ensure_anomalies_table()
    above, for the same reason: no migration runner exists in this repo,
    so Render never gets a .sql file run against it unless startup does
    it itself."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS authenticity_checks (
                check_id SERIAL PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                has_company_chop BOOLEAN NOT NULL DEFAULT FALSE,
                has_company_logo BOOLEAN NOT NULL DEFAULT FALSE,
                has_company_name BOOLEAN NOT NULL DEFAULT FALSE,
                ai_notes TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(document_id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_authenticity_document_id ON authenticity_checks(document_id)')
        conn.commit()
        conn.close()
        print('Authenticity checks table ready')
    except Exception as e:
        print(f'WARNING: could not ensure authenticity_checks table exists: {type(e).__name__}: {e}')


def _ensure_authenticity_v2_columns():
    """Layer 6 v2 (soft-gate rules + upload source): adds the new columns
    to whatever authenticity_checks table _ensure_authenticity_checks_table()
    just ensured exists, and widens the UNIQUE constraint from
    (document_id) to (document_id, document_type) so an invoice/PO/GR check
    on the same parent document_id can coexist instead of colliding. Safe
    to run on every boot: every step is idempotent (IF NOT EXISTS / guarded
    by a pg_constraint lookup)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            ALTER TABLE authenticity_checks
              ADD COLUMN IF NOT EXISTS has_signature BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS document_type VARCHAR(20),
              ADD COLUMN IF NOT EXISTS upload_source VARCHAR(30),
              ADD COLUMN IF NOT EXISTS authenticity_status VARCHAR(20) NOT NULL DEFAULT 'passed'
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_authenticity_status ON authenticity_checks(authenticity_status)')
        cursor.execute('''
            SELECT 1 FROM pg_constraint WHERE conname = 'authenticity_checks_document_id_key'
        ''')
        if cursor.fetchone():
            cursor.execute('ALTER TABLE authenticity_checks DROP CONSTRAINT authenticity_checks_document_id_key')
        cursor.execute('''
            SELECT 1 FROM pg_constraint WHERE conname = 'authenticity_checks_document_id_doctype_key'
        ''')
        if not cursor.fetchone():
            cursor.execute('''
                ALTER TABLE authenticity_checks
                ADD CONSTRAINT authenticity_checks_document_id_doctype_key UNIQUE (document_id, document_type)
            ''')
        conn.commit()
        conn.close()
        print('Authenticity checks v2 columns ready')
    except Exception as e:
        print(f'WARNING: could not migrate authenticity_checks to v2: {type(e).__name__}: {e}')


def _ensure_authenticity_v3_columns():
    """AI-powered authentication upgrade: adds columns for the new
    Claude-first-then-Gemini-fallback visual verification engine
    (helpers/authenticity_check.py) alongside the existing v2 columns —
    purely additive, nothing existing is touched. Same auto-create-on-
    startup pattern as every other _ensure_ function (no migration
    runner in this repo)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            ALTER TABLE authenticity_checks
              ADD COLUMN IF NOT EXISTS ai_engine_used VARCHAR(10),
              ADD COLUMN IF NOT EXISTS ai_visual_result JSONB,
              ADD COLUMN IF NOT EXISTS document_consistency JSONB,
              ADD COLUMN IF NOT EXISTS risk_level VARCHAR(10),
              ADD COLUMN IF NOT EXISTS boxes JSONB
        ''')
        conn.commit()
        conn.close()
        print('Authenticity checks v3 columns ready')
    except Exception as e:
        print(f'WARNING: could not migrate authenticity_checks to v3: {type(e).__name__}: {e}')


def _ensure_authenticity_evidence_corrections_table():
    """Human-in-the-Loop evidence-location editing (v10 spec). A
    NEW table, not new columns on authenticity_checks — deliberately
    separate from authenticity_checks.boxes (the AI-only region list),
    since a recheck REPLACES that column wholesale (fresh Claude/Vision/
    OpenCV run) while an auditor's correction must survive every
    recheck untouched. Being a separate table also gives each
    correction its own real row-level audit metadata (corrected_by/
    corrected_at) rather than bolting that onto a JSONB blob.

    One row per (document_id, document_type, evidence_type) — the
    UNIQUE constraint below is exactly what makes "the latest correction
    for this evidence type always wins" an upsert instead of an
    ever-growing history table. original_ai_box is a JSONB SNAPSHOT of
    whatever AI box existed at the moment of correction (not a live
    reference) — the AI box itself may be replaced entirely by a later
    recheck, so a snapshot is the only stable "what was this corrected
    FROM" record. source='auditor_deleted' rows (x/y/width/height all
    NULL) record that the auditor explicitly removed a location, distinct
    from "no correction has ever been made" (no row at all) — this is
    what lets "Needs Location" and "no correction yet" behave
    differently in the frontend. Same auto-create-on-startup pattern as
    every other _ensure_ function (no migration runner in this repo).

    Column-for-column, this MUST stay in sync with every query in
    helpers/evidence_corrections.py: correction_id, document_id,
    document_type, evidence_type, page, x, y, width, height, source,
    original_ai_box, corrected_by, corrected_at, created_at, and the
    UNIQUE(document_id, document_type, evidence_type) constraint that
    every INSERT ... ON CONFLICT (document_id, document_type,
    evidence_type) in that module depends on to upsert correctly."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS authenticity_evidence_corrections (
                correction_id SERIAL PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                document_type VARCHAR(20) NOT NULL,
                evidence_type VARCHAR(50) NOT NULL,
                page INTEGER NOT NULL DEFAULT 1,
                x NUMERIC,
                y NUMERIC,
                width NUMERIC,
                height NUMERIC,
                source VARCHAR(20) NOT NULL,
                original_ai_box JSONB,
                corrected_by INTEGER NOT NULL REFERENCES users(user_id),
                corrected_at TIMESTAMP NOT NULL DEFAULT NOW(),
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE(document_id, document_type, evidence_type)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_evidence_corrections_document
            ON authenticity_evidence_corrections(document_id, document_type)
        ''')
        conn.commit()
        print('authenticity_evidence_corrections table ready')
    except Exception as e:
        # Never swallowed: the real driver-level error (Postgres error
        # code + message, when this is a psycopg2 error) is logged in
        # full, at ERROR severity — the prior version of this handler
        # printed only str(e) under a "WARNING" label, exactly the kind
        # of thing that goes unnoticed in a log stream and leaves the
        # table permanently missing (nothing here retries) until the
        # next deploy/restart happens to succeed.
        pgcode = getattr(e, 'pgcode', None)
        pgerror = getattr(e, 'pgerror', None)
        print(f'ERROR: could not create authenticity_evidence_corrections table — '
              f'{type(e).__name__}: {e} | pgcode={pgcode!r} pgerror={pgerror!r}')
    finally:
        # A connection obtained above must always be returned to the
        # pool, success or failure — otherwise a migration that fails on
        # every boot (e.g. a persistent permissions/connectivity issue)
        # leaks one pooled connection per restart until the pool itself
        # is exhausted, breaking every OTHER route that needs a
        # connection too, not just this table's own queries.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _ensure_file_bytes_columns():
    """Render's free tier filesystem is ephemeral (wiped on every redeploy/
    restart) — uploaded files can no longer live only on local disk.
    Adds file_bytes (BYTEA) / file_mime columns to documents/
    purchase_orders/goods_receipts so the original file survives
    restarts. Same auto-create-on-startup pattern as the other _ensure_
    functions above, for the same reason (no migration runner in this
    repo). Safe to run on every boot: ADD COLUMN IF NOT EXISTS is a
    no-op once the columns exist."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for table in ('documents', 'purchase_orders', 'goods_receipts'):
            cursor.execute(f'''
                ALTER TABLE {table}
                  ADD COLUMN IF NOT EXISTS file_bytes BYTEA,
                  ADD COLUMN IF NOT EXISTS file_mime VARCHAR(100)
            ''')
        conn.commit()
        conn.close()
        print('File bytes columns ready')
    except Exception as e:
        print(f'WARNING: could not add file_bytes columns: {type(e).__name__}: {e}')


def _ensure_document_hash_columns():
    """Audit Evidence Passport — document integrity. Adds sha256_baseline
    (the SHA-256 hex digest captured at upload time) to documents/
    purchase_orders/goods_receipts so a later Passport view can recompute
    the hash from the stored file_bytes and compare against this baseline.
    Same auto-create-on-startup pattern as the other _ensure_ functions
    above (no migration runner in this repo). Existing rows keep this
    NULL — never backfilled, since a baseline recorded after the fact
    wouldn't actually prove anything."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for table in ('documents', 'purchase_orders', 'goods_receipts'):
            cursor.execute(f'''
                ALTER TABLE {table}
                  ADD COLUMN IF NOT EXISTS sha256_baseline VARCHAR(64)
            ''')
        conn.commit()
        conn.close()
        print('Document hash baseline columns ready')
    except Exception as e:
        print(f'WARNING: could not add sha256_baseline columns: {type(e).__name__}: {e}')


def _ensure_3way_comparison_columns():
    """3-way audit Field Comparison table redesign: PO Ref, Item/
    Description, Quantity are regex-extracted (no Gemini call) and
    stored on extracted_fields (invoice)/purchase_orders/goods_receipts.
    Same auto-create-on-startup pattern as the other _ensure_ functions
    above, for the same reason (no migration runner in this repo)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            ALTER TABLE extracted_fields
              ADD COLUMN IF NOT EXISTS po_reference VARCHAR(100),
              ADD COLUMN IF NOT EXISTS item_description TEXT,
              ADD COLUMN IF NOT EXISTS quantity NUMERIC
        ''')
        cursor.execute('''
            ALTER TABLE purchase_orders
              ADD COLUMN IF NOT EXISTS item_description TEXT,
              ADD COLUMN IF NOT EXISTS quantity NUMERIC
        ''')
        cursor.execute('''
            ALTER TABLE goods_receipts
              ADD COLUMN IF NOT EXISTS po_reference VARCHAR(100),
              ADD COLUMN IF NOT EXISTS item_description TEXT,
              ADD COLUMN IF NOT EXISTS quantity NUMERIC
        ''')
        conn.commit()
        conn.close()
        print('3-way comparison columns ready')
    except Exception as e:
        print(f'WARNING: could not add 3-way comparison columns: {type(e).__name__}: {e}')


def _ensure_invoice_currency_column():
    """Gemini is now the PRIMARY field extractor and detects the currency
    of a document's total (RM/MYR/USD/...) — purchase_orders/goods_receipts
    already had a currency column; extracted_fields (invoice) didn't.
    Same auto-create-on-startup pattern as the other _ensure_ functions
    above, for the same reason (no migration runner in this repo)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            ALTER TABLE extracted_fields
              ADD COLUMN IF NOT EXISTS currency VARCHAR(10)
        ''')
        conn.commit()
        conn.close()
        print('Invoice currency column ready')
    except Exception as e:
        print(f'WARNING: could not add invoice currency column: {type(e).__name__}: {e}')


def _ensure_document_line_items_table():
    """Line-item level 3-way audit matching: every row of a document's
    goods/services table (not just the first line item, which was the
    prior single-value item_description/quantity limitation — an invoice
    with 3 items only ever compared item #1, items 2/3 were never
    checked). One row per (document, line) here; document_id points at
    the SAME invoice document_id used across purchase_orders/
    goods_receipts (not po_id/gr_id), matching how those tables already
    key off the invoice's document_id. Same auto-create-on-startup
    pattern as the other _ensure_ functions above, for the same reason
    (no migration runner in this repo)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
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
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_document_line_items_doc
            ON document_line_items (document_id, document_type)
        ''')
        conn.commit()
        conn.close()
        print('document_line_items table ready')
    except Exception as e:
        print(f'WARNING: could not create document_line_items table: {type(e).__name__}: {e}')


def _ensure_gemini_cache_table():
    """Gemini-first extraction architecture: caches a Gemini extraction
    result by (file content hash, document type) so re-processing the
    exact same file bytes never calls Gemini a second time (see helpers/
    gemini_cache.py) — enforced at the DB level rather than in memory,
    since Gunicorn worker processes don't share memory and Render's free
    tier wipes process memory on every redeploy/restart anyway. Same
    auto-create-on-startup pattern as the other _ensure_ functions above,
    for the same reason (no migration runner in this repo)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gemini_extraction_cache (
                id SERIAL PRIMARY KEY,
                file_hash VARCHAR(64) NOT NULL,
                document_type VARCHAR(10) NOT NULL,
                gemini_result JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cursor.execute('''
            SELECT 1 FROM pg_constraint WHERE conname = 'gemini_extraction_cache_hash_type_key'
        ''')
        if not cursor.fetchone():
            cursor.execute('''
                ALTER TABLE gemini_extraction_cache
                ADD CONSTRAINT gemini_extraction_cache_hash_type_key UNIQUE (file_hash, document_type)
            ''')
        conn.commit()
        conn.close()
        print('gemini_extraction_cache table ready')
    except Exception as e:
        print(f'WARNING: could not create gemini_extraction_cache table: {type(e).__name__}: {e}')


def _ensure_claude_cache_table():
    """Same purpose/pattern as _ensure_gemini_cache_table() above, for
    Claude (see helpers/claude_cache.py) — a SEPARATE table from
    gemini_extraction_cache (not touched here) so the already-working
    Gemini cache carries zero risk from this addition. The extra
    `provider` column exists for parity with the requested cache key
    shape (file_hash + document_type + provider); every row here is
    provider='claude' today."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS claude_extraction_cache (
                id SERIAL PRIMARY KEY,
                file_hash VARCHAR(64) NOT NULL,
                document_type VARCHAR(10) NOT NULL,
                provider VARCHAR(10) NOT NULL DEFAULT 'claude',
                claude_result JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cursor.execute('''
            SELECT 1 FROM pg_constraint WHERE conname = 'claude_extraction_cache_hash_type_provider_key'
        ''')
        if not cursor.fetchone():
            cursor.execute('''
                ALTER TABLE claude_extraction_cache
                ADD CONSTRAINT claude_extraction_cache_hash_type_provider_key UNIQUE (file_hash, document_type, provider)
            ''')
        conn.commit()
        conn.close()
        print('claude_extraction_cache table ready')
    except Exception as e:
        print(f'WARNING: could not create claude_extraction_cache table: {type(e).__name__}: {e}')


def _ensure_authenticity_cache_table():
    """Same purpose/pattern as _ensure_claude_cache_table() above, for
    the authenticity engine (see helpers/authenticity_cache.py) — a
    SEPARATE table so this cost-optimization addition carries zero risk
    to the existing extraction caches or the authenticity_checks table
    itself. authenticity_version is part of the unique key so a prompt/
    schema change (bumping CLAUDE_AUTHENTICITY_PROMPT_VERSION) never
    serves a stale-shaped cached result."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS authenticity_result_cache (
                id SERIAL PRIMARY KEY,
                file_hash VARCHAR(64) NOT NULL,
                document_type VARCHAR(10) NOT NULL,
                authenticity_version VARCHAR(10) NOT NULL,
                engine VARCHAR(10) NOT NULL,
                raw_result JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cursor.execute('''
            SELECT 1 FROM pg_constraint WHERE conname = 'authenticity_result_cache_hash_type_version_key'
        ''')
        if not cursor.fetchone():
            cursor.execute('''
                ALTER TABLE authenticity_result_cache
                ADD CONSTRAINT authenticity_result_cache_hash_type_version_key
                UNIQUE (file_hash, document_type, authenticity_version)
            ''')
        conn.commit()
        conn.close()
        print('authenticity_result_cache table ready')
    except Exception as e:
        print(f'WARNING: could not create authenticity_result_cache table: {type(e).__name__}: {e}')


def _ensure_send_back_cycles_table():
    """Auditor <-> Finance send-back/correction workflow: one row per
    send-back CYCLE (not one mutable reason overwritten in place), so a
    record sent back multiple times keeps its full history — each new
    cycle is a new row, never an UPDATE over a prior cycle's reason/
    response. documents.status keeps its existing values (returned/
    resubmitted/approved/...) unchanged; cycle_status here is a finer-
    grained sub-state (action_required -> resubmitted -> resolved) used
    only for the richer UI, so no existing dashboard count/filter that
    reads documents.status is affected. Same auto-create-on-startup
    pattern as every other _ensure_ function above (no migration runner
    in this repo)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS send_back_cycles (
                cycle_id SERIAL PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                cycle_number INTEGER NOT NULL,
                return_reason_category VARCHAR(50) NOT NULL,
                reason_other_note TEXT,
                auditor_instruction TEXT NOT NULL,
                required_actions JSONB NOT NULL DEFAULT '[]',
                required_action_other_note TEXT,
                priority VARCHAR(10) NOT NULL DEFAULT 'normal',
                response_due_date DATE,
                sent_back_by INTEGER NOT NULL REFERENCES users(user_id),
                sent_back_at TIMESTAMP NOT NULL DEFAULT NOW(),
                finance_response TEXT,
                finance_responded_by INTEGER REFERENCES users(user_id),
                finance_responded_at TIMESTAMP,
                resubmitted_by INTEGER REFERENCES users(user_id),
                resubmitted_at TIMESTAMP,
                cycle_status VARCHAR(20) NOT NULL DEFAULT 'action_required',
                resolution VARCHAR(20),
                resolved_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE(document_id, cycle_number)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_send_back_cycles_document ON send_back_cycles(document_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_send_back_cycles_status ON send_back_cycles(cycle_status)')
        conn.commit()
        conn.close()
        print('send_back_cycles table ready')
    except Exception as e:
        print(f'WARNING: could not create send_back_cycles table: {type(e).__name__}: {e}')


def _ensure_calendar_tasks_table():
    """Audit Workflow Calendar's ONLY new table — manual audit tasks
    (title/description/date/assigned_to/priority) an auditor or finance
    user creates by hand. Every OTHER calendar event type (pending
    review, finance correction due, exception/anomaly follow-up) is
    computed on the fly from existing tables (documents, send_back_
    cycles, review_records-backed exception classification, anomalies) —
    nothing about those is stored here or duplicated. Same auto-create-
    on-startup pattern as every other _ensure_ function above (no
    migration runner in this repo)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS calendar_tasks (
                task_id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                description TEXT,
                event_date DATE NOT NULL,
                assigned_to INTEGER REFERENCES users(user_id),
                priority VARCHAR(10) NOT NULL DEFAULT 'normal',
                status VARCHAR(10) NOT NULL DEFAULT 'open',
                created_by INTEGER NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_calendar_tasks_date ON calendar_tasks(event_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_calendar_tasks_assigned ON calendar_tasks(assigned_to)')
        conn.commit()
        conn.close()
        print('calendar_tasks table ready')
    except Exception as e:
        print(f'WARNING: could not create calendar_tasks table: {type(e).__name__}: {e}')


def _ensure_ai_assistant_cache_table():
    """AI Audit Assistant response cache (helpers/ai_assistant.py,
    routes/ai_assistant.py) — same DB-backed pattern as claude_cache.py/
    gemini_cache.py/authenticity_cache.py (Gunicorn workers don't share
    memory, Render's free-tier disk is ephemeral), so an unchanged case
    + the same button/question never re-spends a Claude/Gemini call.
    context_hash already encodes the full case data + question, so this
    needs no separate invalidation logic: any change to the underlying
    case (new matching result, new authenticity/anomaly finding, a
    different question) naturally produces a new hash."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_assistant_cache (
                id SERIAL PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                action VARCHAR(30) NOT NULL,
                context_hash VARCHAR(64) NOT NULL,
                response JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(document_id, action, context_hash)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_assistant_cache_document ON ai_assistant_cache(document_id)')
        conn.commit()
        conn.close()
        print('ai_assistant_cache table ready')
    except Exception as e:
        print(f'WARNING: could not create ai_assistant_cache table: {type(e).__name__}: {e}')


def _ensure_review_records_need_review_action():
    """review_records.action has a CHECK constraint (review_records_
    action_check) limiting it to 'approved'/'returned'/'resubmitted'/
    'closed' — predating routes/reviews.py::need_review_document(),
    which inserts 'need_review'. Without this, every Need Review click
    fails with a Postgres CheckViolation. Widens the constraint to also
    allow 'need_review'; safe to run on every boot (DROP/ADD is
    idempotent — dropping a constraint that doesn't exist is a no-op
    under IF EXISTS, and re-adding the same definition is harmless)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('ALTER TABLE review_records DROP CONSTRAINT IF EXISTS review_records_action_check')
        cursor.execute('''
            ALTER TABLE review_records ADD CONSTRAINT review_records_action_check
                CHECK (action IN ('approved', 'returned', 'resubmitted', 'closed', 'need_review'))
        ''')
        conn.commit()
        conn.close()
        print("review_records.action_check widened for 'need_review'")
    except Exception as e:
        print(f'WARNING: could not widen review_records_action_check: {type(e).__name__}: {e}')


def _ensure_documents_withdrawn_duplicate_status():
    """documents.status has a CHECK constraint (documents_status_check)
    limiting it to 'uploaded'/'ocr_processing'/'ocr_done'/'under_review'/
    'returned'/'resubmitted'/'approved'/'completed' — predating the
    Finance-side duplicate-resolution feature (routes/reviews.py::
    withdraw_duplicate()), which sets 'withdrawn_duplicate'. Without
    this, every Withdraw This Duplicate click fails with a Postgres
    CheckViolation. Widens the constraint to also allow
    'withdrawn_duplicate'; safe to run on every boot (DROP/ADD is
    idempotent — dropping a constraint that doesn't exist is a no-op
    under IF EXISTS, and re-adding the same definition is harmless).
    The exact pre-existing value list was confirmed against the live
    database (pg_get_constraintdef) before writing this, not guessed —
    getting this wrong would silently forbid a status value some other
    part of the app already relies on."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_status_check')
        cursor.execute('''
            ALTER TABLE documents ADD CONSTRAINT documents_status_check
                CHECK (status IN ('uploaded', 'ocr_processing', 'ocr_done', 'under_review',
                                   'returned', 'resubmitted', 'approved', 'completed',
                                   'withdrawn_duplicate'))
        ''')
        conn.commit()
        conn.close()
        print("documents.status_check widened for 'withdrawn_duplicate'")
    except Exception as e:
        print(f'WARNING: could not widen documents_status_check: {type(e).__name__}: {e}')


def _ensure_document_review_steps_table():
    """New table backing the Audit Review page's guided/sequential
    review checklist (Three-Way Matching -> Exception Review ->
    Authenticity Review -> Anomaly Review) — one row per document per
    step, upserted on 'Mark as Reviewed' (UNIQUE(document_id, step)
    means re-marking just updates reviewed_by/reviewed_at rather than
    creating a duplicate). Deliberately separate from review_records
    (which tracks the document-level approve/return/need_review
    DECISION, unchanged) — this tracks step-level REVIEW PROGRESS, a
    different, finer-grained concept the sequential unlock/Approve-
    gating logic needs. Auto-create-on-startup, same pattern as every
    other table in this file — CREATE TABLE/INDEX IF NOT EXISTS is a
    no-op on a boot where the table already exists, so every existing
    row and the routes' own decision/gating logic are untouched either
    way; this only ever adds the table, never alters or drops data.

    A prior version of this function left `conn` open (never closed)
    on the except path — a connection-pool leak (see db.py's
    _PooledConnection) that, combined with a real CREATE-TABLE failure,
    could starve later requests of a healthy pooled connection with no
    clear signal why. Also adds an explicit post-CREATE verification
    query (to_regclass) so a failure surfaces as a loud, unmistakable
    ERROR line in the startup log — matching the exact symptom that
    triggered this fix ("relation document_review_steps does not
    exist" surfacing only later, at request time, from routes/reviews.
    py::mark_review_step, with no earlier indication in the deploy log
    that the migration itself never actually created the table)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS document_review_steps (
                id SERIAL PRIMARY KEY,
                document_id INT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                step VARCHAR(30) NOT NULL,
                reviewed_by INT NOT NULL REFERENCES users(user_id),
                reviewed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE(document_id, step)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_document_review_steps_document ON document_review_steps(document_id)')
        conn.commit()

        # Verify, don't assume — to_regclass returns NULL if the
        # relation still doesn't exist (e.g. the CREATE silently no-
        # opped for a reason other than "already exists", or ran
        # against a different schema/search_path than expected).
        cursor.execute("SELECT to_regclass('document_review_steps')")
        exists = cursor.fetchone()[0] is not None
        if exists:
            print('document_review_steps table ready (verified present)')
        else:
            print('ERROR: document_review_steps CREATE TABLE ran without error, '
                  'but the table is still not visible via to_regclass — check DB '
                  'user privileges / search_path on this environment.')
    except Exception as e:
        print(f'WARNING: could not create document_review_steps table: {type(e).__name__}: {e}')
    finally:
        if conn is not None:
            conn.close()


def _ensure_document_relationships_table():
    """Enterprise V3 Phase 1: flexible many-to-many document relationships
    (PO<->Invoice, PO<->GR, Invoice<->GR), additive to the existing
    one-to-one attachment model (purchase_orders.document_id /
    goods_receipts.document_id). Purely new and unread by any existing
    page — _build_comparison() (routes/auditor.py) and every page built
    on it are completely untouched by this table's existence.

    Polymorphic association: purchase_orders/goods_receipts rows have no
    independent row in `documents`, so parent_id/child_id mean different
    things depending on parent_type/child_type ('invoice' -> documents.
    document_id, 'po' -> purchase_orders.po_id, 'gr' -> goods_receipts.
    gr_id). A single-column SQL FOREIGN KEY can't target three different
    tables, so referential integrity for parent_id/child_id is enforced
    in helpers/document_relationships.py::create_relationship() instead —
    see the matching migrations/*.sql file for the full rationale."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS document_relationships (
                id SERIAL PRIMARY KEY,
                parent_type VARCHAR(10) NOT NULL,
                parent_id INTEGER NOT NULL,
                child_type VARCHAR(10) NOT NULL,
                child_id INTEGER NOT NULL,
                relationship_type VARCHAR(20) NOT NULL,
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
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_document_relationships_parent ON document_relationships(parent_type, parent_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_document_relationships_child ON document_relationships(child_type, child_id)')
        conn.commit()
        conn.close()
        print('document_relationships table ready')
    except Exception as e:
        print(f'WARNING: could not create document_relationships table: {type(e).__name__}: {e}')


def _ensure_document_relationships_v2_columns():
    """Enterprise V3 Phase 2: additive columns so the deterministic
    relationship builder (helpers/relationship_builder.py) can coexist
    with Phase 1's manual API without ever overwriting a human-confirmed
    link — see the matching migrations/*.sql file for the full
    rationale. relationship_source ('manual'|'auto') defaults to
    'manual', so every existing Phase 1 row (created via the POST API)
    keeps its correct meaning unchanged. Safe to run on every boot:
    every step is idempotent (IF NOT EXISTS / guarded by a pg_constraint
    lookup)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            ALTER TABLE document_relationships
              ADD COLUMN IF NOT EXISTS relationship_source VARCHAR(10) NOT NULL DEFAULT 'manual',
              ADD COLUMN IF NOT EXISTS matching_reason TEXT
        ''')
        cursor.execute('''
            SELECT 1 FROM pg_constraint WHERE conname = 'chk_document_relationships_source'
        ''')
        if not cursor.fetchone():
            cursor.execute('''
                ALTER TABLE document_relationships
                ADD CONSTRAINT chk_document_relationships_source CHECK (relationship_source IN ('manual', 'auto'))
            ''')
        conn.commit()
        conn.close()
        print('document_relationships v2 columns ready')
    except Exception as e:
        print(f'WARNING: could not add document_relationships v2 columns: {type(e).__name__}: {e}')


def _ensure_transaction_packages_table():
    """Enterprise V3 Phase 5: Finance Transaction Package workflow —
    lets Finance group related AP documents (PO/Invoice/GR) into one
    package before auditor review. Purely additive/organizational —
    does not replace or duplicate document_relationships (Phase 1) or
    any upload/OCR/extraction/matching logic. See the matching
    migrations/*.sql file for the full rationale."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transaction_packages (
                id SERIAL PRIMARY KEY,
                package_name VARCHAR(200) NOT NULL,
                created_by INTEGER NOT NULL REFERENCES users(user_id),
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT chk_transaction_packages_status
                    CHECK (status IN ('draft', 'waiting_documents', 'processing', 'completed'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transaction_packages_created_by ON transaction_packages(created_by)')
        conn.commit()
        conn.close()
        print('transaction_packages table ready')
    except Exception as e:
        print(f'WARNING: could not create transaction_packages table: {type(e).__name__}: {e}')


def _ensure_transaction_package_documents_table():
    """Polymorphic association, same pattern/trade-off as Phase 1's
    document_relationships table — document_id means documents.
    document_id / purchase_orders.po_id / goods_receipts.gr_id
    depending on document_role, so no single-table SQL FOREIGN KEY is
    possible; referential integrity is enforced in helpers/
    transaction_packages.py::link_document_to_package()."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transaction_package_documents (
                id SERIAL PRIMARY KEY,
                package_id INTEGER NOT NULL REFERENCES transaction_packages(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL,
                document_role VARCHAR(20) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT chk_transaction_package_documents_role
                    CHECK (document_role IN ('invoice', 'purchase_order', 'goods_receipt')),
                CONSTRAINT uq_transaction_package_documents UNIQUE (package_id, document_role, document_id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transaction_package_documents_package ON transaction_package_documents(package_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transaction_package_documents_doc ON transaction_package_documents(document_role, document_id)')
        conn.commit()
        conn.close()
        print('transaction_package_documents table ready')
    except Exception as e:
        print(f'WARNING: could not create transaction_package_documents table: {type(e).__name__}: {e}')


# Authentication Phase — the one initial admin account. Admin is
# deliberately NOT reachable through POST /auth/register (see routes/
# auth.py::register()'s allowed_roles) — this is the only place an
# admin account gets created without one already existing. Same auto-
# run-on-startup, idempotent pattern as every other _ensure_ function
# above: a no-op once the account exists, so it's safe to redeploy any
# number of times. Uses the EXACT SAME bcrypt hashing routes/auth.py's
# own register()/login() already use — no new auth mechanism, no
# hardcoded login bypass anywhere in the frontend.
_ADMIN_SEED_EMAIL = 'loylth910@gmail.com'
_ADMIN_SEED_PASSWORD = 'admin123'


def _ensure_admin_seed_account():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE email = %s', (_ADMIN_SEED_EMAIL,))
        if cursor.fetchone():
            conn.close()
            print('Admin seed account already exists')
            return
        password_hash = bcrypt.hashpw(_ADMIN_SEED_PASSWORD.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cursor.execute(
            '''INSERT INTO users (full_name, email, password_hash, role)
               VALUES (%s, %s, %s, 'admin')''',
            ('System Administrator', _ADMIN_SEED_EMAIL, password_hash)
        )
        conn.commit()
        conn.close()
        print(f'Admin seed account created: {_ADMIN_SEED_EMAIL}')
    except Exception as e:
        print(f'WARNING: could not ensure admin seed account exists: {type(e).__name__}: {e}')


class _UtcJSONProvider(DefaultJSONProvider):
    """Every timestamp column in this schema is a naive `timestamp
    without time zone` (db.py's pooled connections now force the
    session timezone to UTC on every environment, so those naive values
    are consistently real UTC digits). A raw datetime reaching jsonify()
    without an explicit .isoformat() call would otherwise fall through
    to the default provider's http_date()-based "Sun, 02 Aug 2026
    21:19:00 GMT" format - technically parseable, but not ISO 8601 and
    inconsistent with the routes that already call
    to_utc_iso()/serialize_row_datetimes() by hand. This is the
    catch-all for anything that doesn't. date objects get the same
    fallback fix (a plain isoformat() instead of the GMT string) but are
    never Z-suffixed - a date-only value has no time-of-day to mark as
    UTC, and must never gain one (see helpers/time_format.py)."""
    @staticmethod
    def default(obj):
        if isinstance(obj, datetime):
            return to_utc_iso(obj)
        if isinstance(obj, date):
            return obj.isoformat()
        return DefaultJSONProvider.default(obj)


app = Flask(__name__)
app.json = _UtcJSONProvider(app)

app.config['JWT_SECRET_KEY']           = Config.JWT_SECRET_KEY
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = Config.JWT_ACCESS_TOKEN_EXPIRES
jwt = JWTManager(app)

CORS(app, origins=Config.FRONTEND_ORIGINS, supports_credentials=True)

app.register_blueprint(auth_bp,      url_prefix='/auth')
app.register_blueprint(documents_bp, url_prefix='/documents')
app.register_blueprint(matching_bp,  url_prefix='/matching')
app.register_blueprint(reviews_bp,   url_prefix='/reviews')
app.register_blueprint(admin_bp,     url_prefix='/admin')
app.register_blueprint(ocr_review_bp, url_prefix='/ocr-review')
app.register_blueprint(auditor_bp,    url_prefix='/auditor')
app.register_blueprint(anomalies_bp,  url_prefix='/anomalies')
app.register_blueprint(authenticity_bp, url_prefix='/authenticity')
app.register_blueprint(calendar_bp,   url_prefix='/calendar')
app.register_blueprint(ai_assistant_bp, url_prefix='/ai-assistant')
app.register_blueprint(document_relationships_bp, url_prefix='/documents')
app.register_blueprint(transaction_packages_bp, url_prefix='/transaction-packages')

_ensure_anomalies_table()
_ensure_anomaly_detection_runs_table()
_ensure_authenticity_checks_table()
_ensure_authenticity_v2_columns()
_ensure_authenticity_v3_columns()
_ensure_authenticity_evidence_corrections_table()
_ensure_file_bytes_columns()
_ensure_document_hash_columns()
_ensure_3way_comparison_columns()
_ensure_invoice_currency_column()
_ensure_document_line_items_table()
_ensure_gemini_cache_table()
_ensure_claude_cache_table()
_ensure_authenticity_cache_table()
_ensure_send_back_cycles_table()
_ensure_calendar_tasks_table()
_ensure_ai_assistant_cache_table()
_ensure_review_records_need_review_action()
_ensure_documents_withdrawn_duplicate_status()
_ensure_document_review_steps_table()
_ensure_document_relationships_table()
_ensure_document_relationships_v2_columns()
_ensure_transaction_packages_table()
_ensure_transaction_package_documents_table()
_ensure_admin_seed_account()

@app.route('/')
def hello_world():
    return jsonify({'message': 'Welcome to AuditLens API!'})

if __name__ == '__main__':
    app.run(debug=True)