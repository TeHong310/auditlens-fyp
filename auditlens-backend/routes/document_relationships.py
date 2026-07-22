"""Enterprise V3 Phase 1 relationship API — GET/POST/DELETE endpoints for
document_relationships (see helpers/document_relationships.py for the
service layer, app.py's _ensure_document_relationships_table() for the
schema). Registered under the same '/documents' url_prefix as routes/
documents.py, as a separate blueprint so this purely-additive feature
never touches the existing upload/OCR route file.

Does NOT replace or call _build_comparison() (routes/auditor.py) — the
existing matching engine and every page built on it are untouched."""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.document_relationships import (
    VALID_TYPES, RELATIONSHIP_TYPE_PAIRS, _ENTITY_TABLES,
    create_relationship, delete_relationship, get_relationship_by_id,
    get_related_documents, get_related_invoices, get_related_purchase_orders,
    get_related_goods_receipts,
)
from routes.auditor import build_comparison, build_shadow_comparison

document_relationships_bp = Blueprint('document_relationships', __name__)


def _owner_id(cursor, doc_type, doc_id):
    """Returns the uploaded_by user_id for the given (doc_type, doc_id)
    node, or None if the node doesn't exist. documents/purchase_orders/
    goods_receipts each carry their own uploaded_by column."""
    table, pk = _ENTITY_TABLES[doc_type]
    cursor.execute(f'SELECT uploaded_by FROM {table} WHERE {pk} = %s', (doc_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def _require_node_access(doc_type, doc_id):
    """Returns (user, None) on success, or (None, (response, status)) to
    return immediately — same role-check pattern as routes/documents.py's
    _require_timeline_access() / routes/ai_assistant.py's
    _require_finance_owner(). Auditor: any node. Finance Executive: only
    an invoice/PO/GR they uploaded themselves."""
    user_id = get_jwt_identity()
    user = get_user_by_id(user_id)

    if user['role'] not in ('auditor', 'finance_executive'):
        return None, (jsonify({'error': 'Access denied'}), 403)

    if user['role'] == 'finance_executive':
        conn = get_db_connection()
        cursor = conn.cursor()
        owner_id = _owner_id(cursor, doc_type, doc_id)
        conn.close()
        if owner_id is None:
            return None, (jsonify({'error': f'{doc_type} {doc_id} not found'}), 404)
        if owner_id != user['user_id']:
            return None, (jsonify({'error': 'Access denied'}), 403)

    return user, None


def _require_auditor_only():
    """Stricter than _require_node_access() above — Enterprise V3 Phase
    3's shadow-comparison debug endpoint deliberately excludes Finance
    (even for a document they own), since it's an internal engine-
    validation tool, not a document-review feature. Returns (user, None)
    on success, or (None, (response, status))."""
    user_id = get_jwt_identity()
    user = get_user_by_id(user_id)
    if user['role'] != 'auditor':
        return None, (jsonify({'error': 'Access denied. Auditor only.'}), 403)
    return user, None


@document_relationships_bp.route('/<int:document_id>/matching-comparison', methods=['GET'])
@jwt_required()
def get_matching_comparison(document_id):
    """Enterprise V3 Phase 3 (STEP 4) — read-only debug endpoint: legacy
    vs Enterprise V2 matching results side by side, plus the differences
    between them. Auditor only (see _require_auditor_only — Finance is
    excluded even for a document they own). Works regardless of the
    current feature-flag state, since it calls build_shadow_comparison()
    directly rather than going through build_comparison()'s dispatch —
    an auditor can inspect a comparison without needing shadow mode
    enabled first. Never writes anything, never calls Claude/Gemini."""
    _, err = _require_auditor_only()
    if err:
        return err

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        comparison = build_shadow_comparison(cursor, document_id)
    finally:
        conn.close()

    if comparison is None:
        return jsonify({'error': 'Invoice document not found'}), 404
    return jsonify(comparison), 200


@document_relationships_bp.route('/<int:document_id>/relationships', methods=['GET'])
@jwt_required()
def get_document_relationships(document_id):
    """document_id is always an invoice (documents.document_id), consistent
    with every other '/documents/<id>/...' route in this app."""
    _, err = _require_node_access('invoice', document_id)
    if err:
        return err

    return jsonify({
        'document_id': document_id,
        'relationships': get_related_documents('invoice', document_id),
        'related_invoices': get_related_invoices('invoice', document_id),
        'related_purchase_orders': get_related_purchase_orders('invoice', document_id),
        'related_goods_receipts': get_related_goods_receipts('invoice', document_id),
    }), 200


@document_relationships_bp.route('/<int:document_id>/enterprise-matching', methods=['GET'])
@jwt_required()
def get_enterprise_matching(document_id):
    """Enterprise V3 Phase 2 (STEP 12) — exposes build_comparison()'s
    result (legacy shape, or the additive V2 shape when V2 is enabled
    and this invoice has explicit document_relationships) directly,
    since Phase 1's GET /<id>/relationships only returns raw links, not
    the cumulative matching result. Same access control as every other
    route in this file."""
    _, err = _require_node_access('invoice', document_id)
    if err:
        return err

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        result = build_comparison(cursor, document_id)
    finally:
        conn.close()

    if result is None:
        return jsonify({'error': 'Invoice document not found'}), 404
    return jsonify(result), 200


@document_relationships_bp.route('/relationships', methods=['POST'])
@jwt_required()
def create_document_relationship():
    data = request.get_json(silent=True) or {}
    parent_type = data.get('parent_type')
    parent_id = data.get('parent_id')
    child_type = data.get('child_type')
    child_id = data.get('child_id')
    relationship_type = data.get('relationship_type')

    if parent_type not in VALID_TYPES or child_type not in VALID_TYPES:
        return jsonify({'error': f'parent_type/child_type must be one of {VALID_TYPES}'}), 400
    if not isinstance(parent_id, int) or not isinstance(child_id, int):
        return jsonify({'error': 'parent_id and child_id must be integers'}), 400
    if relationship_type not in RELATIONSHIP_TYPE_PAIRS:
        return jsonify({'error': f'relationship_type must be one of {tuple(RELATIONSHIP_TYPE_PAIRS)}'}), 400

    _, err = _require_node_access(parent_type, parent_id)
    if err:
        return err
    _, err = _require_node_access(child_type, child_id)
    if err:
        return err

    relationship, error = create_relationship(
        parent_type, parent_id, child_type, child_id, relationship_type,
        matched_quantity=data.get('matched_quantity'),
        matched_amount=data.get('matched_amount'),
        confidence_score=data.get('confidence_score'),
    )
    if error:
        return jsonify({'error': error}), 400
    return jsonify(relationship), 201


@document_relationships_bp.route('/relationships/<int:relationship_id>', methods=['DELETE'])
@jwt_required()
def delete_document_relationship(relationship_id):
    relationship = get_relationship_by_id(relationship_id)
    if not relationship:
        return jsonify({'error': 'Relationship not found'}), 404

    _, err = _require_node_access(relationship['parent_type'], relationship['parent_id'])
    if err:
        return err
    _, err = _require_node_access(relationship['child_type'], relationship['child_id'])
    if err:
        return err

    delete_relationship(relationship_id)
    return jsonify({'message': 'Relationship deleted'}), 200
