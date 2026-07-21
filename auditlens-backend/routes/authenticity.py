import os
import io
import mimetypes
from flask import Blueprint, jsonify, request, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.authenticity_check import (
    run_authenticity_check, save_rendered_authenticity_image, AUTHENTICITY_IMAGE_DIR
)
from helpers.auth_rules import compute_authentication
from routes.auditor import _build_comparison

authenticity_bp = Blueprint('authenticity', __name__)

VALID_STATUSES = ('passed', 'warning')
VALID_DOC_TYPES = ('invoice', 'po', 'gr')


def _with_authentication_score(row):
    """
    Enriches an authenticity_checks row (already fetched via
    _SELECT_WITH_JOINS, which includes document_number from the joined
    extracted_fields/purchase_orders/goods_receipts table) with the new
    document-type-aware authentication_score/status/summary/signal_details
    fields — computed on the fly from data already in `row`, no new
    query and no new DB column. Mutates and returns `row` so every
    existing field (authenticity_status, has_company_chop, etc.) stays
    exactly as-is for the existing frontend, which reads those directly.
    """
    detected_signals = {
        'company_name': bool(row.get('has_company_name')),
        'company_logo': bool(row.get('has_company_logo')),
        'company_chop': bool(row.get('has_company_chop')),
        'signature':    bool(row.get('has_signature')),
        'doc_number':   bool(row.get('document_number')),
    }
    row.update(compute_authentication(row.get('document_type'), detected_signals))
    return row

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
           ac.authenticity_status, ac.ai_notes, ac.signal_boxes, ac.created_at,
           ac.ai_engine_used, ac.ai_visual_result, ac.document_consistency,
           ac.risk_level, ac.boxes
    FROM authenticity_checks ac
    LEFT JOIN extracted_fields ef ON ac.document_type = 'invoice' AND ac.document_id = ef.document_id
    LEFT JOIN purchase_orders po ON ac.document_type = 'po' AND ac.document_id = po.document_id
    LEFT JOIN goods_receipts gr ON ac.document_type = 'gr' AND ac.document_id = gr.document_id
'''


def _lookup_file_info(cursor, document_id, document_type):
    """
    Returns {'file_bytes', 'file_mime', 'file_name', 'file_path'} for the
    original uploaded file, or None if no row exists. PO/GR file info
    lives in their own tables, keyed by document_id but with their own
    row (a document_id can have multiple PO/GR rows over time; take the
    most recent, matching this app's existing convention elsewhere for
    "the current PO/GR for this document").

    file_bytes (Postgres) is the durable source — Render's free tier
    disk is ephemeral and wiped on every redeploy/restart. If file_bytes
    is NULL (a file over Config.MAX_DB_FILE_BYTES, or a record from
    before this feature existed) but the local disk copy still happens
    to exist in this process, that's read and returned instead —
    degrades gracefully without weakening the DB as the source of truth
    going forward.
    """
    if document_type == 'invoice':
        cursor.execute(
            'SELECT file_path, file_name, file_bytes, file_mime FROM documents WHERE document_id = %s',
            (document_id,)
        )
    elif document_type == 'po':
        cursor.execute(
            '''SELECT file_path, file_name, file_bytes, file_mime FROM purchase_orders
               WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1''',
            (document_id,)
        )
    elif document_type == 'gr':
        cursor.execute(
            '''SELECT file_path, file_name, file_bytes, file_mime FROM goods_receipts
               WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1''',
            (document_id,)
        )
    else:
        return None

    row = cursor.fetchone()
    if not row:
        return None

    file_bytes = bytes(row['file_bytes']) if row['file_bytes'] is not None else None
    if file_bytes is None and row['file_path'] and os.path.exists(row['file_path']):
        with open(row['file_path'], 'rb') as f:
            file_bytes = f.read()

    return {
        'file_bytes': file_bytes,
        'file_mime':  row['file_mime'],
        'file_name':  row['file_name'],
        'file_path':  row['file_path'],
    }


def _document_consistency_for(cursor, document_id):
    """Document Consistency Verification (spec section 3) — reuses the
    EXISTING 3-way matching engine (routes/auditor.py::_build_comparison,
    already used by GET /auditor/record/<id>/comparison) instead of
    asking Claude to reason across Invoice/PO/GR itself. Returns None if
    the parent `documents` row doesn't exist at all (comparison has
    nothing to build against)."""
    comparison = _build_comparison(cursor, document_id)
    if not comparison:
        return None
    match = comparison['match_result']
    return {
        'vendor_match':   match['vendor_match'],
        'po_match':       match['po_reference_match'],
        'item_match':     match['line_items_match'],
        'amount_match':   match['amount_match'],
        'overall_status': match['overall_status'],
    }


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

        # On-demand: no check has ever run for this document/type yet —
        # run the Claude-first-then-Gemini-fallback engine now (instead
        # of the old automatic upload-time call) and cache the result,
        # same "compute on first read" pattern already used below by
        # GET /authenticity/<id>/image for the rendered PNG.
        if not row:
            info = _lookup_file_info(cursor, document_id, document_type)
            if not info or not info['file_bytes']:
                conn.close()
                return jsonify({'error': 'No authenticity check for this document'}), 404

            document_consistency = _document_consistency_for(cursor, document_id)
            conn.close()

            check_id = run_authenticity_check(document_id, info['file_bytes'], info['file_name'],
                                                document_type, document_consistency=document_consistency)
            if check_id is None:
                return jsonify({'error': 'Authenticity check failed — see server logs'}), 502

            conn   = get_db_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(_SELECT_WITH_JOINS + ' WHERE ac.check_id = %s', (check_id,))
            row = cursor.fetchone()

        conn.close()
        return jsonify(_with_authentication_score(row)), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET AUTHENTICITY IMAGE — the image to show + draw overlay markers on.
# GET /authenticity/<document_id>/image?document_type=invoice|po|gr
# Serves a cached rendered PNG if one already exists in the local
# (ephemeral) cache. Otherwise, for a PDF, renders page 1 on demand from
# the ORIGINAL file bytes stored in Postgres — NOT local disk, which is
# wiped on every Render redeploy/restart — and caches the render
# locally, so every future request in this process is instant. For an
# image upload (jpg/png), serves the DB-stored bytes directly, since
# they already are an image. PURE RENDERING ONLY (PyMuPDF/fitz via
# save_rendered_authenticity_image) — this route never calls Gemini or
# makes any AI/network call; it only ever reads/renders bytes already
# sitting in the database. Auditor only.
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
        info = _lookup_file_info(cursor, document_id, document_type)
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if not info or not info['file_bytes']:
        return jsonify({'error': 'Original file unavailable for this document'}), 404

    if not info['file_name'].lower().endswith('.pdf'):
        mimetype = info['file_mime'] or mimetypes.guess_type(info['file_name'])[0] or 'image/jpeg'
        return send_file(io.BytesIO(info['file_bytes']), mimetype=mimetype)

    # No cached render yet for this PDF — render page 1 on demand from
    # the ORIGINAL file bytes (Postgres, not disk) and cache it locally,
    # so every future request for this document is instant.
    # save_rendered_authenticity_image() is pure PyMuPDF rendering, no
    # Gemini/network call whatsoever — confirmed again this session.
    save_rendered_authenticity_image(document_id, document_type, info['file_bytes'], info['file_name'])

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

        return jsonify([_with_authentication_score(row) for row in rows]), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# RE-CHECK: force re-run the authenticity engine (Claude primary, Gemini
# fallback) for one document and update the cached row. GET /<id> above
# also triggers this engine on-demand (first view of a never-checked
# document) — this route is for an already-checked document the auditor
# wants re-verified.
# POST /authenticity/<document_id>/recheck?document_type=invoice|po|gr
# Auditor only.
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
        info = _lookup_file_info(cursor, document_id, document_type)

        if not info or not info['file_bytes']:
            conn.close()
            return jsonify({'error': 'No file found for this document/document_type'}), 404

        document_consistency = _document_consistency_for(cursor, document_id)
        conn.close()

        check_id = run_authenticity_check(document_id, info['file_bytes'], info['file_name'],
                                            document_type, document_consistency=document_consistency)
        if check_id is None:
            return jsonify({'error': 'Re-check failed (Claude/Gemini call unsuccessful) — see server logs'}), 502

        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            _SELECT_WITH_JOINS + ' WHERE ac.check_id = %s',
            (check_id,)
        )
        row = cursor.fetchone()
        conn.close()

        return jsonify(_with_authentication_score(row)), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
