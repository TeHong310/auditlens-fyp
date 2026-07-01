import re
import csv
import io
from datetime import datetime, timedelta, date
from flask import Blueprint, jsonify, request, Response
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id

auditor_bp = Blueprint('auditor', __name__)


def _normalize_vendor(name):
    if not name:
        return ''
    v = name.lower()
    v = re.sub(r'[.,()]', '', v)
    v = re.sub(r'\bsdn\s*bhd\b', '', v)
    v = re.sub(r'\bberhad\b', '', v)
    v = re.sub(r'\s+', ' ', v).strip()
    return v


def _amounts_equal(a, b):
    if a is None or b is None:
        return None
    try:
        return abs(float(a) - float(b)) < 0.01
    except (TypeError, ValueError):
        return None


def _build_comparison(cursor, invoice_document_id):
    """Shared by GET /record/<id>/comparison and the exceptions detector,
    so match logic only lives in one place. Returns None if the invoice
    document doesn't exist."""
    cursor.execute(
        '''SELECT d.document_id, d.file_name, d.uploaded_at,
                  ef.invoice_number, ef.vendor_name, ef.invoice_date,
                  ef.total_amount, ef.ocr_confidence
           FROM documents d
           LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
           WHERE d.document_id = %s''',
        (invoice_document_id,)
    )
    inv_row = cursor.fetchone()
    if not inv_row:
        return None

    cursor.execute(
        '''SELECT po_id, file_name, po_number, vendor_name, po_date, total_amount
           FROM purchase_orders WHERE document_id = %s
           ORDER BY uploaded_at DESC LIMIT 1''',
        (invoice_document_id,)
    )
    po_row = cursor.fetchone()

    cursor.execute(
        '''SELECT gr_id, file_name, gr_number, vendor_name, receipt_date
           FROM goods_receipts WHERE document_id = %s
           ORDER BY uploaded_at DESC LIMIT 1''',
        (invoice_document_id,)
    )
    gr_row = cursor.fetchone()

    invoice = {
        'document_id':    inv_row['document_id'],
        'filename':       inv_row['file_name'],
        'ocr_confidence': float(inv_row['ocr_confidence']) if inv_row['ocr_confidence'] is not None else None,
        'invoice_no':     inv_row['invoice_number'],
        'vendor_name':    inv_row['vendor_name'],
        'invoice_date':   inv_row['invoice_date'].isoformat() if inv_row['invoice_date'] else None,
        'total_amount':   float(inv_row['total_amount']) if inv_row['total_amount'] is not None else None,
        'uploaded_at':    inv_row['uploaded_at'].isoformat() if inv_row['uploaded_at'] else None,
    }

    po = None
    if po_row:
        po = {
            'po_id':        po_row['po_id'],
            'filename':     po_row['file_name'],
            'po_no':        po_row['po_number'],
            'vendor_name':  po_row['vendor_name'],
            'po_date':      po_row['po_date'].isoformat() if po_row['po_date'] else None,
            'total_amount': float(po_row['total_amount']) if po_row['total_amount'] is not None else None,
        }

    gr = None
    if gr_row:
        gr = {
            'gr_id':        gr_row['gr_id'],
            'filename':     gr_row['file_name'],
            'gr_no':        gr_row['gr_number'],
            'vendor_name':  gr_row['vendor_name'],
            'receipt_date': gr_row['receipt_date'].isoformat() if gr_row['receipt_date'] else None,
        }

    # ── Vendor match: compare normalized vendor_name across every
    # present doc (invoice always present; PO/GR only if uploaded) ──
    vendor_names = [invoice['vendor_name']]
    if po:
        vendor_names.append(po['vendor_name'])
    if gr:
        vendor_names.append(gr['vendor_name'])
    normalized = [_normalize_vendor(v) for v in vendor_names if v]
    vendor_match = len(set(normalized)) <= 1 if normalized else None

    # ── Amount match: Invoice vs PO only (GR carries no monetary
    # total by design) ──
    amount_match = _amounts_equal(invoice['total_amount'], po['total_amount']) if po else None

    # ── PO reference match: Invoice/PO/GR are linked by a document_id
    # foreign key at upload time (not by OCR-extracted PO-number
    # cross-references, which don't exist as separate fields on the
    # invoice or GR OCR data), so any PO/GR returned here is already
    # guaranteed to belong to this invoice by construction. Treated as
    # not-applicable (no independent field to compare) rather than
    # invented/hardcoded. ──
    po_reference_match = None

    # ── Date order: PO date <= GR date <= Invoice date, skipping the
    # check if any required date is missing ──
    date_order_valid = None
    if po and gr and po['po_date'] and gr['receipt_date'] and invoice['invoice_date']:
        date_order_valid = po['po_date'] <= gr['receipt_date'] <= invoice['invoice_date']
    elif po and invoice['invoice_date'] and po['po_date'] and not gr:
        date_order_valid = po['po_date'] <= invoice['invoice_date']
    elif gr and invoice['invoice_date'] and gr['receipt_date'] and not po:
        date_order_valid = gr['receipt_date'] <= invoice['invoice_date']

    checks = [vendor_match, amount_match, date_order_valid]
    applicable_checks = [c for c in checks if c is not None]

    if any(c is False for c in checks):
        overall_status = 'FAIL'
    elif not po or not gr:
        overall_status = 'PARTIAL'
    elif applicable_checks and all(applicable_checks):
        overall_status = 'PASS'
    else:
        overall_status = 'PARTIAL'

    return {
        'invoice': invoice,
        'po': po,
        'gr': gr,
        'match_result': {
            'vendor_match':        vendor_match,
            'amount_match':        amount_match,
            'po_reference_match':  po_reference_match,
            'date_order_valid':    date_order_valid,
            'overall_status':      overall_status,
        }
    }


# ------------------------------------------------------------
# GET FULL 3-WAY COMPARISON FOR AN INVOICE RECORD
# GET /auditor/record/<invoice_document_id>/comparison
# Auditor only
# ------------------------------------------------------------
@auditor_bp.route('/record/<int:invoice_document_id>/comparison', methods=['GET'])
@jwt_required()
def get_record_comparison(invoice_document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        result = _build_comparison(cursor, invoice_document_id)
        conn.close()

        if result is None:
            return jsonify({'error': 'Invoice document not found'}), 404

        return jsonify(result), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# EXCEPTION DETECTION
# Classifies one invoice's comparison result + document row into at
# most one exception (highest severity wins), or None if clean.
# Severity order: mismatch > sent_back = missing_document > low_confidence
# ------------------------------------------------------------
def _classify_exception(cursor, doc_row, comparison):
    candidates = []  # (rank, type, label, detail, severity)
    mr = comparison['match_result']

    if mr['overall_status'] == 'FAIL':
        parts = []
        label_parts = []
        if mr['vendor_match'] is False:
            parts.append('Vendor names differ')
            label_parts.append('Vendor')
        if mr['amount_match'] is False:
            inv_amt = comparison['invoice']['total_amount']
            po_amt  = comparison['po']['total_amount'] if comparison['po'] else None
            parts.append(f"Amount differs: Invoice RM{inv_amt} vs PO RM{po_amt}")
            label_parts.append('Amount')
        if mr['date_order_valid'] is False:
            parts.append('Document dates out of expected order')
            label_parts.append('Date')
        label = (' & '.join(label_parts) + ' Mismatch') if label_parts else 'Mismatch'
        detail = '; '.join(parts) or 'Fields do not match'
        candidates.append((4, 'mismatch', label, detail, 'high'))

    if doc_row['status'] == 'returned':
        cursor.execute(
            '''SELECT remarks FROM review_records
               WHERE document_id = %s AND action = 'returned'
               ORDER BY reviewed_at DESC LIMIT 1''',
            (doc_row['document_id'],)
        )
        remark_row = cursor.fetchone()
        detail = remark_row['remarks'] if remark_row and remark_row['remarks'] else 'Sent back to Finance for correction'
        candidates.append((3, 'sent_back', 'Sent Back to Finance', detail, 'medium'))

    if not comparison['po'] or not comparison['gr']:
        missing = []
        if not comparison['po']:
            missing.append('PO')
        if not comparison['gr']:
            missing.append('GR')
        label = 'Missing ' + ' and '.join(missing)
        candidates.append((3, 'missing_document', label,
                            f"Invoice uploaded but {' and '.join(missing)} not yet received", 'medium'))

    ocr_confidence = comparison['invoice']['ocr_confidence']
    if ocr_confidence is not None and ocr_confidence < 80:
        pct = round(ocr_confidence)
        candidates.append((1, 'low_confidence', f'Low OCR Confidence ({pct}%)',
                            f'OCR confidence {pct}% — verify extracted fields', 'low'))

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[0])
    return candidates[0]


# ------------------------------------------------------------
# GET EXCEPTIONS LIST
# GET /auditor/exceptions
# Auditor only
# ------------------------------------------------------------
@auditor_bp.route('/exceptions', methods=['GET'])
@jwt_required()
def get_exceptions():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    type_filter = request.args.get('type', 'all')
    limit  = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Scope: invoices still "in flight" for audit — under review,
        # resubmitted after correction, or just sent back (so the
        # sent_back exception type itself has something to surface).
        # Already-approved invoices are excluded; they're resolved.
        cursor.execute(
            '''SELECT document_id, uploaded_at, status
               FROM documents
               WHERE status IN ('under_review', 'resubmitted', 'returned')
               ORDER BY uploaded_at DESC'''
        )
        doc_rows = cursor.fetchall()

        exceptions = []
        for doc_row in doc_rows:
            comparison = _build_comparison(cursor, doc_row['document_id'])
            if not comparison:
                continue
            classified = _classify_exception(cursor, doc_row, comparison)
            if not classified:
                continue
            _, exc_type, label, detail, severity = classified

            if type_filter != 'all' and exc_type != type_filter:
                continue

            exceptions.append({
                'invoice_document_id': doc_row['document_id'],
                'invoice_no':          comparison['invoice']['invoice_no'],
                'vendor_name':         comparison['invoice']['vendor_name'],
                'uploaded_at':         comparison['invoice']['uploaded_at'],
                'exception_type':      exc_type,
                'exception_label':     label,
                'detail':              detail,
                'severity':            severity,
            })

        conn.close()

        return jsonify(exceptions[offset:offset + limit]), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# REPORT: SUMMARY STATS + 30-DAY TIMELINE
# GET /auditor/report/summary
# Auditor only
# ------------------------------------------------------------
def _period_start(period):
    now = datetime.utcnow()
    if period == 'today':
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        return now - timedelta(days=7)
    if period == 'month':
        return now - timedelta(days=30)
    return None  # 'all'


@auditor_bp.route('/report/summary', methods=['GET'])
@jwt_required()
def get_report_summary():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    period = request.args.get('period', 'month')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        period_start = _period_start(period)

        # ── Stats: approved/sent_back counted as events within the
        # period; pending/exceptions are current-state snapshots (a
        # "how many right now", not something a past period bounds). ──
        if period_start:
            cursor.execute(
                '''SELECT rr.action, COUNT(*) AS cnt
                   FROM review_records rr
                   JOIN users u ON rr.reviewed_by = u.user_id
                   WHERE u.role = 'auditor' AND rr.action IN ('approved', 'returned')
                     AND rr.reviewed_at >= %s
                   GROUP BY rr.action''',
                (period_start,)
            )
        else:
            cursor.execute(
                '''SELECT rr.action, COUNT(*) AS cnt
                   FROM review_records rr
                   JOIN users u ON rr.reviewed_by = u.user_id
                   WHERE u.role = 'auditor' AND rr.action IN ('approved', 'returned')
                   GROUP BY rr.action'''
            )
        action_counts = {row['action']: row['cnt'] for row in cursor.fetchall()}

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM documents WHERE status IN ('under_review', 'resubmitted')"
        )
        pending = cursor.fetchone()['cnt']

        # NOTE (perf): exceptions are recomputed per-call by scanning
        # every in-flight invoice through the same matching logic as
        # /auditor/exceptions, rather than reading a cached status
        # column. There's no such column in the current schema, and
        # this app's invoice volume is small (FYP/demo scale), so it's
        # fine for now — flagged for a real cache (e.g. a match_status
        # column updated on upload/approve/return) if volume grows.
        cursor.execute(
            '''SELECT document_id, uploaded_at, status
               FROM documents
               WHERE status IN ('under_review', 'resubmitted', 'returned')'''
        )
        doc_rows = cursor.fetchall()
        exception_count = 0
        for doc_row in doc_rows:
            comparison = _build_comparison(cursor, doc_row['document_id'])
            if comparison and _classify_exception(cursor, doc_row, comparison):
                exception_count += 1

        stats = {
            'approved':   action_counts.get('approved', 0),
            'sent_back':  action_counts.get('returned', 0),
            'pending':    pending,
            'exceptions': exception_count,
        }

        # ── Timeline: always the last 30 days, regardless of `period` ──
        thirty_days_ago = datetime.utcnow() - timedelta(days=29)
        thirty_days_ago = thirty_days_ago.replace(hour=0, minute=0, second=0, microsecond=0)

        cursor.execute(
            '''SELECT DATE(rr.reviewed_at) AS day, rr.action, COUNT(*) AS cnt
               FROM review_records rr
               JOIN users u ON rr.reviewed_by = u.user_id
               WHERE u.role = 'auditor' AND rr.action IN ('approved', 'returned')
                 AND rr.reviewed_at >= %s
               GROUP BY DATE(rr.reviewed_at), rr.action''',
            (thirty_days_ago,)
        )
        action_by_day = {}
        for row in cursor.fetchall():
            action_by_day.setdefault(row['day'], {})[row['action']] = row['cnt']

        cursor.execute(
            '''SELECT DATE(uploaded_at) AS day, COUNT(*) AS cnt
               FROM documents
               WHERE status IN ('under_review', 'resubmitted') AND uploaded_at >= %s
               GROUP BY DATE(uploaded_at)''',
            (thirty_days_ago,)
        )
        pending_by_day = {row['day']: row['cnt'] for row in cursor.fetchall()}

        conn.close()

        timeline = []
        for i in range(30):
            day = (thirty_days_ago + timedelta(days=i)).date()
            day_actions = action_by_day.get(day, {})
            timeline.append({
                'date':      day.isoformat(),
                'approved':  day_actions.get('approved', 0),
                'sent_back': day_actions.get('returned', 0),
                'pending':   pending_by_day.get(day, 0),
            })

        return jsonify({
            'period':   period,
            'stats':    stats,
            'timeline': timeline,
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# REPORT: AUDIT TRAIL (approve/send-back/need-review history)
# GET /auditor/report/audit-trail
# Auditor only
# ------------------------------------------------------------
ACTION_DB_TO_API = {'approved': 'approved', 'returned': 'sent_back', 'need_review': 'need_review'}
ACTION_API_TO_DB = {v: k for k, v in ACTION_DB_TO_API.items()}


def _audit_trail_query(action_filter, start_date, end_date):
    where = ["u.role = 'auditor'", "rr.action IN ('approved', 'returned', 'need_review')"]
    params = []

    if action_filter and action_filter != 'all':
        db_action = ACTION_API_TO_DB.get(action_filter)
        if db_action:
            where.append('rr.action = %s')
            params.append(db_action)

    if start_date:
        where.append('rr.reviewed_at >= %s')
        params.append(start_date)
    if end_date:
        where.append('rr.reviewed_at <= %s')
        params.append(end_date)

    where_clause = ' AND '.join(where)
    base = f'''FROM review_records rr
               JOIN users u ON rr.reviewed_by = u.user_id
               JOIN documents d ON rr.document_id = d.document_id
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               WHERE {where_clause}'''
    return base, params


@auditor_bp.route('/report/audit-trail', methods=['GET'])
@jwt_required()
def get_audit_trail():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    action_filter = request.args.get('action', 'all')
    start_date    = request.args.get('start_date')
    end_date      = request.args.get('end_date')
    limit         = request.args.get('limit', 50, type=int)
    offset        = request.args.get('offset', 0, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        base, params = _audit_trail_query(action_filter, start_date, end_date)

        cursor.execute(f'SELECT COUNT(*) AS cnt {base}', params)
        total = cursor.fetchone()['cnt']

        cursor.execute(
            f'''SELECT rr.reviewed_at, u.full_name AS auditor_name, u.email AS auditor_email,
                       rr.action, ef.invoice_number, rr.document_id, rr.remarks
                {base}
                ORDER BY rr.reviewed_at DESC
                LIMIT %s OFFSET %s''',
            params + [limit, offset]
        )
        rows = cursor.fetchall()
        conn.close()

        entries = [{
            'timestamp':           row['reviewed_at'].isoformat() if row['reviewed_at'] else None,
            'auditor_name':        row['auditor_name'],
            'auditor_email':       row['auditor_email'],
            'action':              ACTION_DB_TO_API.get(row['action'], row['action']),
            'invoice_no':          row['invoice_number'],
            'invoice_document_id': row['document_id'],
            'remarks':             row['remarks'],
        } for row in rows]

        return jsonify({'total': total, 'entries': entries}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# REPORT: AUDIT TRAIL CSV EXPORT
# GET /auditor/report/audit-trail/export.csv
# Auditor only
# ------------------------------------------------------------
@auditor_bp.route('/report/audit-trail/export.csv', methods=['GET'])
@jwt_required()
def export_audit_trail_csv():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    action_filter = request.args.get('action', 'all')
    start_date    = request.args.get('start_date')
    end_date      = request.args.get('end_date')
    limit         = request.args.get('limit', 50, type=int)
    offset        = request.args.get('offset', 0, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        base, params = _audit_trail_query(action_filter, start_date, end_date)

        cursor.execute(
            f'''SELECT rr.reviewed_at, u.full_name AS auditor_name, u.email AS auditor_email,
                       rr.action, ef.invoice_number, rr.remarks
                {base}
                ORDER BY rr.reviewed_at DESC
                LIMIT %s OFFSET %s''',
            params + [limit, offset]
        )
        rows = cursor.fetchall()
        conn.close()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(['Timestamp', 'Auditor Name', 'Auditor Email', 'Action', 'Invoice No', 'Remarks'])
        for row in rows:
            writer.writerow([
                row['reviewed_at'].isoformat() if row['reviewed_at'] else '',
                row['auditor_name'] or '',
                row['auditor_email'] or '',
                ACTION_DB_TO_API.get(row['action'], row['action']),
                row['invoice_number'] or '',
                row['remarks'] or '',
            ])

        return Response(
            buffer.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=audit_trail.csv'}
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 500
