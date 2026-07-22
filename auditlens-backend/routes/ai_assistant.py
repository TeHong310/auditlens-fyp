"""AI Audit Assistant — contextual, on-demand AI help for the auditor
reviewing ONE invoice case (Record Detail page). NOT a general chatbot:
every endpoint here is scoped to a single document_id and the AI only
ever sees data AuditLens' OWN engines already computed (three-way
matching via routes.auditor._build_comparison/_classify_exception,
authenticity_checks, anomalies, review_records) — nothing here adds to
or changes extraction/matching/authenticity/anomaly logic.

AI is called ONLY when the auditor explicitly clicks a button (never on
page load) — one POST endpoint per action below. Every response is
cached in ai_assistant_cache, keyed by (document_id, action, hash of
the case data + question) — an unchanged case + the same question never
re-spends a Claude/Gemini call.
"""
import hashlib
import json
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.ai_assistant import ask_ai_assistant
from helpers.send_back import REASON_CATEGORIES, REQUIRED_ACTIONS, PRIORITIES
from routes.auditor import _build_comparison, _classify_exception

ai_assistant_bp = Blueprint('ai_assistant', __name__)


def _build_case_context(cursor, document_id):
    """Structured, AI-facing snapshot of ONE case — every value here is
    read from data AuditLens already computed elsewhere (three-way
    matching, authenticity, anomaly detection); nothing is derived or
    guessed here. Returns None if the invoice document doesn't exist
    (same contract as _build_comparison)."""
    comparison = _build_comparison(cursor, document_id)
    if not comparison:
        return None
    mr = comparison['match_result']

    cursor.execute('SELECT document_id, uploaded_at, status FROM documents WHERE document_id = %s', (document_id,))
    doc_row = cursor.fetchone()

    exception_info = None
    classified = _classify_exception(cursor, doc_row, comparison) if doc_row else None
    if classified:
        _, exc_type, label, detail, severity = classified
        exception_info = {'type': exc_type, 'label': label, 'detail': detail, 'severity': severity}

    missing_documents = []
    if not comparison['po']:
        missing_documents.append('Purchase Order')
    if not comparison['gr']:
        missing_documents.append('Goods Receipt')

    cursor.execute(
        'SELECT document_type, authenticity_status, risk_level FROM authenticity_checks WHERE document_id = %s',
        (document_id,)
    )
    authenticity = {
        row['document_type']: {'status': row['authenticity_status'], 'risk_level': row['risk_level']}
        for row in cursor.fetchall()
    }

    cursor.execute(
        '''SELECT anomaly_type, severity, detected_pattern, ai_explanation, status
           FROM anomalies WHERE invoice_document_id = %s''',
        (document_id,)
    )
    anomalies = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        '''SELECT rr.action, rr.remarks, rr.reviewed_at, u.full_name AS reviewer_name
           FROM review_records rr JOIN users u ON rr.reviewed_by = u.user_id
           WHERE rr.document_id = %s ORDER BY rr.reviewed_at ASC''',
        (document_id,)
    )
    audit_history = [{
        'action':      row['action'],
        'remarks':     row['remarks'],
        'reviewed_at': row['reviewed_at'].isoformat() if row['reviewed_at'] else None,
        'reviewer':    row['reviewer_name'],
    } for row in cursor.fetchall()]

    return {
        'invoice_number':     comparison['invoice']['invoice_no'],
        'vendor':             comparison['invoice']['vendor_name'],
        'amount':             comparison['invoice']['total_amount'],
        'currency':           comparison['invoice']['currency'],
        'invoice_date':       comparison['invoice']['invoice_date'],
        'po_uploaded':        comparison['po'] is not None,
        'gr_uploaded':        comparison['gr'] is not None,
        'missing_documents':  missing_documents,
        'matching_status':    mr['overall_status'],
        'matching_details': {
            'vendor_match':           mr['vendor_match'],
            'amount_match':           mr['amount_match'],
            'po_reference_match':     mr['po_reference_match'],
            'line_items_match':       mr['line_items_match'],
            'line_items_price_match': mr['line_items_price_match'],
        },
        'exception':          exception_info,
        'authenticity':       authenticity or None,
        'anomalies':           anomalies,
        'audit_history':      audit_history,
        'document_status':    doc_row['status'] if doc_row else None,
    }


def _clamp_send_back_result(result, exception_info):
    """Never trust the AI's enum choices blindly — clamp reason_category/
    required_actions/priority to the SAME valid values routes/reviews.py
    already enforces server-side (helpers/send_back.py), falling back to
    a sensible default derived from the case's own exception
    classification if the AI returned something invalid or empty."""
    result = result or {}

    reason_category = result.get('reason_category')
    if reason_category not in REASON_CATEGORIES:
        reason_category = 'missing_document' if exception_info and exception_info.get('type') == 'missing_document' else 'other'

    required_actions = [a for a in (result.get('required_actions') or []) if a in REQUIRED_ACTIONS]
    if not required_actions:
        required_actions = ['upload_missing_document'] if reason_category == 'missing_document' else ['other']

    priority = result.get('priority')
    if priority not in PRIORITIES:
        priority = 'medium' if exception_info and exception_info.get('severity') == 'medium' else 'normal'

    instruction = (result.get('instruction') or '').strip()
    if not instruction:
        instruction = 'Please review and provide the required information.'

    return {
        'reason_category':  reason_category,
        'required_actions': required_actions,
        'priority':          priority,
        'instruction':       instruction,
    }


def _cache_key(context, question):
    payload = json.dumps({'context': context, 'question': question}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _get_cached(cursor, document_id, action, context_hash):
    cursor.execute(
        'SELECT response FROM ai_assistant_cache WHERE document_id = %s AND action = %s AND context_hash = %s',
        (document_id, action, context_hash)
    )
    row = cursor.fetchone()
    return row['response'] if row else None


def _save_cache(document_id, action, context_hash, response):
    """Best-effort — a cache write failure must never break the response
    the auditor already received, it just means the next identical call
    calls the AI again (same fail-soft convention as claude_cache.py/
    gemini_cache.py)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO ai_assistant_cache (document_id, action, context_hash, response)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (document_id, action, context_hash) DO UPDATE SET response = EXCLUDED.response''',
            (document_id, action, context_hash, psycopg2.extras.Json(response))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'WARNING: ai_assistant_cache write failed: {type(e).__name__}: {e}')


def _run_action(document_id, action, question=None):
    """Shared by every endpoint below (caller already checked the
    auditor role). Builds the case context, serves a cached response if
    one exists for this exact context+question, otherwise calls the AI
    once and caches the result. Returns (response_dict, status_code)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    context = _build_case_context(cursor, document_id)
    if context is None:
        conn.close()
        return {'error': 'Invoice document not found'}, 404

    context_hash = _cache_key(context, question)
    cached = _get_cached(cursor, document_id, action, context_hash)
    conn.close()
    if cached is not None:
        print(f"DEBUG AI ASSISTANT: cache hit for document_id={document_id} action={action} — no AI call")
        return {**cached, 'cached': True}, 200

    result, provider = ask_ai_assistant(action, context, question)
    if result is None:
        return {'error': 'AI Assistant is unavailable right now — see server logs'}, 502

    if action == 'prepare_send_back':
        result = _clamp_send_back_result(result, context.get('exception'))

    response = {**result, 'provider': provider, 'cached': False}
    _save_cache(document_id, action, context_hash, response)
    return response, 200


def _require_auditor():
    """Returns (user, None) on success, or (None, (response, status)) to
    return immediately — same role-check pattern as every other route
    module in this app."""
    user_id = get_jwt_identity()
    user = get_user_by_id(user_id)
    if user['role'] != 'auditor':
        return None, (jsonify({'error': 'Access denied. Auditor only.'}), 403)
    return user, None


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/explain-exception
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/explain-exception', methods=['POST'])
@jwt_required()
def explain_exception(document_id):
    _, err = _require_auditor()
    if err:
        return err
    try:
        response, status = _run_action(document_id, 'explain_exception')
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/explain-risk
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/explain-risk', methods=['POST'])
@jwt_required()
def explain_risk(document_id):
    _, err = _require_auditor()
    if err:
        return err
    try:
        response, status = _run_action(document_id, 'explain_risk')
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/generate-remark
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/generate-remark', methods=['POST'])
@jwt_required()
def generate_remark(document_id):
    _, err = _require_auditor()
    if err:
        return err
    try:
        response, status = _run_action(document_id, 'generate_remark')
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/prepare-send-back
# Only pre-fills the EXISTING Send Back form — never submits it.
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/prepare-send-back', methods=['POST'])
@jwt_required()
def prepare_send_back(document_id):
    _, err = _require_auditor()
    if err:
        return err
    try:
        response, status = _run_action(document_id, 'prepare_send_back')
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/ask
# Body: {"question": "..."}
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/ask', methods=['POST'])
@jwt_required()
def ask(document_id):
    _, err = _require_auditor()
    if err:
        return err

    data = request.get_json() or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'question is required'}), 400

    try:
        response, status = _run_action(document_id, 'ask', question=question)
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500
