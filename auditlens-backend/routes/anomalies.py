from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.anomaly_detector import run_anomaly_detection

anomalies_bp = Blueprint('anomalies', __name__)

VALID_SEVERITIES = ('high', 'medium', 'low')
VALID_TYPES = ('amount', 'round', 'weekend', 'duplicate')
VALID_REVIEW_STATUSES = ('reviewed', 'dismissed')


# ------------------------------------------------------------
# GET ANOMALIES LIST
# GET /anomalies?severity=&type=&status=&limit=&offset=
# Auditor only
# ------------------------------------------------------------
@anomalies_bp.route('', methods=['GET'])
@jwt_required()
def get_anomalies():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    severity_filter = request.args.get('severity', 'all')
    type_filter     = request.args.get('type', 'all')
    status_filter   = request.args.get('status', 'all')
    limit  = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        where = []
        params = []
        if severity_filter != 'all' and severity_filter in VALID_SEVERITIES:
            where.append('a.severity = %s')
            params.append(severity_filter)
        if type_filter != 'all' and type_filter in VALID_TYPES:
            where.append('a.anomaly_type = %s')
            params.append(type_filter)
        if status_filter != 'all' and status_filter in ('pending', 'reviewed', 'dismissed'):
            where.append('a.status = %s')
            params.append(status_filter)

        where_clause = ('WHERE ' + ' AND '.join(where)) if where else ''

        cursor.execute(
            f'''SELECT a.anomaly_id, a.invoice_document_id,
                       ef.invoice_number AS invoice_no, ef.vendor_name,
                       ef.invoice_date, ef.total_amount AS amount,
                       a.anomaly_type, a.severity, a.detected_pattern,
                       a.ai_explanation, a.ai_recommendation, a.status,
                       a.created_at
                FROM anomalies a
                LEFT JOIN extracted_fields ef ON a.invoice_document_id = ef.document_id
                {where_clause}
                ORDER BY a.created_at DESC
                LIMIT %s OFFSET %s''',
            params + [limit, offset]
        )
        rows = cursor.fetchall()
        conn.close()

        return jsonify(rows), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# REVIEW / DISMISS AN ANOMALY
# POST /anomalies/<id>/review
# Body: {"status": "reviewed" | "dismissed", "note": "optional"}
# Auditor only
# ------------------------------------------------------------
@anomalies_bp.route('/<int:anomaly_id>/review', methods=['POST'])
@jwt_required()
def review_anomaly(anomaly_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    data   = request.get_json() or {}
    status = data.get('status')

    if status not in VALID_REVIEW_STATUSES:
        return jsonify({'error': f'status must be one of {VALID_REVIEW_STATUSES}'}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute('SELECT anomaly_id FROM anomalies WHERE anomaly_id = %s', (anomaly_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Anomaly not found'}), 404

        cursor.execute(
            '''UPDATE anomalies
               SET status = %s, reviewed_by = %s, reviewed_at = NOW()
               WHERE anomaly_id = %s
               RETURNING anomaly_id, status, reviewed_by, reviewed_at''',
            (status, user['user_id'], anomaly_id)
        )
        updated = cursor.fetchone()
        conn.commit()
        conn.close()

        return jsonify(updated), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# RE-RUN ANOMALY ANALYSIS FOR EVERY INVOICE
# POST /anomalies/run
# Auditor only
#
# Re-runs the EXISTING per-document detection (helpers/anomaly_detector.
# py::run_anomaly_detection — untouched) across every invoice that has
# extracted fields, the same delete-then-redetect pattern already used
# by routes/admin.py's single-document diagnostic endpoint and scripts/
# backfill_anomaly_detection.py, just looped across every invoice
# instead of one. Lets a vendor's baseline grow (more history since a
# document was first uploaded) actually change that document's result
# on demand, from the auditor-facing page, instead of only via the
# admin-only diagnostic route or a manually-run script.
# ------------------------------------------------------------
def _run_full_anomaly_analysis():
    conn   = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute('SELECT DISTINCT document_id FROM extracted_fields ORDER BY document_id')
    document_ids = [row['document_id'] for row in cursor.fetchall()]
    conn.close()

    anomalies_found = 0
    errors = 0
    for document_id in document_ids:
        try:
            conn   = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM anomalies WHERE invoice_document_id = %s', (document_id,))
            conn.commit()
            conn.close()

            created_ids = run_anomaly_detection(document_id)
            anomalies_found += len(created_ids)
        except Exception as e:
            errors += 1
            print(f"DEBUG /anomalies/run: document_id={document_id} failed: {type(e).__name__}: {e}")

    return {
        'documents_analyzed': len(document_ids),
        'anomalies_found': anomalies_found,
        'errors': errors,
    }


@anomalies_bp.route('/run', methods=['POST'])
@jwt_required()
def run_full_anomaly_analysis():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        result = _run_full_anomaly_analysis()
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# ANOMALY STATS FOR DASHBOARD
# GET /anomalies/stats
# Auditor only
# ------------------------------------------------------------
@anomalies_bp.route('/stats', methods=['GET'])
@jwt_required()
def get_anomaly_stats():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute('SELECT COUNT(*) AS cnt FROM anomalies')
        total = cursor.fetchone()['cnt']

        cursor.execute('SELECT severity, COUNT(*) AS cnt FROM anomalies GROUP BY severity')
        by_severity = {row['severity']: row['cnt'] for row in cursor.fetchall()}

        cursor.execute('SELECT anomaly_type, COUNT(*) AS cnt FROM anomalies GROUP BY anomaly_type')
        by_type = {row['anomaly_type']: row['cnt'] for row in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) AS cnt FROM anomalies WHERE status = 'pending'")
        pending = cursor.fetchone()['cnt']

        # Every invoice with extracted fields has gone through detection
        # (run_anomaly_detection is auto-triggered right after extraction
        # for every invoice, unconditionally) — the real, existing count
        # of screened transactions, not a new/separate tally.
        cursor.execute('SELECT COUNT(*) AS cnt FROM extracted_fields')
        transactions_analysed = cursor.fetchone()['cnt']

        # anomaly_detection_runs logs one row per actual detection run,
        # including clean results that leave no `anomalies` row — see
        # helpers/anomaly_detector.py::_log_detection_run. NULL (never
        # analysed) until the first run after this table was added.
        cursor.execute('SELECT MAX(run_at) AS last_run FROM anomaly_detection_runs')
        last_run = cursor.fetchone()['last_run']

        conn.close()

        return jsonify({
            'total': total,
            'by_severity': {s: by_severity.get(s, 0) for s in VALID_SEVERITIES},
            'by_type': {t: by_type.get(t, 0) for t in VALID_TYPES},
            'pending': pending,
            'transactions_analysed': transactions_analysed,
            'last_analysed': last_run.isoformat() if last_run else None,
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
