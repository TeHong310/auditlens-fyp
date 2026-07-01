from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id

ocr_review_bp = Blueprint('ocr_review', __name__)

# ------------------------------------------------------------
# GET RELATED PO / GR STATUS FOR AN INVOICE
# GET /ocr-review/invoice/<document_id>/related-docs
# ------------------------------------------------------------
@ocr_review_bp.route('/invoice/<int:document_id>/related-docs', methods=['GET'])
@jwt_required()
def get_related_docs(document_id):
    user_id = get_jwt_identity()
    get_user_by_id(user_id)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            'SELECT invoice_number FROM extracted_fields WHERE document_id = %s',
            (document_id,)
        )
        invoice = cursor.fetchone()

        if not invoice:
            conn.close()
            return jsonify({'error': 'Invoice not found'}), 404

        cursor.execute(
            '''SELECT po_id, po_number, file_name FROM purchase_orders
               WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1''',
            (document_id,)
        )
        po = cursor.fetchone()

        cursor.execute(
            '''SELECT gr_id, gr_number, file_name FROM goods_receipts
               WHERE document_id = %s ORDER BY uploaded_at DESC LIMIT 1''',
            (document_id,)
        )
        gr = cursor.fetchone()

        conn.close()

        return jsonify({
            'invoice_no': invoice['invoice_number'],
            'po': {
                'uploaded':    po is not None,
                'po_id':       po['po_id'] if po else None,
                'po_no':       po['po_number'] if po else None,
                'filename':    po['file_name'] if po else None,
            },
            'gr': {
                'uploaded':    gr is not None,
                'gr_id':       gr['gr_id'] if gr else None,
                'gr_no':       gr['gr_number'] if gr else None,
                'filename':    gr['file_name'] if gr else None,
            }
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
