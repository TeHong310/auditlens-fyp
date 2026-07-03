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

@app.route('/')
def hello_world():
    return jsonify({'message': 'Welcome to AuditLens API!'})

if __name__ == '__main__':
    app.run(debug=True)