from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id

authenticity_bp = Blueprint('authenticity', __name__)


# ------------------------------------------------------------
# GET SINGLE AUTHENTICITY CHECK
# GET /authenticity/<document_id>
# Auditor only
# ------------------------------------------------------------
@authenticity_bp.route('/<int:document_id>', methods=['GET'])
@jwt_required()
def get_authenticity_check(document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            '''SELECT check_id, document_id, has_company_chop, has_company_logo,
                      has_company_name, ai_notes, created_at
               FROM authenticity_checks WHERE document_id = %s''',
            (document_id,)
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({'error': 'No authenticity check for this document'}), 404

        return jsonify(row), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET ALL AUTHENTICITY CHECKS (list view)
# GET /authenticity
# Auditor only
# ------------------------------------------------------------
@authenticity_bp.route('', methods=['GET'])
@jwt_required()
def get_authenticity_checks():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            '''SELECT ac.check_id, ac.document_id, ef.invoice_number, ef.vendor_name,
                      ac.has_company_chop, ac.has_company_logo, ac.has_company_name,
                      ac.ai_notes, ac.created_at
               FROM authenticity_checks ac
               LEFT JOIN extracted_fields ef ON ac.document_id = ef.document_id
               ORDER BY ac.created_at DESC'''
        )
        rows = cursor.fetchall()
        conn.close()

        return jsonify(rows), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
