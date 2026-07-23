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
    resolve_package_for_document, delete_empty_package, delete_package,
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


# ------------------------------------------------------------
# DELETE AN EMPTY TRANSACTION PACKAGE
# DELETE /transaction-packages/<package_id>
# Finance Executive only, owner only, EMPTY packages only.
#
# Phase 9 — cleans up the "ghost draft package" left behind when every
# document Finance meant to add to a brand new package auto-groups
# (Phase 7.1) into a different, already-existing package instead. The
# frontend calls this after the create-package flow when it detects
# that outcome (see finance-transaction-create.component.ts).
# ------------------------------------------------------------
@transaction_packages_bp.route('/<int:package_id>', methods=['DELETE'])
@jwt_required()
def delete_transaction_package(package_id):
    user, err = _require_finance()
    if err:
        return err
    _, err = _require_package_owner(package_id, user)
    if err:
        return err

    deleted = delete_empty_package(package_id)
    if not deleted:
        return jsonify({'error': 'Only an empty transaction package (no linked documents) can be deleted'}), 400

    return jsonify({'message': 'Empty transaction package deleted'}), 200


# ------------------------------------------------------------
# DELETE A TRANSACTION PACKAGE AND ITS DOCUMENTS (management feature)
# DELETE /transaction-packages/<package_id>/force
# Finance Executive only, owner only.
#
# Phase 15 — a separate, explicit endpoint from the plain DELETE above
# on purpose: that route is called automatically and silently by the
# create-package flow (finance-transaction-create.component.ts) to
# clean up an empty "ghost" package, and must keep behaving exactly as
# it always has (refuse anything non-empty). This route is the
# deliberate, user-confirmed "delete this package and everything in
# it" action — only ever called from an explicit Delete button behind
# a confirmation modal.
# ------------------------------------------------------------
@transaction_packages_bp.route('/<int:package_id>/force', methods=['DELETE'])
@jwt_required()
def force_delete_transaction_package(package_id):
    user, err = _require_finance()
    if err:
        return err
    _, err = _require_package_owner(package_id, user)
    if err:
        return err

    try:
        result = delete_package(package_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        return jsonify({'error': f'Failed to delete transaction package: {e}'}), 500

    return jsonify({'message': 'Transaction package deleted', **result}), 200


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

    # Phase 7.1 (auto-grouping, not blocking): if this document's own PO
    # reference matches a PO already anchoring a DIFFERENT package this
    # same Finance user owns, land it there instead of fragmenting the
    # same AP transaction into two packages. Falls back to the
    # requested package_id whenever no match is found — and, per Phase 9,
    # ALSO on any unexpected failure in the lookup itself: this is a
    # best-effort auto-grouping convenience, matching this module's own
    # established "must never block the primary action" philosophy
    # (see _rebuild_relationships_for_package/_ensure_sibling_checks
    # elsewhere in this app). Its failure must never mean the document
    # doesn't get attached to a package at all.
    try:
        resolved_package_id = resolve_package_for_document(package_id, document_id, document_role, user['user_id'])
    except Exception as e:
        print(f"WARNING: resolve_package_for_document failed for document_id={document_id} "
              f"role={document_role} requested_package_id={package_id}: {type(e).__name__}: {e}")
        resolved_package_id = package_id

    link, error = link_document_to_package(resolved_package_id, document_id, document_role)
    if error:
        return jsonify({'error': error}), 400

    package = get_package(resolved_package_id)
    return jsonify({'link': link, 'package': package, 'redirected_from_package_id': package_id if resolved_package_id != package_id else None}), 201
