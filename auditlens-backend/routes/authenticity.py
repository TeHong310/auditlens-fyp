import os
import mimetypes
from flask import Blueprint, jsonify, request, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.authenticity_check import (
    run_authenticity_check, save_rendered_authenticity_image, AUTHENTICITY_IMAGE_DIR
)

authenticity_bp = Blueprint('authenticity', __name__)

VALID_STATUSES = ('passed', 'warning')
VALID_DOC_TYPES = ('invoice', 'po', 'gr')

# Reused by both GET endpoints: joins to whichever source table actually has
# the reference number + vendor name + file identifiers for this row's
# document_type (invoice -> extracted_fields/documents, po ->
# purchase_orders, gr -> goods_receipts). A single document_id can have up
# to 3 authenticity_checks rows (one per type), so there's no one join that
# fits every row. po_id/gr_id are included because the file-serving routes
# for those types are keyed by po_id/gr_id, not document_id
# (GET /documents/po/<po_id>/file, GET /documents/gr/<gr_id>/file).
_SELECT_WITH_JOINS = '''
    SELECT ac.check_id, ac.document_id,
           CASE ac.document_type
               WHEN 'invoice' THEN ef.invoice_number
               WHEN 'po' THEN po.po_number
               WHEN 'gr' THEN gr.gr_number
           END AS document_number,
           COALESCE(
               CASE ac.document_type WHEN 'invoice' THEN ef.vendor_name END,
               CASE ac.document_type WHEN 'po' THEN po.vendor_name END,
               CASE ac.document_type WHEN 'gr' THEN gr.vendor_name END
           ) AS vendor_name,
           po.po_id, gr.gr_id,
           ac.document_type, ac.has_company_chop, ac.has_company_logo,
           ac.has_company_name, ac.has_signature, ac.upload_source,
           ac.authenticity_status, ac.ai_notes, ac.signal_boxes, ac.created_at
    FROM authenticity_checks ac
    LEFT JOIN extracted_fields ef ON ac.document_type = 'invoice' AND ac.document_id = ef.document_id
    LEFT JOIN purchase_orders po ON ac.document_type = 'po' AND ac.document_id = po.document_id
    LEFT JOIN goods_receipts gr ON ac.document_type = 'gr' AND ac.document_id = gr.document_id
'''


def _lookup_file_path(cursor, document_id, document_type):
    """File path for the physical file Gemini needs to (re-)analyze.
    PO/GR file paths live in their own tables, keyed by document_id but
    with their own file_path column (a document_id can have multiple PO/GR
    rows over time; take the most recent, matching this app's existing
    convention elsewhere for "the current PO/GR for this document")."""
    if document_type == 'invoice':
        cursor.execute('SELECT file_path FROM documents WHERE document_id = %s', (document_id,))
    elif document_type == 'po':
        cursor.execute(
            'SELECT file_path FROM purchase_orders WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
    elif document_type == 'gr':
        cursor.execute(
            'SELECT file_path FROM goods_receipts WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
    else:
        return None
    row = cursor.fetchone()
    return row['file_path'] if row else None


# ------------------------------------------------------------
# GET SINGLE AUTHENTICITY CHECK
# GET /authenticity/<document_id>?document_type=invoice|po|gr (default invoice)
# Auditor only
# ------------------------------------------------------------
@authenticity_bp.route('/<int:document_id>', methods=['GET'])
@jwt_required()
def get_authenticity_check(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    # A document_id can have up to 3 checks now (invoice/po/gr all attach to
    # the same parent document_id) - default to 'invoice' since that's what
    # every existing caller (the Record Detail warning banner) actually wants.
    document_type = request.args.get('document_type', 'invoice')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            _SELECT_WITH_JOINS + ' WHERE ac.document_id = %s AND ac.document_type = %s',
            (document_id, document_type)
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({'error': 'No authenticity check for this document'}), 404

        return jsonify(row), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET AUTHENTICITY IMAGE — the image to show + draw overlay markers on.
# GET /authenticity/<document_id>/image?document_type=invoice|po|gr
# Serves a cached rendered PNG if one already exists (saved either at
# upload time or by an earlier call to this route). Otherwise, for a
# PDF, renders page 1 from the ORIGINAL uploaded file on demand and
# caches it — this is the fix for older records uploaded before image
# saving existed, so every record can display its image regardless of
# when it was uploaded. For an image upload (jpg/png), serves the
# original file directly, since it already is one. PURE RENDERING ONLY
# (PyMuPDF/fitz via save_rendered_authenticity_image) — this route never
# calls Gemini or makes any AI/network call; it only ever reads/renders
# the already-stored original file. Auditor only.
# ------------------------------------------------------------
@authenticity_bp.route('/<int:document_id>/image', methods=['GET'])
@jwt_required()
def get_authenticity_image(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    document_type = request.args.get('document_type', 'invoice')
    if document_type not in VALID_DOC_TYPES:
        return jsonify({'error': f'document_type must be one of {VALID_DOC_TYPES}'}), 400

    rendered_path = os.path.join(AUTHENTICITY_IMAGE_DIR, f'{document_id}_{document_type}.png')
    if os.path.exists(rendered_path):
        return send_file(rendered_path, mimetype='image/png')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        file_path = _lookup_file_path(cursor, document_id, document_type)
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if not file_path or not os.path.exists(file_path):
        return jsonify({'error': 'Original file unavailable for this document'}), 404

    if not file_path.lower().endswith('.pdf'):
        mimetype = mimetypes.guess_type(file_path)[0] or 'image/jpeg'
        return send_file(file_path, mimetype=mimetype)

    # No cached render yet for this PDF — render page 1 on demand from
    # the original file and cache it, so every future request for this
    # document is instant. save_rendered_authenticity_image() is pure
    # PyMuPDF rendering, no Gemini/network call whatsoever.
    save_rendered_authenticity_image(document_id, document_type, file_path)

    if not os.path.exists(rendered_path):
        return jsonify({'error': 'Could not render document image'}), 500

    print(f"DEBUG authenticity image: rendered from original for doc={document_id} (no Gemini)")
    return send_file(rendered_path, mimetype='image/png')


# ------------------------------------------------------------
# GET ALL AUTHENTICITY CHECKS (list view)
# GET /authenticity?status=passed|warning
# Auditor only
# ------------------------------------------------------------
@authenticity_bp.route('', methods=['GET'])
@jwt_required()
def get_authenticity_checks():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    status_filter = request.args.get('status', 'all')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        query = _SELECT_WITH_JOINS
        params = []
        if status_filter != 'all' and status_filter in VALID_STATUSES:
            query += ' WHERE ac.authenticity_status = %s'
            params.append(status_filter)
        query += ' ORDER BY ac.created_at DESC'

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return jsonify(rows), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# RE-CHECK: re-run Gemini Vision for one document and force-update
# the cached row (including signal_boxes).
# POST /authenticity/<document_id>/recheck?document_type=invoice|po|gr
# Auditor only. THE ONLY CODE PATH THAT CALLS GEMINI AFTER UPLOAD.
# ------------------------------------------------------------
@authenticity_bp.route('/<int:document_id>/recheck', methods=['POST'])
@jwt_required()
def recheck_authenticity(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    document_type = request.args.get('document_type', 'invoice')
    if document_type not in VALID_DOC_TYPES:
        return jsonify({'error': f'document_type must be one of {VALID_DOC_TYPES}'}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        file_path = _lookup_file_path(cursor, document_id, document_type)
        conn.close()

        if not file_path:
            return jsonify({'error': 'No file found for this document/document_type'}), 404

        check_id = run_authenticity_check(document_id, file_path, document_type)
        if check_id is None:
            return jsonify({'error': 'Re-check failed (Gemini call unsuccessful) — see server logs'}), 502

        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            _SELECT_WITH_JOINS + ' WHERE ac.check_id = %s',
            (check_id,)
        )
        row = cursor.fetchone()
        conn.close()

        return jsonify(row), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
