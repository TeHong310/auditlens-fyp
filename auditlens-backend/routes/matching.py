from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit
from helpers.entity_normalizer import is_same_company, log_entity_match_debug

matching_bp = Blueprint('matching', __name__)


# ============================================================
# Helper: Compare two vendor names (normalized + OCR-typo-tolerant
# fuzzy similarity — see helpers/entity_normalizer.py). Kept separate
# from the generic compare_field() below, which the vendor comparisons
# here used to go through: compare_field()'s "text" branch is a naive
# substring/character-overlap heuristic, not company-name-aware (no
# suffix stripping, no fuzzy tolerance for a single OCR-dropped letter),
# and reported the SAME supplier as a mismatch whenever spacing/suffix/
# spelling varied across documents.
# ============================================================
def compare_vendor_field(val1, val2, source_label='vendor A', target_label='vendor B'):
    if val1 is None and val2 is None:
        return True, 100.0
    if val1 is None or val2 is None:
        return False, 0.0
    result = is_same_company(val1, val2)
    log_entity_match_debug(source_label, val1, target_label, val2, result)
    return result['match'], result['similarity']


# ============================================================
# Helper: Compare two field values
# ============================================================
def compare_field(val1, val2, field_type='text'):
    if val1 is None and val2 is None:
        return True, 100.0
    if val1 is None or val2 is None:
        return False, 0.0

    if field_type == 'amount':
        try:
            match = abs(float(val1) - float(val2)) < 0.01
            return match, 100.0 if match else 0.0
        except:
            return False, 0.0

    if field_type == 'date':
        match = str(val1) == str(val2)
        return match, 100.0 if match else 0.0

    v1 = str(val1).lower().strip()
    v2 = str(val2).lower().strip()

    if v1 == v2:
        return True, 100.0

    shorter = min(len(v1), len(v2))
    longer  = max(len(v1), len(v2))
    if longer == 0:
        return True, 100.0

    if v1 in v2 or v2 in v1:
        score = round((shorter / longer) * 100, 2)
        return score >= 70, score

    common = sum(1 for c in v1 if c in v2)
    score  = round((common / longer) * 100, 2)
    return score >= 70, score


# ============================================================
# Helper: Serialize row dates
# ============================================================
def serialize_row(row):
    if not row:
        return None
    r = dict(row)
    for k, v in r.items():
        if hasattr(v, 'isoformat'):
            r[k] = v.isoformat()
    return r


# ============================================================
# Helper: Run 2-Way Match (Invoice vs GR)
# ============================================================
def run_two_way_match(invoice, gr):
    results = {}

    inv_gr_vendor, inv_gr_vendor_score = compare_vendor_field(
        invoice.get('vendor_name'), gr.get('vendor_name'), 'Invoice vendor', 'GR vendor'
    )

    # Check logistics partner if vendor doesn't match
    logistics_note = None
    if not inv_gr_vendor:
        is_logistics, logistics_note = is_logistics_partner_match(
            invoice.get('vendor_name'), gr.get('vendor_name')
        )
        if is_logistics:
            inv_gr_vendor       = True
            inv_gr_vendor_score = 100.0

    inv_gr_amount, inv_gr_amount_score = compare_field(
        invoice.get('total_amount'), gr.get('total_amount'), 'amount'
    )

    results['invoice_gr_vendor_match'] = inv_gr_vendor
    results['invoice_gr_vendor_score'] = inv_gr_vendor_score
    results['invoice_gr_amount_match'] = inv_gr_amount
    results['invoice_gr_amount_score'] = inv_gr_amount_score
    results['invoice_gr_match']        = inv_gr_vendor and inv_gr_amount
    results['logistics_note']          = logistics_note

    match_count   = sum([inv_gr_vendor, inv_gr_amount])
    overall_score = round((match_count / 2) * 100, 2)

    if overall_score == 100:
        overall_status = 'full_match'
    elif overall_score >= 50:
        overall_status = 'partial_match'
    else:
        overall_status = 'mismatch'

    results['overall_match_score'] = overall_score
    results['overall_status']      = overall_status
    results['match_type']          = '2-way'

    return results


# ============================================================
# Helper: Run 3-Way Match (Invoice vs PO vs GR)
# ============================================================
def run_three_way_match(invoice, po, gr):
    results = {}

    # Invoice vs PO
    inv_po_vendor, inv_po_vendor_score = compare_vendor_field(
        invoice.get('vendor_name'), po.get('vendor_name'), 'Invoice vendor', 'PO vendor'
    )
    inv_po_amount, inv_po_amount_score = compare_field(
        invoice.get('total_amount'), po.get('total_amount'), 'amount'
    )
    results['invoice_po_vendor_match'] = inv_po_vendor
    results['invoice_po_vendor_score'] = inv_po_vendor_score
    results['invoice_po_amount_match'] = inv_po_amount
    results['invoice_po_amount_score'] = inv_po_amount_score
    results['invoice_po_match']        = inv_po_vendor and inv_po_amount

    # Invoice vs GR
    inv_gr_vendor, inv_gr_vendor_score = compare_vendor_field(
        invoice.get('vendor_name'), gr.get('vendor_name'), 'Invoice vendor', 'GR vendor'
    )

    # Logistics partner check
    logistics_note = None
    if not inv_gr_vendor:
        is_logistics, logistics_note = is_logistics_partner_match(
            invoice.get('vendor_name'), gr.get('vendor_name')
        )
        if is_logistics:
            inv_gr_vendor       = True
            inv_gr_vendor_score = 100.0

    inv_gr_amount, inv_gr_amount_score = compare_field(
        invoice.get('total_amount'), gr.get('total_amount'), 'amount'
    )
    results['invoice_gr_vendor_match'] = inv_gr_vendor
    results['invoice_gr_vendor_score'] = inv_gr_vendor_score
    results['invoice_gr_amount_match'] = inv_gr_amount
    results['invoice_gr_amount_score'] = inv_gr_amount_score
    results['invoice_gr_match']        = inv_gr_vendor and inv_gr_amount
    results['logistics_note']          = logistics_note

    # PO vs GR
    po_gr_vendor, po_gr_vendor_score = compare_vendor_field(
        po.get('vendor_name'), gr.get('vendor_name'), 'PO vendor', 'GR vendor'
    )
    po_gr_amount, po_gr_amount_score = compare_field(
        po.get('total_amount'), gr.get('total_amount'), 'amount'
    )
    results['po_gr_vendor_match'] = po_gr_vendor
    results['po_gr_vendor_score'] = po_gr_vendor_score
    results['po_gr_amount_match'] = po_gr_amount
    results['po_gr_amount_score'] = po_gr_amount_score
    results['po_gr_match']        = po_gr_vendor and po_gr_amount

    match_count   = sum([
        results['invoice_po_match'],
        results['invoice_gr_match'],
        results['po_gr_match']
    ])
    overall_score = round((match_count / 3) * 100, 2)

    if overall_score == 100:
        overall_status = 'full_match'
    elif overall_score >= 67:
        overall_status = 'partial_match'
    else:
        overall_status = 'mismatch'

    results['overall_match_score'] = overall_score
    results['overall_status']      = overall_status
    results['match_type']          = '3-way'

    return results


# ============================================================
# Helper: Detect and save exceptions
# ============================================================
def save_exceptions(cursor, document_id, match_results, match_type, invoice, po, gr):
    exceptions = []

    if match_type == '3-way':
        if not match_results['invoice_po_match']:
            exceptions.append({
                'type': 'invoice_po_mismatch',
                'description': f"Invoice vs PO mismatch: vendor='{invoice.get('vendor_name')}' vs '{po.get('vendor_name')}', amount='{invoice.get('total_amount')}' vs '{po.get('total_amount')}'",
                'severity': 'high'
            })
        if not match_results['invoice_gr_match']:
            exceptions.append({
                'type': 'invoice_gr_mismatch',
                'description': f"Invoice vs GR mismatch: vendor='{invoice.get('vendor_name')}' vs '{gr.get('vendor_name')}', amount='{invoice.get('total_amount')}' vs '{gr.get('total_amount')}'",
                'severity': 'high'
            })
        if not match_results['po_gr_match']:
            exceptions.append({
                'type': 'po_gr_mismatch',
                'description': f"PO vs GR mismatch: vendor or amount does not match",
                'severity': 'medium'
            })
    else:
        if not match_results['invoice_gr_match']:
            exceptions.append({
                'type': 'invoice_gr_mismatch',
                'description': f"Invoice vs GR mismatch: vendor='{invoice.get('vendor_name')}' vs '{gr.get('vendor_name')}', amount='{invoice.get('total_amount')}' vs '{gr.get('total_amount')}'",
                'severity': 'high'
            })

    for exc in exceptions:
        cursor.execute(
            '''INSERT INTO exceptions (document_id, exception_type, description, severity)
               VALUES (%s, %s, %s, %s)''',
            (document_id, exc['type'], exc['description'], exc['severity'])
        )

    return exceptions


# ------------------------------------------------------------
# RUN MATCHING (Auto 2-way or 3-way)
# POST /matching/run/<document_id>
# Auditor only
# ------------------------------------------------------------
@matching_bp.route('/run/<int:document_id>', methods=['POST'])
@jwt_required()
def run_matching(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get Invoice OCR data
        cursor.execute(
            'SELECT * FROM extracted_fields WHERE document_id = %s',
            (document_id,)
        )
        invoice = cursor.fetchone()
        if not invoice:
            conn.close()
            return jsonify({'error': 'Invoice OCR data not found'}), 404

        # Get PO (optional)
        cursor.execute(
            'SELECT * FROM purchase_orders WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
        po = cursor.fetchone()

        # Get GR
        cursor.execute(
            'SELECT * FROM goods_receipts WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
        gr = cursor.fetchone()

        if not gr:
            conn.close()
            return jsonify({'error': 'Goods Receipt not uploaded yet. Please upload GR first.'}), 400

        invoice_dict = dict(invoice)
        gr_dict      = dict(gr)
        po_dict      = dict(po) if po else None

        # Decide match type
        if po_dict:
            match_results = run_three_way_match(invoice_dict, po_dict, gr_dict)
            match_type    = '3-way'
            po_id         = po_dict['po_id']
            gr_id         = gr_dict['gr_id']
        else:
            match_results = run_two_way_match(invoice_dict, gr_dict)
            match_type    = '2-way'
            po_id         = None
            gr_id         = gr_dict['gr_id']

        # Save to three_way_matches table
        cursor2 = conn.cursor()
        cursor2.execute(
            '''INSERT INTO three_way_matches
               (document_id, po_id, gr_id,
                invoice_po_match, invoice_gr_match, po_gr_match,
                overall_match_score, overall_status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING three_way_id''',
            (
                document_id,
                po_id,
                gr_id,
                match_results.get('invoice_po_match', None),
                match_results.get('invoice_gr_match', False),
                match_results.get('po_gr_match', None),
                match_results['overall_match_score'],
                match_results['overall_status']
            )
        )
        three_way_id = cursor2.fetchone()[0]

        # Save exceptions
        exceptions = save_exceptions(
            cursor2, document_id, match_results,
            match_type, invoice_dict, po_dict or {}, gr_dict
        )

        # Update document status
        cursor2.execute(
            "UPDATE documents SET status = 'under_review' WHERE document_id = %s",
            (document_id,)
        )

        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'RUN_MATCHING', 'three_way_matches', three_way_id,
                  f'{match_type} match for document {document_id}: {match_results["overall_status"]}')

        response = {
            'message':        f'{match_type} matching completed',
            'three_way_id':   three_way_id,
            'match_type':     match_type,
            'overall_status': match_results['overall_status'],
            'overall_score':  match_results['overall_match_score'],
            'po_available':   po_dict is not None,
            'gr_available':   True,
            'exceptions_found': len(exceptions),
            'logistics_note': match_results.get('logistics_note'),
            'invoice': {
                'vendor_name':    invoice_dict.get('vendor_name'),
                'total_amount':   float(invoice_dict['total_amount']) if invoice_dict.get('total_amount') else None,
                'invoice_date':   str(invoice_dict['invoice_date']) if invoice_dict.get('invoice_date') else None,
                'invoice_number': invoice_dict.get('invoice_number'),
            },
            'goods_receipt': {
                'gr_number':    gr_dict.get('gr_number'),
                'vendor_name':  gr_dict.get('vendor_name'),
                'total_amount': float(gr_dict['total_amount']) if gr_dict.get('total_amount') else None,
                'receipt_date': str(gr_dict['receipt_date']) if gr_dict.get('receipt_date') else None,
            },
            'match_details': {
                'invoice_gr_vendor_match': match_results.get('invoice_gr_vendor_match'),
                'invoice_gr_amount_match': match_results.get('invoice_gr_amount_match'),
                'invoice_gr_match':        match_results.get('invoice_gr_match'),
            }
        }

        if po_dict:
            response['purchase_order'] = {
                'po_number':    po_dict.get('po_number'),
                'vendor_name':  po_dict.get('vendor_name'),
                'total_amount': float(po_dict['total_amount']) if po_dict.get('total_amount') else None,
                'po_date':      str(po_dict['po_date']) if po_dict.get('po_date') else None,
            }
            response['match_details'].update({
                'invoice_po_vendor_match': match_results.get('invoice_po_vendor_match'),
                'invoice_po_amount_match': match_results.get('invoice_po_amount_match'),
                'invoice_po_match':        match_results.get('invoice_po_match'),
                'po_gr_vendor_match':      match_results.get('po_gr_vendor_match'),
                'po_gr_amount_match':      match_results.get('po_gr_amount_match'),
                'po_gr_match':             match_results.get('po_gr_match'),
            })

        return jsonify(response), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET MATCH RESULT
# GET /matching/result/<document_id>
# ------------------------------------------------------------
@matching_bp.route('/result/<int:document_id>', methods=['GET'])
@jwt_required()
def get_match_result(document_id):
    user_id = get_jwt_identity()
    get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT twm.*,
                      po.po_number, po.vendor_name as po_vendor,
                      po.total_amount as po_amount, po.po_date,
                      gr.gr_number, gr.vendor_name as gr_vendor,
                      gr.total_amount as gr_amount, gr.receipt_date,
                      ef.invoice_number, ef.vendor_name as invoice_vendor,
                      ef.total_amount as invoice_amount, ef.invoice_date,
                      ef.ocr_confidence
               FROM three_way_matches twm
               LEFT JOIN purchase_orders po  ON twm.po_id = po.po_id
               LEFT JOIN goods_receipts  gr  ON twm.gr_id = gr.gr_id
               LEFT JOIN extracted_fields ef ON twm.document_id = ef.document_id
               WHERE twm.document_id = %s
               ORDER BY twm.matched_at DESC LIMIT 1''',
            (document_id,)
        )
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'error': 'No match result found'}), 404

        return jsonify({'match_result': serialize_row(result)}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET EXCEPTIONS
# GET /matching/exceptions/<document_id>
# ------------------------------------------------------------
@matching_bp.route('/exceptions/<int:document_id>', methods=['GET'])
@jwt_required()
def get_exceptions(document_id):
    user_id = get_jwt_identity()
    get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            'SELECT * FROM exceptions WHERE document_id = %s ORDER BY created_at DESC',
            (document_id,)
        )
        exceptions = cursor.fetchall()
        conn.close()

        return jsonify({
            'document_id':      document_id,
            'total_exceptions': len(exceptions),
            'exceptions':       [serialize_row(e) for e in exceptions]
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET ALL DOCUMENTS FOR AUDITOR REVIEW
# GET /matching/queue
# Auditor only
# ------------------------------------------------------------
@matching_bp.route('/queue', methods=['GET'])
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
            '''SELECT d.document_id, d.file_name, d.status, d.uploaded_at,
                      ef.invoice_number, ef.vendor_name, ef.total_amount,
                      ef.invoice_date, ef.ocr_confidence,
                      CASE WHEN po.po_id IS NOT NULL THEN true ELSE false END as has_po,
                      CASE WHEN gr.gr_id IS NOT NULL THEN true ELSE false END as has_gr,
                      ac.authenticity_status, ac.has_company_name,
                      ac.has_company_chop, ac.has_signature
               FROM documents d
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               LEFT JOIN (
                   SELECT DISTINCT ON (document_id) * FROM purchase_orders ORDER BY document_id, uploaded_at DESC
               ) po ON d.document_id = po.document_id
               LEFT JOIN (
                   SELECT DISTINCT ON (document_id) * FROM goods_receipts ORDER BY document_id, uploaded_at DESC
               ) gr ON d.document_id = gr.document_id
               LEFT JOIN authenticity_checks ac ON d.document_id = ac.document_id AND ac.document_type = 'invoice'
               WHERE d.status IN ('under_review', 'resubmitted')
               ORDER BY d.uploaded_at DESC'''
        )
        documents = cursor.fetchall()
        conn.close()

        return jsonify({
            'documents': [serialize_row(d) for d in documents]
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

        # Known logistics partners mapping
# Key = logistics company name (lowercase), Value = actual supplier (lowercase)
LOGISTICS_PARTNERS = {
    'swap logistics distribution sdn bhd': 'maxis broadband sdn bhd',
    'swap logistics': 'maxis',
    'gdex': None,      # universal courier, any vendor ok
    'pos laju': None,
    'dhl': None,
    'j&t': None,
    'ninja van': None,
    'citylink': None,
}

def is_logistics_partner_match(vendor1, vendor2):
    """
    Check if vendor mismatch is because one is a logistics partner of the other.
    Returns (is_logistics_match, note)
    """
    if not vendor1 or not vendor2:
        return False, None

    v1 = str(vendor1).lower().strip()
    v2 = str(vendor2).lower().strip()

    for logistics, supplier in LOGISTICS_PARTNERS.items():
        # Check if v1 is a logistics partner
        if logistics in v1:
            if supplier is None:
                return True, f"Delivered via logistics partner: {vendor1}"
            if supplier in v2:
                return True, f"Delivered via logistics partner: {vendor1} on behalf of {vendor2}"

        # Check if v2 is a logistics partner
        if logistics in v2:
            if supplier is None:
                return True, f"Delivered via logistics partner: {vendor2}"
            if supplier in v1:
                return True, f"Delivered via logistics partner: {vendor2} on behalf of {vendor1}"

    return False, None