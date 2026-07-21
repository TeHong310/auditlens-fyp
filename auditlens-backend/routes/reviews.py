import json
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit
from helpers.send_back import (
    validate_send_back_payload, validate_finance_response_payload,
    compute_activity_summary, is_overdue,
)

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

        # If this record went through a send-back correction cycle
        # (Finance resubmitted it), resolve that cycle as 'approved' —
        # the cycle row itself (reason/instruction/response) is never
        # overwritten, only its resolution/resolved_at are set.
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
# RETURN DOCUMENT (Send Back to Finance)
# POST /reviews/return/<document_id>
# Auditor only
#
# Accepts either:
#   - the NEW structured payload (Feature 1): reason_category,
#     instruction, required_actions[], priority, due_date — creates a
#     send_back_cycles row so the full reason/instruction/priority/due-
#     date survives this and every future cycle for this document.
#   - the LEGACY payload ({"remarks": "..."}) — kept working exactly as
#     before for backward compatibility; no cycle row is created since
#     there's no structured data to store for it.
# ------------------------------------------------------------
@reviews_bp.route('/return/<int:document_id>', methods=['POST'])
@jwt_required()
def return_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    data = request.get_json() or {}
    is_structured = 'reason_category' in data or 'instruction' in data

    cleaned = None
    if is_structured:
        errors, cleaned = validate_send_back_payload(data)
        if errors:
            return jsonify({'error': '; '.join(errors)}), 400
        remarks = cleaned['instruction']
    else:
        remarks = (data.get('remarks') or '').strip()
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

        cycle_number = None
        if cleaned:
            # A prior cycle only needs resolving if this document is
            # being sent back AGAIN after Finance already responded —
            # its reason/response are preserved, never overwritten.
            cursor.execute(
                '''SELECT cycle_id, cycle_number FROM send_back_cycles
                   WHERE document_id = %s ORDER BY cycle_number DESC LIMIT 1''',
                (document_id,)
            )
            prev = cursor.fetchone()
            cycle_number = (prev[1] + 1) if prev else 1
            if prev:
                cursor.execute(
                    '''UPDATE send_back_cycles
                       SET cycle_status = 'resolved', resolution = 'returned_again',
                           resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                       WHERE cycle_id = %s AND cycle_status != 'resolved' ''',
                    (prev[0],)
                )

            cursor.execute(
                '''INSERT INTO send_back_cycles
                   (document_id, cycle_number, return_reason_category, reason_other_note,
                    auditor_instruction, required_actions, required_action_other_note,
                    priority, response_due_date, sent_back_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING cycle_id''',
                (document_id, cycle_number, cleaned['reason_category'], cleaned['reason_other_note'],
                 cleaned['instruction'], json.dumps(cleaned['required_actions']),
                 cleaned['required_action_other_note'], cleaned['priority'], cleaned['due_date'],
                 user['user_id'])
            )

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'RETURN_DOCUMENT', 'documents', document_id,
                  f'Auditor returned document {document_id} with remarks: {remarks}')

        return jsonify({
            'message':      'Document returned to Finance for correction',
            'document_id':  document_id,
            'review_id':    review_id,
            'status':       'returned',
            'remarks':      remarks,
            'cycle_number': cycle_number,
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# FINANCE RESUBMIT DOCUMENT
# POST /reviews/resubmit/<document_id>
# Finance Executive only
#
# When the document has an OPEN send-back cycle (created by the new
# structured send-back flow), a written response is REQUIRED (Feature 3)
# and is saved onto that cycle — finance_response/finance_responded_by/
# finance_responded_at/resubmitted_by/resubmitted_at, cycle_status ->
# 'resubmitted'. Documents returned before this feature existed (no
# cycle row) fall back to the original optional-remarks behavior.
# ------------------------------------------------------------
@reviews_bp.route('/resubmit/<int:document_id>', methods=['POST'])
@jwt_required()
def resubmit_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied. Finance Executive only.'}), 403

    data = request.get_json() or {}

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute('SELECT status FROM documents WHERE document_id = %s', (document_id,))
        doc = cursor.fetchone()

        if not doc:
            conn.close()
            return jsonify({'error': 'Document not found'}), 404

        if doc['status'] != 'returned':
            conn.close()
            return jsonify({'error': f'Document is not returned. Current status: {doc["status"]}'}), 400

        cursor.execute(
            '''SELECT cycle_id FROM send_back_cycles
               WHERE document_id = %s AND cycle_status = 'action_required'
               ORDER BY cycle_number DESC LIMIT 1''',
            (document_id,)
        )
        open_cycle = cursor.fetchone()

        if open_cycle:
            errors, response_text = validate_finance_response_payload(data)
            if errors:
                conn.close()
                return jsonify({'error': '; '.join(errors)}), 400
        else:
            response_text = (data.get('response') or data.get('remarks') or '').strip() \
                or 'Resubmitted after correction'

        remarks = response_text

        cursor.execute(
            '''INSERT INTO review_records (document_id, reviewed_by, action, remarks)
               VALUES (%s, %s, %s, %s) RETURNING review_id''',
            (document_id, user['user_id'], 'resubmitted', remarks)
        )
        review_id = cursor.fetchone()['review_id']

        cursor.execute(
            "UPDATE documents SET status = 'resubmitted', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (document_id,)
        )

        if open_cycle:
            cursor.execute(
                '''UPDATE send_back_cycles
                   SET finance_response = %s, finance_responded_by = %s, finance_responded_at = CURRENT_TIMESTAMP,
                       resubmitted_by = %s, resubmitted_at = CURRENT_TIMESTAMP,
                       cycle_status = 'resubmitted', updated_at = CURRENT_TIMESTAMP
                   WHERE cycle_id = %s''',
                (response_text, user['user_id'], user['user_id'], open_cycle['cycle_id'])
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
# GET SEND-BACK CYCLE HISTORY
# GET /reviews/send-back-cycles/<document_id>
# Any authenticated user (same permissive pattern as /history/<id> above)
#
# Every cycle ever created for this document, oldest first — a record
# sent back multiple times returns every cycle, none overwritten. Each
# cycle is annotated with a timestamp-based `activity_summary` (Feature
# 4's "Changes Since Send Back" — never a fabricated field diff, only
# real stored timestamps compared against sent_back_at) and `is_overdue`.
# ------------------------------------------------------------
@reviews_bp.route('/send-back-cycles/<int:document_id>', methods=['GET'])
@jwt_required()
def get_send_back_cycles(document_id):
    user_id = get_jwt_identity()
    get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT sbc.*, u1.full_name AS sent_back_by_name,
                      u2.full_name AS finance_responded_by_name,
                      u3.full_name AS resubmitted_by_name
               FROM send_back_cycles sbc
               JOIN users u1 ON sbc.sent_back_by = u1.user_id
               LEFT JOIN users u2 ON sbc.finance_responded_by = u2.user_id
               LEFT JOIN users u3 ON sbc.resubmitted_by = u3.user_id
               WHERE sbc.document_id = %s
               ORDER BY sbc.cycle_number ASC''',
            (document_id,)
        )
        cycles = cursor.fetchall()

        cursor.execute('SELECT edited_at FROM extracted_fields WHERE document_id = %s', (document_id,))
        ef_row = cursor.fetchone()
        invoice_edited_at = ef_row['edited_at'] if ef_row else None

        cursor.execute(
            'SELECT uploaded_at FROM purchase_orders WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
        po_row = cursor.fetchone()
        po_uploaded_at = po_row['uploaded_at'] if po_row else None

        cursor.execute(
            'SELECT uploaded_at FROM goods_receipts WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
        gr_row = cursor.fetchone()
        gr_uploaded_at = gr_row['uploaded_at'] if gr_row else None

        conn.close()

        result = []
        for c in cycles:
            row = dict(c)
            row['activity_summary'] = compute_activity_summary(row, invoice_edited_at, po_uploaded_at, gr_uploaded_at)
            row['is_overdue'] = is_overdue(row)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({'document_id': document_id, 'cycles': result}), 200

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