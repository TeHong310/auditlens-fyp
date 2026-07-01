import re
from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id

auditor_bp = Blueprint('auditor', __name__)


def _serialize(row):
    if not row:
        return None
    r = dict(row)
    for k, v in r.items():
        if hasattr(v, 'isoformat'):
            r[k] = v.isoformat()
    return r


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
            conn.close()
            return jsonify({'error': 'Invoice document not found'}), 404

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

        conn.close()

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

        return jsonify({
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
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
