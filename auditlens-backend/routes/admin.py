import io
import contextlib
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
import bcrypt
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit
from helpers.anomaly_detector import run_anomaly_detection
from config import Config

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
# DELETE USER
# DELETE /admin/users/<user_id>
# Admin only
#
# Safe-deletion: users are referenced (NO ACTION, no CASCADE) from
# documents, purchase_orders, goods_receipts, review_records,
# send_back_cycles, anomalies, calendar_tasks, transaction_packages
# and audit_logs. Rather than hand-checking every one of those tables,
# the DELETE is simply attempted and a ForeignKeyViolation is caught —
# this covers all of them automatically and can't go stale if a future
# table adds its own FK to users. Documents are never cascade-deleted;
# a user with any linked records must be disabled instead.
# ------------------------------------------------------------
@admin_bp.route('/users/<int:target_user_id>', methods=['DELETE'])
@jwt_required()
def delete_user(target_user_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    if str(target_user_id) == str(user['user_id']):
        return jsonify({'error': 'You cannot delete your own account'}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT full_name, email FROM users WHERE user_id = %s', (target_user_id,))
        target = cursor.fetchone()
        if not target:
            conn.close()
            return jsonify({'error': 'User not found'}), 404

        try:
            cursor.execute('DELETE FROM users WHERE user_id = %s', (target_user_id,))
            conn.commit()
        except psycopg2.errors.ForeignKeyViolation:
            conn.rollback()
            conn.close()
            return jsonify({
                'error': ('Cannot delete this user: they have existing documents, reviews, '
                          'or audit records linked to their account. Disable the account instead.')
            }), 409

        conn.close()

        log_audit(user['user_id'], 'DELETE_USER', 'users', target_user_id,
                  f'Admin deleted user {target[1]}')

        return jsonify({
            'message': 'User deleted successfully',
            'user_id': target_user_id
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# RESET USER PASSWORD
# POST /admin/users/<user_id>/reset-password
# Admin only
# ------------------------------------------------------------
@admin_bp.route('/users/<int:target_user_id>/reset-password', methods=['POST'])
@jwt_required()
def reset_user_password(target_user_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    data          = request.get_json() or {}
    new_password  = data.get('new_password', '')

    if not new_password or len(new_password) < 6:
        return jsonify({'error': 'new_password is required and must be at least 6 characters'}), 400

    password_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT email FROM users WHERE user_id = %s', (target_user_id,))
        target = cursor.fetchone()
        if not target:
            conn.close()
            return jsonify({'error': 'User not found'}), 404

        cursor.execute(
            'UPDATE users SET password_hash = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s',
            (password_hash, target_user_id)
        )
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'RESET_PASSWORD', 'users', target_user_id,
                  f'Admin reset password for user {target[0]}')

        return jsonify({
            'message': 'Password reset successfully',
            'user_id': target_user_id
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
            # These records are always invoices — documents.status is the
            # only per-document workflow status in the schema; purchase_
            # orders/goods_receipts have no status column of their own and
            # remain visible via the existing Record Detail related-
            # documents view rather than as separate rows here.
            row['document_number'] = row.get('invoice_number') or row['file_name']
            row['document_type']   = 'invoice'
            result.append(row)

        return jsonify({
            'total':     len(result),
            'documents': result
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# ADMIN APPROVE DOCUMENT
# POST /admin/documents/<document_id>/approve
# Admin only
#
# A separate endpoint from Auditor's own POST /reviews/approve
# (routes/reviews.py, untouched) rather than broadening that route's
# role check — reuses the exact same documents.status / review_records
# / send_back_cycles transition it already performs.
# ------------------------------------------------------------
@admin_bp.route('/documents/<int:document_id>/approve', methods=['POST'])
@jwt_required()
def admin_approve_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    data    = request.get_json() or {}
    remarks = (data.get('remarks') or 'Approved by admin').strip()

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT status FROM documents WHERE document_id = %s', (document_id,))
        doc = cursor.fetchone()
        if not doc:
            conn.close()
            return jsonify({'error': 'Document not found'}), 404
        if doc[0] == 'approved':
            conn.close()
            return jsonify({'error': 'Document is already approved'}), 400

        cursor.execute(
            '''INSERT INTO review_records (document_id, reviewed_by, action, remarks)
               VALUES (%s, %s, 'approved', %s) RETURNING review_id''',
            (document_id, user['user_id'], remarks)
        )
        review_id = cursor.fetchone()[0]

        cursor.execute(
            "UPDATE documents SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (document_id,)
        )
        cursor.execute(
            "UPDATE exceptions SET is_resolved = TRUE, resolved_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (document_id,)
        )

        # Resolve any open Finance send-back cycle, mirroring the Auditor's
        # own approve() exactly.
        cursor.execute(
            '''SELECT cycle_id FROM send_back_cycles
               WHERE document_id = %s AND cycle_status = 'resubmitted'
               ORDER BY cycle_number DESC LIMIT 1''',
            (document_id,)
        )
        open_cycle = cursor.fetchone()
        if open_cycle:
            cursor.execute(
                '''UPDATE send_back_cycles
                   SET cycle_status = 'resolved', resolution = 'approved',
                       resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                   WHERE cycle_id = %s''',
                (open_cycle[0],)
            )

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'ADMIN_APPROVE_DOCUMENT', 'documents', document_id,
                  f'Admin approved document {document_id}')

        return jsonify({
            'message':     'Document approved successfully',
            'document_id': document_id,
            'review_id':   review_id,
            'status':      'approved'
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# ADMIN SEND BACK DOCUMENT (to Finance or to Auditor)
# POST /admin/documents/<document_id>/send-back
# Admin only
# Body: {"target": "finance" | "auditor", "reason": "...", "message": "..."}
#
# target=finance mirrors Auditor's own POST /reviews/return exactly
# (review_records action='returned', documents.status='returned') — the
# same valid review_records.action value the Auditor's return already
# uses, so no schema/constraint change is needed.
#
# target=auditor re-opens an already-decided document (documents.status
# -> 'under_review', which is what already puts it back in the
# Auditor's own review queue — GET /reviews/queue). There is no
# matching review_records.action for "re-opened for auditor" in that
# table's existing CHECK constraint (approved/returned/resubmitted/
# closed all mean something else), so this is recorded via the
# general-purpose audit_logs trail instead, without altering that
# constraint.
# ------------------------------------------------------------
@admin_bp.route('/documents/<int:document_id>/send-back', methods=['POST'])
@jwt_required()
def admin_send_back_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'admin':
        return jsonify({'error': 'Access denied. Admin only.'}), 403

    data    = request.get_json() or {}
    target  = (data.get('target') or '').strip().lower()
    reason  = (data.get('reason') or '').strip()
    message = (data.get('message') or '').strip()

    if target not in ('finance', 'auditor'):
        return jsonify({'error': "target must be 'finance' or 'auditor'"}), 400
    if not reason or not message:
        return jsonify({'error': 'reason and message are required'}), 400

    remarks = f'{reason}: {message}'

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT status FROM documents WHERE document_id = %s', (document_id,))
        doc = cursor.fetchone()
        if not doc:
            conn.close()
            return jsonify({'error': 'Document not found'}), 404

        new_status = 'returned' if target == 'finance' else 'under_review'
        if doc[0] == new_status:
            conn.close()
            return jsonify({'error': f'Document is already {new_status}'}), 400

        review_id = None
        if target == 'finance':
            cursor.execute(
                '''INSERT INTO review_records (document_id, reviewed_by, action, remarks)
                   VALUES (%s, %s, 'returned', %s) RETURNING review_id''',
                (document_id, user['user_id'], remarks)
            )
            review_id = cursor.fetchone()[0]

        cursor.execute(
            'UPDATE documents SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE document_id = %s',
            (new_status, document_id)
        )

        conn.commit()
        conn.close()

        action_name = 'ADMIN_SEND_BACK_TO_FINANCE' if target == 'finance' else 'ADMIN_SEND_BACK_TO_AUDITOR'
        log_audit(user['user_id'], action_name, 'documents', document_id,
                  f'Admin sent document {document_id} back to {target}. Reason: {reason}. Message: {message}')

        return jsonify({
            'message':     f'Document sent back to {target}',
            'document_id': document_id,
            'status':      new_status,
            'review_id':   review_id
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