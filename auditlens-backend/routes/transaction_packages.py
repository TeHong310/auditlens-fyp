"""Enterprise V3 Phase 5 — Finance Transaction Package API. Lets
Finance group related AP documents into one package before auditor
review (see helpers/transaction_packages.py for the service layer,
app.py's _ensure_transaction_packages_table()/_ensure_transaction_
package_documents_table() for the schema).

Does NOT provide its own upload endpoint — documents are uploaded via
the EXISTING, unmodified /documents/upload, /documents/upload-po/<id>,
/documents/upload-gr/<id> endpoints; this blueprint only links already-
uploaded document_ids into a package. Finance-only throughout."""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from db import get_user_by_id
from helpers.transaction_packages import (
    create_package, get_package, link_document_to_package,
    get_package_documents, get_relationship_preview, list_packages,
)

transaction_packages_bp = Blueprint('transaction_packages', __name__)


def _require_finance():
    """Returns (user, None) on success, or (None, (response, status))."""
    user_id = get_jwt_identity()
    user = get_user_by_id(user_id)
    if user['role'] != 'finance_executive':
        return None, (jsonify({'error': 'Access denied. Finance Executive only.'}), 403)
    return user, None


def _require_package_owner(package_id, user):
    """Returns (package, None) on success, or (None, (response, status))."""
    package = get_package(package_id)
    if not package:
        return None, (jsonify({'error': 'Transaction package not found'}), 404)
    if package['created_by'] != user['user_id']:
        return None, (jsonify({'error': 'Access denied'}), 403)
    return package, None


@transaction_packages_bp.route('', methods=['POST'])
@jwt_required()
def create_transaction_package():
    user, err = _require_finance()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    package_name = (data.get('package_name') or '').strip()
    if not package_name:
        return jsonify({'error': 'package_name is required'}), 400

    package = create_package(package_name, user['user_id'])
    return jsonify(package), 201


@transaction_packages_bp.route('', methods=['GET'])
@jwt_required()
def get_transaction_packages():
    user, err = _require_finance()
    if err:
        return err

    packages = list_packages(user['user_id'])
    return jsonify(packages), 200


@transaction_packages_bp.route('/<int:package_id>', methods=['GET'])
@jwt_required()
def get_transaction_package_detail(package_id):
    user, err = _require_finance()
    if err:
        return err
    package, err = _require_package_owner(package_id, user)
    if err:
        return err

    return jsonify({
        'package': package,
        'documents': get_package_documents(package_id),
        'relationship_preview': get_relationship_preview(package_id),
    }), 200


@transaction_packages_bp.route('/<int:package_id>/documents', methods=['POST'])
@jwt_required()
def add_transaction_package_document(package_id):
    user, err = _require_finance()
    if err:
        return err
    _, err = _require_package_owner(package_id, user)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    document_id = data.get('document_id')
    document_role = data.get('document_role')
    if not isinstance(document_id, int):
        return jsonify({'error': 'document_id must be an integer'}), 400

    link, error = link_document_to_package(package_id, document_id, document_role)
    if error:
        return jsonify({'error': error}), 400

    package = get_package(package_id)
    return jsonify({'link': link, 'package': package}), 201
