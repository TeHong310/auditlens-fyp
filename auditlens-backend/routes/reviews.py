from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit

reviews_bp = Blueprint('reviews', __name__)

# ------------------------------------------------------------
# GET REVIEW QUEUE (Auditor Dashboard)
# GET /reviews/queue
# Auditor only
# ------------------------------------------------------------
@reviews_bp.route('/queue', methods=['GET'])
@jwt_required()
def get_review_queue():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT DISTINCT ON (d.document_id)
                d.document_id,
                d.file_name,
                d.status,
                d.uploaded_at,
                u.full_name AS uploaded_by,
                ef.invoice_number,
                ef.vendor_name,
                ef.invoice_date,
                ef.total_amount,
                ef.ocr_confidence,
                rm.match_score,
                rm.overall_status AS match_status
               FROM documents d
               JOIN users u ON d.uploaded_by = u.user_id
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               LEFT JOIN record_matches rm ON d.document_id = rm.document_id
               WHERE d.status IN ('under_review', 'resubmitted')
               ORDER BY d.document_id, rm.matched_at DESC'''
        )
        queue = cursor.fetchall()
        conn.close()

        result = []
        for q in queue:
            row = dict(q)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({
            'total': len(result),
            'queue': result
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# APPROVE DOCUMENT
# POST /reviews/approve/<document_id>
# Auditor only
# ------------------------------------------------------------
@reviews_bp.route('/approve/<int:document_id>', methods=['POST'])
@jwt_required()
def approve_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    data    = request.get_json() or {}
    remarks = data.get('remarks', 'Approved by auditor')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT status FROM documents WHERE document_id = %s',
            (document_id,)
        )
        doc = cursor.fetchone()

        if not doc:
            conn.close()
            return jsonify({'error': 'Document not found'}), 404

        if doc[0] not in ('under_review', 'resubmitted'):
            conn.close()
            return jsonify({'error': f'Document is not under review. Current status: {doc[0]}'}), 400

        cursor.execute(
            '''INSERT INTO review_records (document_id, reviewed_by, action, remarks)
               VALUES (%s, %s, %s, %s) RETURNING review_id''',
            (document_id, user['user_id'], 'approved', remarks)
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

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'APPROVE_DOCUMENT', 'documents', document_id,
                  f'Auditor approved document {document_id}')

        return jsonify({
            'message':     'Document approved successfully',
            'document_id': document_id,
            'review_id':   review_id,
            'status':      'approved',
            'remarks':     remarks
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# RETURN DOCUMENT
# POST /reviews/return/<document_id>
# Auditor only
# ------------------------------------------------------------
@reviews_bp.route('/return/<int:document_id>', methods=['POST'])
@jwt_required()
def return_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    data    = request.get_json() or {}
    remarks = data.get('remarks', '')

    if not remarks:
        return jsonify({'error': 'Remarks are required when returning a document'}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT status FROM documents WHERE document_id = %s',
            (document_id,)
        )
        doc = cursor.fetchone()

        if not doc:
            conn.close()
            return jsonify({'error': 'Document not found'}), 404

        if doc[0] not in ('under_review', 'resubmitted'):
            conn.close()
            return jsonify({'error': f'Document is not under review. Current status: {doc[0]}'}), 400

        cursor.execute(
            '''INSERT INTO review_records (document_id, reviewed_by, action, remarks)
               VALUES (%s, %s, %s, %s) RETURNING review_id''',
            (document_id, user['user_id'], 'returned', remarks)
        )
        review_id = cursor.fetchone()[0]

        cursor.execute(
            "UPDATE documents SET status = 'returned', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (document_id,)
        )

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'RETURN_DOCUMENT', 'documents', document_id,
                  f'Auditor returned document {document_id} with remarks: {remarks}')

        return jsonify({
            'message':     'Document returned to Finance for correction',
            'document_id': document_id,
            'review_id':   review_id,
            'status':      'returned',
            'remarks':     remarks
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# FINANCE RESUBMIT DOCUMENT
# POST /reviews/resubmit/<document_id>
# Finance Executive only
# ------------------------------------------------------------
@reviews_bp.route('/resubmit/<int:document_id>', methods=['POST'])
@jwt_required()
def resubmit_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied. Finance Executive only.'}), 403

    data    = request.get_json() or {}
    remarks = data.get('remarks', 'Resubmitted after correction')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT status FROM documents WHERE document_id = %s',
            (document_id,)
        )
        doc = cursor.fetchone()

        if not doc:
            conn.close()
            return jsonify({'error': 'Document not found'}), 404

        if doc[0] != 'returned':
            conn.close()
            return jsonify({'error': f'Document is not returned. Current status: {doc[0]}'}), 400

        cursor.execute(
            '''INSERT INTO review_records (document_id, reviewed_by, action, remarks)
               VALUES (%s, %s, %s, %s) RETURNING review_id''',
            (document_id, user['user_id'], 'resubmitted', remarks)
        )
        review_id = cursor.fetchone()[0]

        cursor.execute(
            "UPDATE documents SET status = 'resubmitted', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (document_id,)
        )

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'RESUBMIT_DOCUMENT', 'documents', document_id,
                  f'Finance resubmitted document {document_id}')

        return jsonify({
            'message':     'Document resubmitted for review',
            'document_id': document_id,
            'review_id':   review_id,
            'status':      'resubmitted',
            'remarks':     remarks
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET REVIEW HISTORY
# GET /reviews/history/<document_id>
# ------------------------------------------------------------
@reviews_bp.route('/history/<int:document_id>', methods=['GET'])
@jwt_required()
def get_review_history(document_id):
    user_id = get_jwt_identity()
    get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT rr.*, u.full_name AS reviewer_name, u.role AS reviewer_role
               FROM review_records rr
               JOIN users u ON rr.reviewed_by = u.user_id
               WHERE rr.document_id = %s
               ORDER BY rr.reviewed_at ASC''',
            (document_id,)
        )
        history = cursor.fetchall()
        conn.close()

        result = []
        for h in history:
            row = dict(h)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({
            'document_id': document_id,
            'total':       len(result),
            'history':     result
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET FINANCE DASHBOARD
# GET /reviews/finance-dashboard
# Finance Executive only
# ------------------------------------------------------------
@reviews_bp.route('/finance-dashboard', methods=['GET'])
@jwt_required()
def finance_dashboard():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied. Finance Executive only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT
                COUNT(*) FILTER (WHERE status = 'ocr_done')     AS pending_submission,
                COUNT(*) FILTER (WHERE status = 'under_review') AS under_review,
                COUNT(*) FILTER (WHERE status = 'returned')     AS returned,
                COUNT(*) FILTER (WHERE status = 'approved')     AS approved,
                COUNT(*) FILTER (WHERE status = 'resubmitted')  AS resubmitted,
                COUNT(*)                                         AS total
               FROM documents
               WHERE uploaded_by = %s''',
            (user['user_id'],)
        )
        stats = cursor.fetchone()

        cursor.execute(
            '''SELECT DISTINCT ON (d.document_id)
                      d.document_id, d.file_name, d.status, d.updated_at,
                      ef.invoice_number, ef.vendor_name, ef.total_amount,
                      rr.remarks AS return_remarks
               FROM documents d
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               LEFT JOIN review_records rr ON d.document_id = rr.document_id
               WHERE d.uploaded_by = %s AND d.status = 'returned'
               ORDER BY d.document_id, rr.reviewed_at DESC''',
            (user['user_id'],)
        )
        returned_docs = cursor.fetchall()
        conn.close()

        result_returned = []
        for d in returned_docs:
            row = dict(d)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result_returned.append(row)

        return jsonify({
            'statistics':         dict(stats),
            'returned_documents': result_returned
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET AUDITOR DASHBOARD
# GET /reviews/auditor-dashboard
# Auditor only
# ------------------------------------------------------------
@reviews_bp.route('/auditor-dashboard', methods=['GET'])
@jwt_required()
def auditor_dashboard():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT
                COUNT(*) FILTER (WHERE status = 'under_review') AS pending_review,
                COUNT(*) FILTER (WHERE status = 'resubmitted')  AS resubmitted,
                COUNT(*) FILTER (WHERE status = 'approved')     AS approved,
                COUNT(*) FILTER (WHERE status = 'returned')     AS returned,
                COUNT(*)                                         AS total
               FROM documents'''
        )
        stats = cursor.fetchone()

        cursor.execute(
            '''SELECT DISTINCT ON (d.document_id)
                d.document_id,
                d.file_name,
                d.status,
                d.uploaded_at,
                u.full_name AS uploaded_by,
                ef.invoice_number,
                ef.vendor_name,
                ef.total_amount,
                rm.match_score,
                rm.overall_status AS match_status
               FROM documents d
               JOIN users u ON d.uploaded_by = u.user_id
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               LEFT JOIN record_matches rm ON d.document_id = rm.document_id
               WHERE d.status IN ('under_review', 'resubmitted')
               AND rm.overall_status IN ('mismatch', 'partial')
               ORDER BY d.document_id, rm.matched_at DESC'''
        )
        high_priority = cursor.fetchall()
        conn.close()

        result_priority = []
        for d in high_priority:
            row = dict(d)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result_priority.append(row)

        return jsonify({
            'statistics':          dict(stats),
            'high_priority_cases': result_priority
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# SUBMIT DOCUMENT TO AUDITOR
# POST /reviews/submit/<document_id>
# Finance Executive only
# ------------------------------------------------------------
@reviews_bp.route('/submit/<int:document_id>', methods=['POST'])
@jwt_required()
def submit_for_review(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            'SELECT * FROM documents WHERE document_id = %s AND uploaded_by = %s',
            (document_id, user['user_id'])
        )
        doc = cursor.fetchone()

        if not doc:
            conn.close()
            return jsonify({'error': 'Document not found'}), 404

        if doc['status'] not in ['ocr_done', 'returned']:
            conn.close()
            return jsonify({'error': 'Document cannot be submitted at this stage'}), 400

        cursor.execute(
            "UPDATE documents SET status = 'under_review' WHERE document_id = %s",
            (document_id,)
        )
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'SUBMIT_FOR_REVIEW', 'documents', document_id,
                  'Document submitted for auditor review')

        return jsonify({'message': 'Document submitted for review successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET FINANCE REPORT
# GET /reviews/finance-report
# Finance Executive only
# ------------------------------------------------------------
@reviews_bp.route('/finance-report', methods=['GET'])
@jwt_required()
def finance_report():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute('''
            SELECT
                d.document_id,
                d.file_name,
                d.file_type,
                d.status,
                d.uploaded_at,
                ef.invoice_number,
                ef.vendor_name,
                ef.invoice_date,
                ef.total_amount,
                ef.tax_amount,
                ef.currency,
                ef.ocr_confidence,
                rm.match_score,
                rm.overall_status,
                rr.action,
                rr.remarks AS comments,
                rr.reviewed_at
            FROM documents d
            LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
            LEFT JOIN record_matches rm ON ef.extraction_id = rm.extraction_id
            LEFT JOIN review_records rr ON d.document_id = rr.document_id
            WHERE d.uploaded_by = %s
            ORDER BY d.uploaded_at DESC
        ''', (user['user_id'],))

        documents = cursor.fetchall()
        conn.close()

        result = []
        for doc in documents:
            row = dict(doc)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
                elif hasattr(v, '__float__'):
                    row[k] = float(v)
            result.append(row)

        return jsonify({'documents': result}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500