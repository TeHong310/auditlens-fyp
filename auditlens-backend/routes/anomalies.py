from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.anomaly_detector import run_anomaly_detection
from helpers.time_format import to_utc_iso

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
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()

        # ef.invoice_date/a.created_at are raw date/datetime objects —
        # Flask's default JSON encoder formats those as an HTTP-style
        # "Wed, 11 Feb 2026 00:00:00 GMT" string instead of converting
        # them, so they're explicitly serialized here (same fix already
        # applied to authenticity_checks.created_at elsewhere in this
        # app) rather than left for jsonify() to mangle. invoice_date is
        # date-only (no time-of-day) and stays a plain isoformat() string
        # unchanged; created_at is a real timestamp and gets the explicit
        # UTC 'Z' marker so the frontend can convert it to Asia/Kuala_
        # Lumpur unambiguously.
        for row in rows:
            if row.get('invoice_date') is not None:
                row['invoice_date'] = row['invoice_date'].isoformat()
            if row.get('created_at') is not None:
                row['created_at'] = to_utc_iso(row['created_at'])

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
# extracted fields, looped across every invoice instead of one. Lets a
# vendor's baseline grow (more history since a document was first
# uploaded), or a Finance correction, actually change that document's
# result on demand, from the auditor-facing page.
#
# Only 'pending' anomalies for each document are cleared before
# re-detecting — a 'reviewed' or 'dismissed' row is an auditor decision
# already on the audit record and is NEVER deleted by a re-run, so
# "preserve previous anomaly and review history before performing any
# new analysis" holds even when a corrected package is re-screened.
# routes/admin.py's single-document diagnostic endpoint and scripts/
# backfill_anomaly_detection.py still use the older blanket-delete
# behavior for their own explicit "wipe and redetect one document"
# purpose — this route is the one auditors actually use day to day, so
# only this one needed the history-preserving fix.
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
            cursor.execute(
                "DELETE FROM anomalies WHERE invoice_document_id = %s AND status = 'pending'",
                (document_id,)
            )
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

        # anomalies has no document_id column of its own — the real FK is
        # anomalies.invoice_document_id -> documents.document_id. Every
        # count below is computed from anomalies INNER JOINed to
        # documents on that column, which both uses the correct names
        # and naturally excludes an "orphan" anomaly row (one whose
        # invoice_document_id no longer matches a real document) from
        # every total, instead of it silently inflating a count.
        cursor.execute(
            '''SELECT COUNT(*) AS cnt FROM anomalies a
               JOIN documents d ON a.invoice_document_id = d.document_id'''
        )
        total = cursor.fetchone()['cnt']

        cursor.execute(
            '''SELECT a.severity, COUNT(*) AS cnt FROM anomalies a
               JOIN documents d ON a.invoice_document_id = d.document_id
               GROUP BY a.severity'''
        )
        by_severity = {row['severity']: row['cnt'] for row in cursor.fetchall()}

        cursor.execute(
            '''SELECT a.anomaly_type, COUNT(*) AS cnt FROM anomalies a
               JOIN documents d ON a.invoice_document_id = d.document_id
               GROUP BY a.anomaly_type'''
        )
        by_type = {row['anomaly_type']: row['cnt'] for row in cursor.fetchall()}

        # by_status covers all 3 real status values regardless of the
        # currently-selected list filter (section A: filter counts must
        # never read as zero just because a different status/severity/
        # type chip is active) — also what the Anomaly Detection page's
        # "No pending anomalies, N reviewed anomaly available under the
        # Reviewed filter" empty-state message is built from.
        cursor.execute(
            '''SELECT a.status, COUNT(*) AS cnt FROM anomalies a
               JOIN documents d ON a.invoice_document_id = d.document_id
               GROUP BY a.status'''
        )
        by_status = {row['status']: row['cnt'] for row in cursor.fetchall()}
        pending = by_status.get('pending', 0)

        # anomaly_detection_runs logs one row per actual detection run,
        # including clean results that leave no `anomalies` row, so it's
        # the authoritative source for both "how many transactions have
        # been screened" and "when was detection last run" — see
        # helpers/anomaly_detector.py::_log_detection_run. Isolated in
        # its own try/except: an empty table is handled fine by plain SQL
        # (COUNT is 0, MAX is NULL, no exception either way), but if the
        # table is missing/broken for any reason this must degrade to the
        # fallback below rather than 500 the whole endpoint. rollback()
        # clears the aborted-transaction state Postgres leaves behind
        # after a caught error, so the connection stays usable for the
        # fallback query that follows.
        transactions_analysed = 0
        last_run = None
        try:
            cursor.execute(
                'SELECT COUNT(DISTINCT invoice_document_id) AS cnt, MAX(run_at) AS last_run '
                'FROM anomaly_detection_runs'
            )
            row = cursor.fetchone()
            transactions_analysed = row['cnt'] or 0
            last_run = row['last_run']
        except Exception as e:
            conn.rollback()
            print(f'WARNING /anomalies/stats: anomaly_detection_runs unavailable, falling back: {type(e).__name__}: {e}')

        # Fallback (task: "distinct invoice_document_id as fallback") —
        # anomaly_detection_runs came back empty/unusable, but real,
        # valid anomaly records may still exist (e.g. an older DB that
        # predates this table). last_analysed has no fallback source and
        # stays None ("Never") in that case, which is honest — only
        # anomaly_detection_runs actually knows when detection last ran.
        if transactions_analysed == 0:
            cursor.execute(
                '''SELECT COUNT(DISTINCT a.invoice_document_id) AS cnt FROM anomalies a
                   JOIN documents d ON a.invoice_document_id = d.document_id'''
            )
            transactions_analysed = cursor.fetchone()['cnt']

        conn.close()

        return jsonify({
            'total': total,
            'by_severity': {s: by_severity.get(s, 0) for s in VALID_SEVERITIES},
            'by_type': {t: by_type.get(t, 0) for t in VALID_TYPES},
            'by_status': {s: by_status.get(s, 0) for s in ('pending', 'reviewed', 'dismissed')},
            'pending': pending,
            'transactions_analysed': transactions_analysed,
            'last_analysed': to_utc_iso(last_run),
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
