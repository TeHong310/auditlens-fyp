import json
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit
from helpers.transaction_packages import get_transaction_context_for_document, get_package_documents
from helpers.send_back import (
    validate_send_back_payload, validate_finance_response_payload,
    compute_activity_summary, is_overdue,
)
from helpers.time_format import serialize_row_datetimes, to_utc_iso
from helpers.duplicate_resolution import get_suspected_original, WITHDRAWN_DUPLICATE_STATUS
from routes.auditor import build_comparison, _matching_status_for_comparison

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

        # Approving completes the transaction review — any anomaly still
        # 'pending' for this invoice is marked 'reviewed' as PART OF that
        # decision (the auditor has now looked at the whole case,
        # including any recorded finding). This never touches an anomaly
        # already 'reviewed' or 'dismissed' — every anomaly row, and its
        # full detected_pattern/ai_explanation history, is preserved
        # exactly as-is; only status/reviewed_by/reviewed_at change on
        # the ones that were still pending. See routes/ai_assistant.py's
        # _classify_anomaly — 'reviewed' is never treated as "cleared",
        # so this does not silently make the finding disappear anywhere
        # it's displayed.
        cursor.execute(
            '''UPDATE anomalies SET status = 'reviewed', reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP
               WHERE invoice_document_id = %s AND status = 'pending' ''',
            (user['user_id'], document_id)
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

        # Sending back deliberately does NOT touch the anomalies table —
        # the issue that prompted this return is not resolved, so any
        # 'pending' anomaly for this invoice stays exactly 'pending'
        # (unlike approve_document, which marks pending anomalies
        # 'reviewed'). Anomalies already 'reviewed'/'dismissed' are also
        # left untouched — full history preserved either way.
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
# MARK DOCUMENT AS NEEDING FURTHER REVIEW
# POST /reviews/need-review/<document_id>
# Auditor only
#
# The third of the Auditor's 3 final-decision controls on Record Detail
# (Approve / Send Back / Need Review). Unlike Approve/Send Back, this is
# not a workflow-ending disposition — the document stays exactly where
# it is (still 'under_review'/'resubmitted', still actionable) and
# anomalies stay exactly as they are ('pending' ones stay pending, the
# same "the issue is not resolved" reasoning as Send Back). It only
# records, in the SAME review_records audit trail Approve/Send Back
# already use, that the auditor looked at this case and flagged it for
# further investigation rather than deciding yet.
# ------------------------------------------------------------
@reviews_bp.route('/need-review/<int:document_id>', methods=['POST'])
@jwt_required()
def need_review_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    data    = request.get_json() or {}
    remarks = (data.get('remarks') or '').strip() or 'Marked for further review by auditor'

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT status FROM documents WHERE document_id = %s', (document_id,))
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
            (document_id, user['user_id'], 'need_review', remarks)
        )
        review_id = cursor.fetchone()[0]

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'NEED_REVIEW_DOCUMENT', 'documents', document_id,
                  f'Auditor marked document {document_id} as needing further review: {remarks}')

        return jsonify({
            'message':     'Document marked as needing further review',
            'document_id': document_id,
            'review_id':   review_id,
            'remarks':     remarks
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# MARK A GUIDED REVIEW STEP AS REVIEWED (Audit Review page)
# POST /reviews/review-steps/<document_id>/<step>
# Auditor only
#
# Backs the Audit Review page's sequential checklist — Three-Way
# Matching -> Exception Review -> Authenticity Review -> Anomaly
# Review. Deliberately separate from review_records (the document-
# level Approve/Send Back/Need Review DECISION, untouched by this) -
# this tracks per-step review PROGRESS, upserted into document_
# review_steps (UNIQUE(document_id, step), so re-marking an already-
# reviewed step just updates reviewed_by/reviewed_at rather than
# creating a duplicate row). The sequential order is enforced HERE,
# not only hidden client-side, so a step can't be marked out of order
# via a direct API call either.
# ------------------------------------------------------------
REVIEW_STEP_ORDER = ['three_way_matching', 'exception_review', 'authenticity_review', 'anomaly_review']


@reviews_bp.route('/review-steps/<int:document_id>/<step>', methods=['POST'])
@jwt_required()
def mark_review_step(document_id, step):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    if step not in REVIEW_STEP_ORDER:
        return jsonify({'error': f'step must be one of {REVIEW_STEP_ORDER}'}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute('SELECT document_id FROM documents WHERE document_id = %s', (document_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Document not found'}), 404

        step_index = REVIEW_STEP_ORDER.index(step)
        if step_index > 0:
            required_prior = REVIEW_STEP_ORDER[:step_index]
            cursor.execute(
                'SELECT step FROM document_review_steps WHERE document_id = %s AND step = ANY(%s)',
                (document_id, required_prior)
            )
            done = {row['step'] for row in cursor.fetchall()}
            missing = [s for s in required_prior if s not in done]
            if missing:
                conn.close()
                return jsonify({
                    'error': f"Complete \"{missing[0].replace('_', ' ').title()}\" before marking this step as reviewed."
                }), 400

        cursor.execute(
            '''INSERT INTO document_review_steps (document_id, step, reviewed_by, reviewed_at)
               VALUES (%s, %s, %s, NOW())
               ON CONFLICT (document_id, step) DO UPDATE
                   SET reviewed_by = EXCLUDED.reviewed_by, reviewed_at = NOW()
               RETURNING reviewed_at''',
            (document_id, step, user['user_id'])
        )
        reviewed_at = cursor.fetchone()['reviewed_at']
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'MARK_REVIEW_STEP', 'documents', document_id,
                  f'Auditor marked "{step}" as reviewed for document {document_id}')

        return jsonify({
            'step':          step,
            'reviewed_by':   user['user_id'],
            'reviewer_name': user['full_name'],
            'reviewed_at':   to_utc_iso(reviewed_at),
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
# GET DUPLICATE-FINDING CONTEXT — the "suspected original" invoice for
# a document that was returned as a possible duplicate. Any
# authenticated user (same permissive pattern as /history/<id> and
# /send-back-cycles/<id> above) — read-only, Finance Correction Detail
# uses it for "View Suspected Original".
# GET /reviews/duplicate-suspect/<document_id>
# ------------------------------------------------------------
@reviews_bp.route('/duplicate-suspect/<int:document_id>', methods=['GET'])
@jwt_required()
def get_duplicate_suspect(document_id):
    user_id = get_jwt_identity()
    get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        suspected_original = get_suspected_original(cursor, document_id)
        conn.close()

        return jsonify({
            'document_id':        document_id,
            'suspected_original': suspected_original,
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# WITHDRAW THIS DUPLICATE — Finance confirms this returned invoice
# really is a duplicate of another already-valid transaction and
# withdraws it, WITHOUT sending it back to the auditor for another
# look (unlike Not a Duplicate/resubmit, which goes back through the
# normal audit cycle). Only valid for a cycle the auditor explicitly
# returned with reason_category='possible_duplicate_invoice' - using
# this for any other return reason is rejected, so the existing field-
# correction/PO-GR-replacement/resubmit flow stays the ONLY path for
# every other send-back reason (unchanged).
#
# Effects (none of them delete a row - see helpers/duplicate_
# resolution.py's WITHDRAWN_DUPLICATE_STATUS docstring):
#   - documents.status -> 'withdrawn_duplicate' (this document only;
#     the suspected original is never touched, so it stays exactly the
#     valid transaction it already was)
#   - the open send_back_cycles row -> cycle_status='resolved',
#     resolution='withdrawn_duplicate' (closes the correction case)
#   - a review_records row (action='closed') + an audit_logs row,
#     both carrying the acting Finance user's id and a real timestamp -
#     the same two places every other decision in this app (approve/
#     return/resubmit) already gets logged, so this shows up in Review
#     History exactly like any other case-closing action.
# POST /reviews/withdraw-duplicate/<document_id>
# Body (optional): {"note": "..."}
# Finance Executive only
# ------------------------------------------------------------
@reviews_bp.route('/withdraw-duplicate/<int:document_id>', methods=['POST'])
@jwt_required()
def withdraw_duplicate(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied. Finance Executive only.'}), 403

    data = request.get_json() or {}
    note = (data.get('note') or '').strip() or 'Finance confirmed this invoice is a duplicate submission and withdrew it.'

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
            '''SELECT cycle_id, return_reason_category FROM send_back_cycles
               WHERE document_id = %s AND cycle_status = 'action_required'
               ORDER BY cycle_number DESC LIMIT 1''',
            (document_id,)
        )
        open_cycle = cursor.fetchone()
        if not open_cycle or open_cycle['return_reason_category'] != 'possible_duplicate_invoice':
            conn.close()
            return jsonify({'error': 'This action is only available when the auditor returned this '
                                      'document as a possible duplicate invoice.'}), 400

        cursor.execute(
            "UPDATE documents SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (WITHDRAWN_DUPLICATE_STATUS, document_id)
        )

        cursor.execute(
            '''UPDATE send_back_cycles
               SET finance_response = %s, finance_responded_by = %s, finance_responded_at = CURRENT_TIMESTAMP,
                   cycle_status = 'resolved', resolution = %s, resolved_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE cycle_id = %s''',
            (note, user['user_id'], WITHDRAWN_DUPLICATE_STATUS, open_cycle['cycle_id'])
        )

        cursor.execute(
            '''INSERT INTO review_records (document_id, reviewed_by, action, remarks)
               VALUES (%s, %s, 'closed', %s) RETURNING review_id''',
            (document_id, user['user_id'], note)
        )
        review_id = cursor.fetchone()['review_id']

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'WITHDRAW_DUPLICATE', 'documents', document_id,
                  f'Finance withdrew document {document_id} as a duplicate submission: {note}')

        return jsonify({
            'message':     'Duplicate withdrawn. This correction case is now closed.',
            'document_id': document_id,
            'review_id':   review_id,
            'status':      WITHDRAWN_DUPLICATE_STATUS,
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
            serialize_row_datetimes(row)
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
            serialize_row_datetimes(row)
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

        # Phase 8 (Bug 1): submission operates at the transaction package
        # level. If this invoice belongs to a Finance Transaction
        # Package (Phase 5), every SIBLING invoice in that same package
        # is submitted too, so a package doesn't end up half-visible to
        # the Auditor (one invoice under_review, its sibling still
        # sitting in Finance's queue). Only siblings in the same
        # submittable state ('ocr_done'/'returned') are touched — never
        # regresses a sibling that's already further along (e.g.
        # already under_review/approved) or forces one still mid-OCR.
        # Reuses the existing Phase 6 helpers unchanged; no new
        # matching/relationship logic, no duplicate documents, no new
        # package.
        submitted_siblings = []
        context = get_transaction_context_for_document(document_id, 'invoice')
        if context:
            docs = get_package_documents(context['transaction_package_id'])
            sibling_ids = [inv['document_id'] for inv in docs['invoices'] if inv['document_id'] != document_id]
            if sibling_ids:
                conn = get_db_connection()
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cursor.execute(
                    "UPDATE documents SET status = 'under_review' "
                    "WHERE document_id = ANY(%s) AND status IN ('ocr_done', 'returned') "
                    "RETURNING document_id",
                    (sibling_ids,)
                )
                submitted_siblings = [row['document_id'] for row in cursor.fetchall()]
                conn.commit()
                conn.close()
                for sibling_id in submitted_siblings:
                    log_audit(user['user_id'], 'SUBMIT_FOR_REVIEW', 'documents', sibling_id,
                              f'Document submitted for auditor review (transaction package {context["transaction_package_id"]}, '
                              f'synchronized with document {document_id})')

        return jsonify({
            'message': 'Document submitted for review successfully',
            'submitted_sibling_document_ids': submitted_siblings,
        }), 200

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

        # purchase_order_number/goods_receipt_number: the SAME document_id-
        # keyed linkage routes/auditor.py::build_comparison() already uses
        # for Three-Way Matching (purchase_orders/goods_receipts rows
        # carry a document_id FK back to the invoice, latest row wins if
        # more than one was ever uploaded) - reused here directly via a
        # LATERAL join instead of calling build_comparison() per row here
        # too (that happens just below, for overall_status only).
        #
        # record_matches (previously joined here for match_score/
        # overall_status) is dead: nothing in the current codebase INSERTs
        # into it anymore (routes/matching.py's run_matching() - the only
        # write path left - writes to three_way_matches, a different
        # table; record_matches only ever appears in DELETE cleanup
        # statements now). That's why overall_status always came back
        # NULL here regardless of whether PO/GR were actually uploaded,
        # which the frontend's fallback then misread as "Missing
        # Documents". Dropped the join entirely.
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
                rr.action,
                rr.remarks AS comments,
                rr.reviewed_at,
                po.po_number AS purchase_order_number,
                gr.gr_number AS goods_receipt_number
            FROM documents d
            LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
            LEFT JOIN review_records rr ON d.document_id = rr.document_id
            LEFT JOIN LATERAL (
                SELECT po_number FROM purchase_orders
                WHERE document_id = d.document_id
                ORDER BY uploaded_at DESC LIMIT 1
            ) po ON true
            LEFT JOIN LATERAL (
                SELECT gr_number FROM goods_receipts
                WHERE document_id = d.document_id
                ORDER BY uploaded_at DESC LIMIT 1
            ) gr ON true
            WHERE d.uploaded_by = %s AND d.status != 'withdrawn_duplicate'
            ORDER BY d.uploaded_at DESC
        ''', (user['user_id'],))

        documents = cursor.fetchall()

        # overall_status: the REAL, currently-active matching result -
        # the exact same build_comparison()/_matching_status_for_
        # comparison() every other matching-aware page (Record Detail,
        # Matching Details, Evidence Passport, the Auditor queue's own
        # standalone-invoice rows) already reads, returning one of
        # 'PASS'/'REVIEW'/'PARTIAL'/'FAIL', or 'PENDING' when no
        # comparison exists yet. No new matching logic - this calls the
        # existing engine, once per document (bounded to this one
        # Finance user's own uploads).
        result = []
        for doc in documents:
            row = dict(doc)
            comparison = build_comparison(cursor, row['document_id'])
            row['overall_status'] = _matching_status_for_comparison(comparison) if comparison else 'PENDING'
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
                elif hasattr(v, '__float__'):
                    row[k] = float(v)
            result.append(row)

        conn.close()

        return jsonify({'documents': result}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500