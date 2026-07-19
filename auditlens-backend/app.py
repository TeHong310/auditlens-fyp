from flask import Flask, jsonify
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from config import Config
from db import get_db_connection
from routes.auth import auth_bp
from routes.documents import documents_bp
from routes.matching import matching_bp
from routes.reviews import reviews_bp
from routes.admin import admin_bp
from routes.ocr_review import ocr_review_bp
from routes.auditor import auditor_bp
from routes.anomalies import anomalies_bp
from routes.authenticity import authenticity_bp


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


app = Flask(__name__)

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

_ensure_anomalies_table()
_ensure_authenticity_checks_table()
_ensure_authenticity_v2_columns()
_ensure_file_bytes_columns()
_ensure_3way_comparison_columns()
_ensure_invoice_currency_column()
_ensure_document_line_items_table()

@app.route('/')
def hello_world():
    return jsonify({'message': 'Welcome to AuditLens API!'})

if __name__ == '__main__':
    app.run(debug=True)