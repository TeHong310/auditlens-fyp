from flask import Blueprint, jsonify, request, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
import os
import io
import mimetypes
from datetime import datetime
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit
from helpers.ocr_helper import run_ocr, extract_fields, extract_po_fields, extract_gr_fields, calculate_confidence, parse_date
from helpers.anomaly_detector import run_anomaly_detection
from helpers.authenticity_check import run_authenticity_check
from helpers.gemini_extractor import gemini_extract_invoice_full
from config import Config

documents_bp = Blueprint('documents', __name__)


def _send_document_file(file_bytes, file_mime, file_path, file_name):
    """
    Serves the DB-stored bytes if present (the durable source — Render's
    disk is ephemeral, so this is what survives a restart). Falls back
    to the local disk path if bytes weren't stored (a file over
    Config.MAX_DB_FILE_BYTES, or a record from before this feature) —
    only works within the same process's lifetime, but degrades
    gracefully rather than failing immediately for that case.
    """
    if file_bytes:
        mimetype = file_mime or mimetypes.guess_type(file_name)[0] or 'application/octet-stream'
        return send_file(
            io.BytesIO(bytes(file_bytes)),
            mimetype=mimetype,
            as_attachment=False,
            download_name=file_name
        )
    if file_path and os.path.exists(file_path):
        mimetype = mimetypes.guess_type(file_name)[0] or 'application/octet-stream'
        return send_file(
            file_path,
            mimetype=mimetype,
            as_attachment=False,
            download_name=file_name
        )
    return jsonify({'error': 'File not found on server'}), 404

# ------------------------------------------------------------
# UPLOAD INVOICE + OCR
# POST /documents/upload
# ------------------------------------------------------------
@documents_bp.route('/upload', methods=['POST'])
@jwt_required()
def upload_document():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied. Finance Executive only.'}), 403

    if 'document' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file         = request.files['document']
    input_method = request.form.get('input_method', 'upload')

    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    file_ext = file.filename.rsplit('.', 1)[-1].lower()
    if file_ext not in Config.ALLOWED_EXTENSIONS:
        return jsonify({'error': f'File type not allowed. Use: {Config.ALLOWED_EXTENSIONS}'}), 400

    try:
        os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = f"{timestamp}_{file.filename}"
        file_path = os.path.join(Config.UPLOAD_FOLDER, safe_name)
        file.save(file_path)

        # Render's free tier disk is ephemeral (wiped on redeploy/restart),
        # so the original bytes are also persisted to Postgres — that's
        # the durable copy going forward; file_path/disk is only a
        # same-process convenience for the OCR/Gemini calls below. Files
        # over the size guard still upload and process normally; they
        # just aren't persisted to the DB (won't survive a restart).
        with open(file_path, 'rb') as f:
            file_bytes_data = f.read()
        file_mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
        db_file_bytes = file_bytes_data if len(file_bytes_data) <= Config.MAX_DB_FILE_BYTES else None
        if db_file_bytes is None:
            print(f"DEBUG Document upload: {safe_name} is {len(file_bytes_data)} bytes, "
                  f"over MAX_DB_FILE_BYTES ({Config.MAX_DB_FILE_BYTES}) — not persisted to DB")

        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO documents
               (uploaded_by, file_name, file_path, file_type, input_method, status, file_bytes, file_mime)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING document_id''',
            (user['user_id'], safe_name, file_path, file_ext, input_method, 'ocr_processing',
             psycopg2.Binary(db_file_bytes) if db_file_bytes is not None else None, file_mime)
        )
        document_id = cursor.fetchone()[0]
        conn.commit()

        # ← 改这里
        ocr_results, ocr_text, confidence = run_ocr(file_path, file_ext)
        confidence = float(confidence)
        fields     = extract_fields(ocr_text)

        # Single merged Gemini vision call: fields + authenticity signals
        # in one request, so an invoice upload never spends more than one
        # Gemini call (avoids the free-tier per-minute limit that two
        # separate calls — field extraction + authenticity — used to hit).
        # Falls back to the regex fields above and the OCR-text
        # authenticity heuristic (below) if this call fails.
        gemini_result = gemini_extract_invoice_full(file_bytes_data, safe_name)
        if gemini_result:
            for key in ('invoice_number', 'vendor_name', 'invoice_date', 'total_amount', 'tax_amount'):
                if gemini_result.get(key) is not None:
                    fields[key] = gemini_result[key]

        if fields['total_amount'] is not None:
            fields['total_amount'] = float(fields['total_amount'])
        if fields['tax_amount'] is not None:
            fields['tax_amount'] = float(fields['tax_amount'])

        invoice_date = parse_date(fields['invoice_date'])

        cursor.execute(
            '''INSERT INTO extracted_fields
               (document_id, invoice_number, vendor_name, invoice_date,
                total_amount, tax_amount, raw_ocr_text, ocr_confidence)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING extraction_id''',
            (document_id, fields['invoice_number'], fields['vendor_name'],
             invoice_date, fields['total_amount'], fields['tax_amount'],
             ocr_text, confidence)
        )
        extraction_id = cursor.fetchone()[0]

        cursor.execute(
            "UPDATE documents SET status = 'ocr_done' WHERE document_id = %s",
            (document_id,)
        )
        conn.commit()
        conn.close()

        try:
            run_anomaly_detection(document_id)
        except Exception as e:
            print(f"DEBUG anomaly detection error: {type(e).__name__}: {e}")

        try:
            run_authenticity_check(document_id, file_bytes_data, safe_name, 'invoice', ocr_text,
                                    precomputed_result=gemini_result,
                                    skip_gemini=not gemini_result)
        except Exception as e:
            print(f"DEBUG authenticity check error: {type(e).__name__}: {e}")

        log_audit(user['user_id'], 'UPLOAD_DOCUMENT', 'documents', document_id,
                  f'Document uploaded and OCR processed: {safe_name}')

        return jsonify({
            'message':        'Document uploaded and OCR processed successfully',
            'document_id':    document_id,
            'extraction_id':  extraction_id,
            'ocr_confidence': confidence,
            'extracted_fields': {
                'invoice_number': fields['invoice_number'],
                'vendor_name':    fields['vendor_name'],
                'invoice_date':   str(invoice_date) if invoice_date else fields['invoice_date'],
                'total_amount':   fields['total_amount'],
                'tax_amount':     fields['tax_amount'],
            },
            'raw_ocr_text': ocr_text
        }), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# UPLOAD PURCHASE ORDER + OCR
# POST /documents/upload-po/<document_id>
# ------------------------------------------------------------
@documents_bp.route('/upload-po/<int:document_id>', methods=['POST'])
@jwt_required()
def upload_purchase_order(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied. Finance Executive only.'}), 403

    if 'document' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file     = request.files['document']
    file_ext = file.filename.rsplit('.', 1)[-1].lower()

    if file_ext not in Config.ALLOWED_EXTENSIONS:
        return jsonify({'error': f'File type not allowed. Use: {Config.ALLOWED_EXTENSIONS}'}), 400

    try:
        os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = f"po_{timestamp}_{file.filename}"
        file_path = os.path.join(Config.UPLOAD_FOLDER, safe_name)
        file.save(file_path)

        with open(file_path, 'rb') as f:
            file_bytes_data = f.read()
        file_mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
        db_file_bytes = file_bytes_data if len(file_bytes_data) <= Config.MAX_DB_FILE_BYTES else None
        if db_file_bytes is None:
            print(f"DEBUG PO upload: {safe_name} is {len(file_bytes_data)} bytes, "
                  f"over MAX_DB_FILE_BYTES ({Config.MAX_DB_FILE_BYTES}) — not persisted to DB")

        # ← 改这里
        ocr_results, ocr_text, confidence = run_ocr(file_path, file_ext)
        confidence = float(confidence)
        fields     = extract_po_fields(ocr_text)

        if fields['total_amount'] is not None:
            fields['total_amount'] = float(fields['total_amount'])

        po_date = parse_date(fields['po_date'])

        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO purchase_orders
               (document_id, uploaded_by, file_name, file_path,
                po_number, vendor_name, po_date, total_amount,
                currency, raw_ocr_text, ocr_confidence, file_bytes, file_mime)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING po_id''',
            (document_id, user['user_id'], safe_name, file_path,
             fields['po_number'], fields['vendor_name'], po_date,
             fields['total_amount'], fields['currency'], ocr_text, confidence,
             psycopg2.Binary(db_file_bytes) if db_file_bytes is not None else None, file_mime)
        )
        po_id = cursor.fetchone()[0]
        conn.commit()
        conn.close()

        try:
            run_authenticity_check(document_id, file_bytes_data, safe_name, 'po', ocr_text)
        except Exception as e:
            print(f"DEBUG authenticity check error: {type(e).__name__}: {e}")

        log_audit(user['user_id'], 'UPLOAD_PO', 'purchase_orders', po_id,
                  f'PO uploaded for document {document_id}: {safe_name}')

        return jsonify({
            'message':        'Purchase Order uploaded and OCR processed successfully',
            'po_id':          po_id,
            'ocr_confidence': confidence,
            'extracted_fields': {
                'po_number':    fields['po_number'],
                'vendor_name':  fields['vendor_name'],
                'po_date':      str(po_date) if po_date else fields['po_date'],
                'total_amount': fields['total_amount'],
                'currency':     fields['currency'],
            }
        }), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# UPLOAD GOODS RECEIPT + OCR
# POST /documents/upload-gr/<document_id>
# ------------------------------------------------------------
@documents_bp.route('/upload-gr/<int:document_id>', methods=['POST'])
@jwt_required()
def upload_goods_receipt(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied. Finance Executive only.'}), 403

    if 'document' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file     = request.files['document']
    file_ext = file.filename.rsplit('.', 1)[-1].lower()

    if file_ext not in Config.ALLOWED_EXTENSIONS:
        return jsonify({'error': f'File type not allowed. Use: {Config.ALLOWED_EXTENSIONS}'}), 400

    try:
        os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = f"gr_{timestamp}_{file.filename}"
        file_path = os.path.join(Config.UPLOAD_FOLDER, safe_name)
        file.save(file_path)

        with open(file_path, 'rb') as f:
            file_bytes_data = f.read()
        file_mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
        db_file_bytes = file_bytes_data if len(file_bytes_data) <= Config.MAX_DB_FILE_BYTES else None
        if db_file_bytes is None:
            print(f"DEBUG GR upload: {safe_name} is {len(file_bytes_data)} bytes, "
                  f"over MAX_DB_FILE_BYTES ({Config.MAX_DB_FILE_BYTES}) — not persisted to DB")

        # ← 改这里
        ocr_results, ocr_text, confidence = run_ocr(file_path, file_ext)
        confidence = float(confidence)
        fields     = extract_gr_fields(ocr_text)

        if fields['total_amount'] is not None:
            fields['total_amount'] = float(fields['total_amount'])

        receipt_date = parse_date(fields['receipt_date'])

        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO goods_receipts
               (document_id, uploaded_by, file_name, file_path,
                gr_number, vendor_name, receipt_date, total_amount,
                currency, raw_ocr_text, ocr_confidence, file_bytes, file_mime)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING gr_id''',
            (document_id, user['user_id'], safe_name, file_path,
             fields['gr_number'], fields['vendor_name'], receipt_date,
             fields['total_amount'], fields['currency'], ocr_text, confidence,
             psycopg2.Binary(db_file_bytes) if db_file_bytes is not None else None, file_mime)
        )
        gr_id = cursor.fetchone()[0]
        conn.commit()
        conn.close()

        try:
            run_authenticity_check(document_id, file_bytes_data, safe_name, 'gr', ocr_text)
        except Exception as e:
            print(f"DEBUG authenticity check error: {type(e).__name__}: {e}")

        log_audit(user['user_id'], 'UPLOAD_GR', 'goods_receipts', gr_id,
                  f'GR uploaded for document {document_id}: {safe_name}')

        return jsonify({
            'message':        'Goods Receipt uploaded and OCR processed successfully',
            'gr_id':          gr_id,
            'ocr_confidence': confidence,
            'extracted_fields': {
                'gr_number':    fields['gr_number'],
                'vendor_name':  fields['vendor_name'],
                'receipt_date': str(receipt_date) if receipt_date else fields['receipt_date'],
                'total_amount': fields['total_amount'],
                'currency':     fields['currency'],
            }
        }), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET PO LIST
# GET /documents/po/list
# ------------------------------------------------------------
@documents_bp.route('/po/list', methods=['GET'])
@jwt_required()
def get_po_list():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if user['role'] == 'finance_executive':
            cursor.execute(
                '''SELECT po.* FROM purchase_orders po
                   JOIN documents d ON po.document_id = d.document_id
                   WHERE d.uploaded_by = %s
                   ORDER BY po.uploaded_at DESC''',
                (user['user_id'],)
            )
        else:
            cursor.execute('SELECT * FROM purchase_orders ORDER BY uploaded_at DESC')

        rows = cursor.fetchall()
        conn.close()

        result = []
        for r in rows:
            row = dict(r)
            row.pop('file_bytes', None)  # BYTEA, not JSON-serializable and huge — file endpoint serves it separately
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({'purchase_orders': result}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET GR LIST
# GET /documents/gr/list
# ------------------------------------------------------------
@documents_bp.route('/gr/list', methods=['GET'])
@jwt_required()
def get_gr_list():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if user['role'] == 'finance_executive':
            cursor.execute(
                '''SELECT gr.* FROM goods_receipts gr
                   JOIN documents d ON gr.document_id = d.document_id
                   WHERE d.uploaded_by = %s
                   ORDER BY gr.uploaded_at DESC''',
                (user['user_id'],)
            )
        else:
            cursor.execute('SELECT * FROM goods_receipts ORDER BY uploaded_at DESC')

        rows = cursor.fetchall()
        conn.close()

        result = []
        for r in rows:
            row = dict(r)
            row.pop('file_bytes', None)  # BYTEA, not JSON-serializable and huge — file endpoint serves it separately
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({'goods_receipts': result}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# UPDATE PO FIELDS
# PUT /documents/po/<po_id>/update
# ------------------------------------------------------------
@documents_bp.route('/po/<int:po_id>/update', methods=['PUT'])
@jwt_required()
def update_po_fields(po_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json()

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        po_date = parse_date(data.get('po_date')) if data.get('po_date') else None

        cursor.execute(
            '''UPDATE purchase_orders SET
               po_number    = %s,
               vendor_name  = %s,
               po_date      = %s,
               total_amount = %s
               WHERE po_id = %s''',
            (
                data.get('po_number'),
                data.get('vendor_name'),
                po_date,
                data.get('total_amount') or None,
                po_id
            )
        )
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'UPDATE_PO', 'purchase_orders', po_id,
                  'Finance manually updated PO fields')

        return jsonify({'message': 'PO updated successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# UPDATE GR FIELDS
# PUT /documents/gr/<gr_id>/update
# ------------------------------------------------------------
@documents_bp.route('/gr/<int:gr_id>/update', methods=['PUT'])
@jwt_required()
def update_gr_fields(gr_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json()

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        receipt_date = parse_date(data.get('receipt_date')) if data.get('receipt_date') else None

        cursor.execute(
            '''UPDATE goods_receipts SET
               gr_number    = %s,
               vendor_name  = %s,
               receipt_date = %s,
               total_amount = %s
               WHERE gr_id = %s''',
            (
                data.get('gr_number'),
                data.get('vendor_name'),
                receipt_date,
                data.get('total_amount') or None,
                gr_id
            )
        )
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'UPDATE_GR', 'goods_receipts', gr_id,
                  'Finance manually updated GR fields')

        return jsonify({'message': 'GR updated successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET DOCUMENT LIST
# GET /documents/
# ------------------------------------------------------------
@documents_bp.route('/', methods=['GET'])
@jwt_required()
def get_documents():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if user['role'] == 'finance_executive':
            cursor.execute(
                '''SELECT d.*, ef.invoice_number, ef.vendor_name, ef.invoice_date,
                          ef.total_amount, ef.tax_amount, ef.ocr_confidence
                   FROM documents d
                   LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
                   WHERE d.uploaded_by = %s
                   ORDER BY d.uploaded_at DESC''',
                (user['user_id'],)
            )
        else:
            cursor.execute(
                '''SELECT d.*, ef.invoice_number, ef.vendor_name, ef.invoice_date,
                          ef.total_amount, ef.tax_amount, ef.ocr_confidence
                   FROM documents d
                   LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
                   ORDER BY d.uploaded_at DESC'''
            )

        documents = cursor.fetchall()
        conn.close()

        result = []
        for d in documents:
            row = dict(d)
            row.pop('file_bytes', None)  # BYTEA, not JSON-serializable and huge — file endpoint serves it separately
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)

        return jsonify({'documents': result}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET SINGLE DOCUMENT DETAIL
# GET /documents/<document_id>
# ------------------------------------------------------------
@documents_bp.route('/<int:document_id>', methods=['GET'])
@jwt_required()
def get_document_detail(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            '''SELECT d.*, ef.*
               FROM documents d
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               WHERE d.document_id = %s''',
            (document_id,)
        )
        document = cursor.fetchone()
        conn.close()

        if not document:
            return jsonify({'error': 'Document not found'}), 404

        row = dict(document)
        row.pop('file_bytes', None)  # BYTEA, not JSON-serializable and huge — file endpoint serves it separately
        for k, v in row.items():
            if hasattr(v, 'isoformat'):
                row[k] = v.isoformat()

        return jsonify({'document': row}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# SERVE DOCUMENT FILE (Invoice)
# GET /documents/<document_id>/file
# ------------------------------------------------------------
@documents_bp.route('/<int:document_id>/file', methods=['GET'])
@jwt_required()
def serve_document_file(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            'SELECT file_name, file_path, file_bytes, file_mime, uploaded_by FROM documents WHERE document_id = %s',
            (document_id,)
        )
        document = cursor.fetchone()
        conn.close()

        if not document:
            return jsonify({'error': 'Document not found'}), 404

        if user['role'] == 'finance_executive' and document['uploaded_by'] != user['user_id']:
            return jsonify({'error': 'Access denied'}), 403

        return _send_document_file(document['file_bytes'], document['file_mime'],
                                    document['file_path'], document['file_name'])

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# SERVE PO FILE
# GET /documents/po/<po_id>/file
# ------------------------------------------------------------
@documents_bp.route('/po/<int:po_id>/file', methods=['GET'])
@jwt_required()
def serve_po_file(po_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            'SELECT file_name, file_path, file_bytes, file_mime, uploaded_by FROM purchase_orders WHERE po_id = %s',
            (po_id,)
        )
        po = cursor.fetchone()
        conn.close()

        if not po:
            return jsonify({'error': 'Purchase order not found'}), 404

        if user['role'] == 'finance_executive' and po['uploaded_by'] != user['user_id']:
            return jsonify({'error': 'Access denied'}), 403

        return _send_document_file(po['file_bytes'], po['file_mime'], po['file_path'], po['file_name'])

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# SERVE GR FILE
# GET /documents/gr/<gr_id>/file
# ------------------------------------------------------------
@documents_bp.route('/gr/<int:gr_id>/file', methods=['GET'])
@jwt_required()
def serve_gr_file(gr_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            'SELECT file_name, file_path, file_bytes, file_mime, uploaded_by FROM goods_receipts WHERE gr_id = %s',
            (gr_id,)
        )
        gr = cursor.fetchone()
        conn.close()

        if not gr:
            return jsonify({'error': 'Goods receipt not found'}), 404

        if user['role'] == 'finance_executive' and gr['uploaded_by'] != user['user_id']:
            return jsonify({'error': 'Access denied'}), 403

        return _send_document_file(gr['file_bytes'], gr['file_mime'], gr['file_path'], gr['file_name'])

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# UPDATE INVOICE EXTRACTED FIELDS
# PUT /documents/<document_id>/update-fields
# ------------------------------------------------------------
@documents_bp.route('/<int:document_id>/update-fields', methods=['PUT'])
@jwt_required()
def update_extracted_fields(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json()

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            '''UPDATE extracted_fields SET
               invoice_number = %s,
               vendor_name    = %s,
               invoice_date   = %s,
               total_amount   = %s,
               tax_amount     = %s
               WHERE document_id = %s''',
            (
                data.get('invoice_number'),
                data.get('vendor_name'),
                data.get('invoice_date') or None,
                data.get('total_amount') or None,
                data.get('tax_amount') or None,
                document_id
            )
        )
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'UPDATE_FIELDS', 'extracted_fields', document_id,
                  'Finance manually updated OCR extracted fields')

        return jsonify({'message': 'Fields updated successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
# ------------------------------------------------------------
# DELETE DOCUMENT
# DELETE /documents/<document_id>
# Finance Executive only
# ------------------------------------------------------------
@documents_bp.route('/<int:document_id>', methods=['DELETE'])
@jwt_required()
def delete_document(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'finance_executive':
        return jsonify({'error': 'Access denied'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        # Delete related records first
        cursor.execute('DELETE FROM exceptions WHERE document_id = %s', (document_id,))
        cursor.execute('DELETE FROM three_way_matches WHERE document_id = %s', (document_id,))
        cursor.execute('DELETE FROM record_matches WHERE document_id = %s', (document_id,))
        cursor.execute('DELETE FROM review_records WHERE document_id = %s', (document_id,))
        cursor.execute('DELETE FROM purchase_orders WHERE document_id = %s', (document_id,))
        cursor.execute('DELETE FROM goods_receipts WHERE document_id = %s', (document_id,))
        cursor.execute('DELETE FROM extracted_fields WHERE document_id = %s', (document_id,))
        cursor.execute('DELETE FROM audit_logs WHERE target_id = %s AND target_table = %s', (document_id, 'documents'))
        cursor.execute('DELETE FROM documents WHERE document_id = %s', (document_id,))

        conn.commit()
        conn.close()

        return jsonify({'message': 'Document deleted successfully'}), 200

    except Exception as e:
        print(f"DELETE ERROR: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ------------------------------------------------------------
# GET PO + GR FOR A DOCUMENT
# GET /documents/<document_id>/supporting
# ------------------------------------------------------------
@documents_bp.route('/<int:document_id>/supporting', methods=['GET'])
@jwt_required()
def get_supporting_documents(document_id):
    user_id = get_jwt_identity()
    get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            'SELECT * FROM purchase_orders WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
        po = cursor.fetchone()

        cursor.execute(
            'SELECT * FROM goods_receipts WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1',
            (document_id,)
        )
        gr = cursor.fetchone()

        conn.close()

        def serialize(row):
            if not row:
                return None
            r = dict(row)
            r.pop('file_bytes', None)  # BYTEA, not JSON-serializable and huge — file endpoint serves it separately
            for k, v in r.items():
                if hasattr(v, 'isoformat'):
                    r[k] = v.isoformat()
            return r

        return jsonify({
            'document_id':    document_id,
            'purchase_order': serialize(po),
            'goods_receipt':  serialize(gr),
            'po_uploaded':    po is not None,
            'gr_uploaded':    gr is not None,
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500