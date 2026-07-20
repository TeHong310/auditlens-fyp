import io
import contextlib
import os
import psutil
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
import bcrypt
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit
from helpers.anomaly_detector import run_anomaly_detection
from config import Config

MEMORY_LIMIT_MB = 512

admin_bp = Blueprint('admin', __name__)

# ------------------------------------------------------------
# GET ALL USERS
# GET /admin/users
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/users', methods=['GET'])
@jwt_required()
def get_all_users():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            '''SELECT user_id, full_name, email, role, is_active, created_at
               FROM users
               ORDER BY created_at DESC'''
        )
        users = cursor.fetchall()
        conn.close()

        result = []
        for u in users:
            row = dict(u)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({
            'total': len(result),
            'users': result
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# CREATE USER (Admin creates user directly)
# POST /admin/users/create
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/users/create', methods=['POST'])
@jwt_required()
def create_user():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    data = request.get_json()

    required_fields = ['full_name', 'email', 'password', 'role']
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({'error': f'{field} is required'}), 400

    full_name = data['full_name'].strip()
    email     = data['email'].strip().lower()
    password  = data['password']
    role      = data['role'].strip().lower()

    allowed_roles = ['admin', 'finance_executive', 'auditor']
    if role not in allowed_roles:
        return jsonify({'error': f'Invalid role. Must be one of: {allowed_roles}'}), 400

    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        # Check if email already exists
        cursor.execute('SELECT user_id FROM users WHERE email = %s', (email,))
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Email already registered'}), 409

        cursor.execute(
            '''INSERT INTO users (full_name, email, password_hash, role)
               VALUES (%s, %s, %s, %s) RETURNING user_id''',
            (full_name, email, password_hash, role)
        )
        new_user_id = cursor.fetchone()[0]
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'CREATE_USER', 'users', new_user_id,
                  f'Admin created user: {email} as {role}')

        return jsonify({
            'message':  'User created successfully',
            'user_id':  new_user_id,
            'email':    email,
            'role':     role
        }), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# ACTIVATE / DEACTIVATE USER
# PUT /admin/users/<user_id>/status
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/users/<int:target_user_id>/status', methods=['PUT'])
@jwt_required()
def update_user_status(target_user_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    data      = request.get_json()
    is_active = data.get('is_active')

    if is_active is None:
        return jsonify({'error': 'is_active field is required (true or false)'}), 400

    if str(target_user_id) == str(user['user_id']):
        return jsonify({'error': 'You cannot deactivate your own account'}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT user_id, full_name, email FROM users WHERE user_id = %s',
            (target_user_id,)
        )
        target = cursor.fetchone()

        if not target:
            conn.close()
            return jsonify({'error': 'User not found'}), 404

        cursor.execute(
            'UPDATE users SET is_active = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s',
            (is_active, target_user_id)
        )
        conn.commit()
        conn.close()

        action = 'ACTIVATE_USER' if is_active else 'DEACTIVATE_USER'
        log_audit(user['user_id'], action, 'users', target_user_id,
                  f'Admin {"activated" if is_active else "deactivated"} user {target[2]}')

        return jsonify({
            'message':   f'User {"activated" if is_active else "deactivated"} successfully',
            'user_id':   target_user_id,
            'is_active': is_active
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# UPDATE USER ROLE
# PUT /admin/users/<user_id>/role
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/users/<int:target_user_id>/role', methods=['PUT'])
@jwt_required()
def update_user_role(target_user_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    data = request.get_json()
    role = data.get('role', '').strip().lower()

    allowed_roles = ['admin', 'finance_executive', 'auditor']
    if role not in allowed_roles:
        return jsonify({'error': f'Invalid role. Must be one of: {allowed_roles}'}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT user_id FROM users WHERE user_id = %s', (target_user_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'error': 'User not found'}), 404

        cursor.execute(
            'UPDATE users SET role = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s',
            (role, target_user_id)
        )
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'UPDATE_USER_ROLE', 'users', target_user_id,
                  f'Admin changed user {target_user_id} role to {role}')

        return jsonify({
            'message': 'User role updated successfully',
            'user_id': target_user_id,
            'role':    role
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET ALL DOCUMENTS (Admin view)
# GET /admin/documents
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/documents', methods=['GET'])
@jwt_required()
def get_all_documents():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT
                d.document_id,
                d.file_name,
                d.status,
                d.uploaded_at,
                d.updated_at,
                u.full_name AS uploaded_by,
                u.email     AS uploaded_by_email,
                ef.invoice_number,
                ef.vendor_name,
                ef.total_amount,
                ef.ocr_confidence,
                rm.match_score,
                rm.overall_status AS match_status
               FROM documents d
               JOIN users u ON d.uploaded_by = u.user_id
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               LEFT JOIN LATERAL (
                   SELECT match_score, overall_status
                   FROM record_matches
                   WHERE document_id = d.document_id
                   ORDER BY matched_at DESC
                   LIMIT 1
               ) rm ON TRUE
               ORDER BY d.uploaded_at DESC'''
        )
        documents = cursor.fetchall()
        conn.close()

        result = []
        for d in documents:
            row = dict(d)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({
            'total':     len(result),
            'documents': result
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET EXCEPTION LOGS
# GET /admin/exceptions
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/exceptions', methods=['GET'])
@jwt_required()
def get_exception_logs():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT e.*, d.file_name, u.full_name AS uploaded_by
               FROM exceptions e
               JOIN documents d ON e.document_id = d.document_id
               JOIN users u ON d.uploaded_by = u.user_id
               ORDER BY e.created_at DESC'''
        )
        exceptions = cursor.fetchall()
        conn.close()

        result = []
        for ex in exceptions:
            row = dict(ex)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({
            'total':      len(result),
            'exceptions': result
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET AUDIT LOGS
# GET /admin/audit-logs
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/audit-logs', methods=['GET'])
@jwt_required()
def get_audit_logs():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    limit = request.args.get('limit', 50, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT al.*, u.full_name AS user_name, u.role AS user_role
               FROM audit_logs al
               LEFT JOIN users u ON al.user_id = u.user_id
               ORDER BY al.logged_at DESC
               LIMIT %s''',
            (limit,)
        )
        logs = cursor.fetchall()
        conn.close()

        result = []
        for log in logs:
            row = dict(log)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({
            'total': len(result),
            'logs':  result
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET SYSTEM STATISTICS
# GET /admin/statistics
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_statistics():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # User stats
        cursor.execute(
            '''SELECT
                COUNT(*) FILTER (WHERE role = 'admin')             AS total_admins,
                COUNT(*) FILTER (WHERE role = 'finance_executive') AS total_finance,
                COUNT(*) FILTER (WHERE role = 'auditor')           AS total_auditors,
                COUNT(*) FILTER (WHERE is_active = TRUE)           AS active_users,
                COUNT(*)                                            AS total_users
               FROM users'''
        )
        user_stats = cursor.fetchone()

        # Document stats
        cursor.execute(
            '''SELECT
                COUNT(*) FILTER (WHERE status = 'uploaded')      AS uploaded,
                COUNT(*) FILTER (WHERE status = 'ocr_done')      AS ocr_done,
                COUNT(*) FILTER (WHERE status = 'under_review')  AS under_review,
                COUNT(*) FILTER (WHERE status = 'returned')      AS returned,
                COUNT(*) FILTER (WHERE status = 'resubmitted')   AS resubmitted,
                COUNT(*) FILTER (WHERE status = 'approved')      AS approved,
                COUNT(*)                                          AS total_documents
               FROM documents'''
        )
        doc_stats = cursor.fetchone()

        # Match stats
        cursor.execute(
            '''SELECT
                COUNT(*) FILTER (WHERE overall_status = 'matched')  AS matched,
                COUNT(*) FILTER (WHERE overall_status = 'partial')  AS partial,
                COUNT(*) FILTER (WHERE overall_status = 'mismatch') AS mismatch,
                ROUND(AVG(match_score), 2)                          AS avg_match_score
               FROM record_matches'''
        )
        match_stats = cursor.fetchone()

        # Exception stats
        cursor.execute(
            '''SELECT
                COUNT(*) FILTER (WHERE is_resolved = FALSE) AS unresolved,
                COUNT(*) FILTER (WHERE is_resolved = TRUE)  AS resolved,
                COUNT(*)                                     AS total_exceptions
               FROM exceptions'''
        )
        exception_stats = cursor.fetchone()

        conn.close()

        return jsonify({
            'users':      dict(user_stats),
            'documents':  dict(doc_stats),
            'matching':   dict(match_stats),
            'exceptions': dict(exception_stats)
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# PROCESS MEMORY USAGE
# GET /admin/debug/memory
# Admin only — Render's free tier caps the backend at 512MB RAM, this
# lets an admin check current usage without shell access to the dyno.
# ------------------------------------------------------------
@admin_bp.route('/debug/memory', methods=['GET'])
@jwt_required()
def get_memory_usage():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    try:
        process = psutil.Process(os.getpid())
        process_memory_mb = round(process.memory_info().rss / (1024 * 1024), 2)
        usage_percentage = round((process_memory_mb / MEMORY_LIMIT_MB) * 100, 2)

        return jsonify({
            'process_memory_mb': process_memory_mb,
            'memory_limit_mb':   MEMORY_LIMIT_MB,
            'usage_percentage':  usage_percentage
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# TEMP DIAGNOSTIC: RE-RUN ANOMALY DETECTION FOR ONE DOCUMENT
# POST /admin/rerun-anomaly/<doc_id>?token=<ADMIN_TOKEN>
#
# TODO: Remove this endpoint after FYP demo. It exists only because
# Render's free tier has no Shell, so scripts/backfill_anomaly_
# detection.py can't be run there directly — this exposes the same
# delete-then-redetect logic over HTTP so it can be triggered from a
# browser/Postman instead.
#
# No @jwt_required() here on purpose (unlike every other route in this
# file) — it's meant to be hit without first extracting/pasting a JWT.
# Guarded by a single shared-secret query param instead. If ADMIN_TOKEN
# isn't set in the environment, the guard is skipped entirely and the
# route is open to anyone who finds the URL — deliberately, as a dev
# fallback, but that means this MUST have ADMIN_TOKEN set before this
# is deployed anywhere public. A hit with no token configured is logged
# as a warning so it's visible in Render's logs either way.
# ------------------------------------------------------------
@admin_bp.route('/rerun-anomaly/<int:doc_id>', methods=['POST'])
def rerun_anomaly_detection(doc_id):
    if Config.ADMIN_TOKEN:
        if request.args.get('token') != Config.ADMIN_TOKEN:
            return jsonify({'error': 'Invalid or missing token'}), 403
    else:
        print('WARNING: /admin/rerun-anomaly hit with no ADMIN_TOKEN set - endpoint is unauthenticated')

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM anomalies WHERE invoice_document_id = %s', (doc_id,))
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()

        log_buffer = io.StringIO()
        created_ids = []
        detection_error = None
        try:
            with contextlib.redirect_stdout(log_buffer):
                created_ids = run_anomaly_detection(doc_id)
        except Exception as e:
            detection_error = f'{type(e).__name__}: {e}'

        response = {
            'doc_id': doc_id,
            'deleted_count': deleted_count,
            'created_anomaly_ids': created_ids,
            'debug_log': log_buffer.getvalue()
        }
        if detection_error:
            response['error'] = detection_error
            return jsonify(response), 500

        return jsonify(response), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500