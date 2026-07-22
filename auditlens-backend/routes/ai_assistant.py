"""AI Audit Assistant — contextual, on-demand AI help for the auditor
reviewing ONE invoice case (Record Detail page). NOT a general chatbot:
every endpoint here is scoped to a single document_id and the AI only
ever sees data AuditLens' OWN engines already computed (three-way
matching via routes.auditor.build_comparison (legacy _build_comparison,
or the Enterprise V3 Phase 2 engine when enabled)/_classify_exception,
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
from routes.auditor import build_comparison, _classify_exception, _matching_status_for_comparison
from helpers.transaction_packages import get_transaction_context_for_document, get_package_documents

ai_assistant_bp = Blueprint('ai_assistant', __name__)


def _classify_anomaly(anomaly):
    """Blocking vs informational — a DETERMINISTIC classification (never
    left to the AI to judge), so a historical/already-handled anomaly
    can never be narrated as an active exception:
      - 'informational': already reviewed or dismissed by an auditor
        (status != 'pending'), or a pending low/medium-severity finding
        of a type that isn't inherently high-stakes (e.g. a round-number
        or weekend-submission pattern).
      - 'blocking': still pending AND either high severity, or an
        amount/duplicate finding — types that map directly to the
        task's "unresolved duplicate" / "amount inconsistency" /
        "high risk anomaly" categories.
    """
    if anomaly.get('status') != 'pending':
        return 'informational'  # already reviewed issue
    if anomaly.get('severity') == 'high':
        return 'blocking'  # high risk anomaly requiring action
    if anomaly.get('anomaly_type') in ('duplicate', 'amount'):
        return 'blocking'  # unresolved duplicate / amount inconsistency
    return 'informational'  # low risk pattern


def _compute_audit_status(comparison, authenticity, missing_documents, document_status, anomalies):
    """Deterministic PASS / REVIEW REQUIRED verdict, computed here in
    Python (never left for the AI to infer) so the AI Assistant can
    never narrate a clean record as a failed audit, or vice versa —
    this is the actual fix for that class of bug, not just better
    prompt wording. Returns (audit_status, reasons: list[str]).

    PASS requires ALL of: three-way matching PASS, no authenticity
    warning, no unresolved send-back (document not currently
    'returned') — AND no 'blocking' anomaly (see _classify_anomaly
    above). Anything else is REVIEW REQUIRED, with `reasons` listing
    exactly which condition(s) failed — the ONLY things the AI is
    allowed to describe as requiring auditor action.

    Enterprise V3 Phase 4: when `comparison` is V2-shaped (engine_
    version == 'v2'), the matching contribution to `reasons` comes from
    V2's own invoice_result.issues instead of legacy's overall_status +
    missing_documents — this is what stops a missing-GR-only warning
    (non-blocking under V2) or a partially-fulfilled PO (a PO-level
    fact, not a per-invoice problem) from incorrectly forcing REVIEW
    REQUIRED once V2 already correctly resolved the invoice as PASS.
    """
    authenticity_ok = not any((v or {}).get('status') == 'warning' for v in (authenticity or {}).values())
    send_back_unresolved = document_status == 'returned'
    blocking_anomalies = [a for a in anomalies if a.get('classification') == 'blocking']

    reasons = []
    if comparison.get('engine_version') == 'v2':
        inv_result = comparison['invoice_result']
        if inv_result['status'] != 'PASS':
            reasons.extend(inv_result['issues'] or ['Enterprise matching flagged this invoice for review'])
    else:
        overall_status = comparison['match_result'].get('overall_status')
        if overall_status != 'PASS':
            reasons.append(f"Three-way matching status is {overall_status}")
        if missing_documents:
            reasons.append(f"Missing: {', '.join(missing_documents)}")

    if not authenticity_ok:
        reasons.append('Authenticity check flagged a warning')
    if send_back_unresolved:
        reasons.append('Document is sent back to Finance and awaiting response')
    for a in blocking_anomalies:
        reasons.append(f"Unresolved {a.get('anomaly_type')} anomaly ({a.get('severity')} severity)")

    if reasons:
        return 'REVIEW REQUIRED', reasons
    return 'PASS', ['All core checks passed and no blocking findings']


def _v2_ai_context_fields(comparison):
    """Enterprise V3 Phase 4 (STEP 4) — additive AI-context fields,
    populated only when the Enterprise V2 engine actually ran for this
    invoice (comparison['engine_version'] == 'v2'); otherwise every
    field is None/False so the AI Assistant's existing prompts are
    completely unaffected until it's actually looking at a V2 case. No
    AI call, no new architecture — this is pure Python data assembly
    from fields build_comparison()/_build_comparison_v2() already
    computed."""
    if comparison.get('engine_version') != 'v2':
        return {
            'matching_engine_version': 'legacy',
            'relationship_mode': False,
            'related_document_count': None,
            'related_invoice_count': None,
            'related_gr_count': None,
            'cumulative_po_quantity': None,
            'cumulative_invoice_quantity': None,
            'cumulative_received_quantity': None,
            'remaining_quantity': None,
            'fulfilment_status': None,
        }

    po_fulfilment = comparison.get('po_fulfilment') or []
    related_invoices = comparison.get('related_invoices') or []
    related_pos = comparison.get('related_purchase_orders') or []
    related_grs = comparison.get('related_goods_receipts') or []

    return {
        'matching_engine_version': 'v2',
        'relationship_mode': True,
        'related_document_count': len(related_invoices) + len(related_pos) + len(related_grs),
        'related_invoice_count': len(related_invoices),
        'related_gr_count': len(related_grs),
        'cumulative_po_quantity': sum((pf['ordered_quantity'] or 0) for pf in po_fulfilment) if po_fulfilment else None,
        'cumulative_invoice_quantity': sum((pf['invoiced_quantity_cumulative'] or 0) for pf in po_fulfilment) if po_fulfilment else None,
        'cumulative_received_quantity': sum((pf['received_quantity_cumulative'] or 0) for pf in po_fulfilment) if po_fulfilment else None,
        'remaining_quantity': sum((pf['remaining_to_invoice'] or 0) for pf in po_fulfilment) if po_fulfilment else None,
        'fulfilment_status': po_fulfilment[0]['status'] if po_fulfilment else None,
    }


def _transaction_ai_context_fields(document_id, comparison):
    """Enterprise V3 Phase 6 (STEP 7) — additive AI-context describing
    the Finance Transaction Package (Phase 5) this invoice belongs to,
    if any. Returns None for a standalone/legacy invoice (no package)
    — the AI's existing prompts already work correctly without this
    field, matching STEP 10's backward-compatibility requirement. No
    AI call, no new calculation: package_name/related documents come
    straight from helpers/transaction_packages.py (Phase 5, unmodified)
    and allocation_summary is read directly from build_comparison()'s
    own already-computed po_fulfilment (Phase 2, unmodified)."""
    context = get_transaction_context_for_document(document_id, 'invoice')
    if not context:
        return None

    docs = get_package_documents(context['transaction_package_id'])
    related_invoices = [
        {'invoice_number': inv.get('invoice_number'), 'amount': inv.get('total_amount'), 'currency': inv.get('currency')}
        for inv in docs['invoices']
    ]
    related_purchase_orders = [
        {'po_number': po.get('po_number'), 'amount': po.get('total_amount'), 'currency': po.get('currency')}
        for po in docs['purchase_orders']
    ]
    related_goods_receipts = [{'gr_number': gr.get('gr_number')} for gr in docs['goods_receipts']]

    allocation_summary = None
    if comparison.get('engine_version') == 'v2' and comparison.get('po_fulfilment'):
        pf = comparison['po_fulfilment'][0]
        allocation_summary = {
            'po_ordered_quantity':          pf.get('ordered_quantity'),
            'po_amount':                    pf.get('po_amount'),
            'invoiced_amount_cumulative':   pf.get('invoiced_amount_cumulative'),
            'remaining_amount':             pf.get('remaining_amount'),
            'fulfilment_status':            pf.get('status'),
        }

    return {
        'package_name':             context['package_name'],
        'related_invoices':         related_invoices,
        'related_purchase_orders':  related_purchase_orders,
        'related_goods_receipts':   related_goods_receipts,
        'allocation_summary':       allocation_summary,
    }


def _build_case_context(cursor, document_id):
    """Structured, AI-facing snapshot of ONE case — every value here is
    read from data AuditLens already computed elsewhere (three-way
    matching, authenticity, anomaly detection); nothing is derived or
    guessed here. Returns None if the invoice document doesn't exist
    (same contract as _build_comparison)."""
    comparison = build_comparison(cursor, document_id)
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
    for a in anomalies:
        a['classification'] = _classify_anomaly(a)

    document_status = doc_row['status'] if doc_row else None
    audit_status, audit_status_reasons = _compute_audit_status(
        comparison, authenticity, missing_documents, document_status, anomalies)

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

    # The auditor's own structured send-back request (Finance Correction
    # Center's "Auditor Request" panel already shows this) — added so
    # the Finance-facing AI actions (explain-issue/generate-response/
    # recommended-steps, routes below) can ground their wording in the
    # ACTUAL reason/instruction/required actions/priority an auditor
    # gave, instead of only the generic exception classification above.
    # None when the document was never returned via the structured form.
    cursor.execute(
        '''SELECT cycle_number, return_reason_category, reason_other_note, auditor_instruction,
                  required_actions, priority, response_due_date, cycle_status, sent_back_at
           FROM send_back_cycles WHERE document_id = %s ORDER BY cycle_number DESC LIMIT 1''',
        (document_id,)
    )
    cycle_row = cursor.fetchone()
    send_back_cycle = None
    if cycle_row:
        send_back_cycle = {
            'reason_category':     cycle_row['return_reason_category'],
            'reason_other_note':   cycle_row['reason_other_note'],
            'auditor_instruction': cycle_row['auditor_instruction'],
            'required_actions':    cycle_row['required_actions'],
            'priority':            cycle_row['priority'],
            'response_due_date':   cycle_row['response_due_date'].isoformat() if cycle_row['response_due_date'] else None,
            'cycle_status':        cycle_row['cycle_status'],
            'sent_back_at':        cycle_row['sent_back_at'].isoformat() if cycle_row['sent_back_at'] else None,
        }

    return {
        'invoice_number':     comparison['invoice']['invoice_no'],
        'vendor':             comparison['invoice']['vendor_name'],
        'amount':             comparison['invoice']['total_amount'],
        'currency':           comparison['invoice']['currency'],
        'invoice_date':       comparison['invoice']['invoice_date'],
        'uploaded_at':        comparison['invoice']['uploaded_at'],
        'ocr_confidence':     comparison['invoice']['ocr_confidence'],
        'po_uploaded':        comparison['po'] is not None,
        'gr_uploaded':        comparison['gr'] is not None,
        'missing_documents':  missing_documents,
        'matching_status':    _matching_status_for_comparison(comparison),
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
        'document_status':    document_status,
        'audit_status':          audit_status,
        'audit_status_reasons':  audit_status_reasons,
        'send_back_cycle':       send_back_cycle,
        **_v2_ai_context_fields(comparison),
        'transaction_context':   _transaction_ai_context_fields(document_id, comparison),
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


def _clamp_explain_exception_result(result, context):
    """Never trust the AI's own PASS/REVIEW REQUIRED verdict — always
    use the DETERMINISTICALLY computed context['audit_status'] instead,
    regardless of what the AI narrated. This is what actually prevents
    the AI from describing a clean record (matching PASS, authenticity
    PASS, no missing documents, no unresolved send-back, no blocking
    anomaly) as a failed audit just because an informational/already-
    reviewed anomaly happens to exist — the wording can vary, but the
    Audit Status label itself can never be wrong."""
    result = result or {}
    audit_status = context.get('audit_status', 'REVIEW REQUIRED')

    reason = (result.get('reason') or '').strip()
    if not reason:
        reason = '; '.join(context.get('audit_status_reasons') or []) or 'See case details.'

    recommended_action = (result.get('recommended_action') or '').strip()
    if not recommended_action:
        recommended_action = ('No action required — ready for approval.' if audit_status == 'PASS'
                               else 'Review the flagged items before approving.')

    return {
        'audit_status':        audit_status,
        'reason':              reason,
        'recommended_action':  recommended_action,
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
    elif action == 'explain_exception':
        result = _clamp_explain_exception_result(result, context)

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


def _require_finance_owner(document_id):
    """Finance-side counterpart of _require_auditor() above — Finance
    can only run the AI assistant against an invoice they uploaded
    themselves, the SAME ownership rule already enforced by
    routes/documents.py::serve_document_file for finance_executive."""
    user_id = get_jwt_identity()
    user = get_user_by_id(user_id)
    if user['role'] != 'finance_executive':
        return None, (jsonify({'error': 'Access denied. Finance Executive only.'}), 403)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT uploaded_by FROM documents WHERE document_id = %s', (document_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None, (jsonify({'error': 'Invoice document not found'}), 404)
    if row[0] != user['user_id']:
        return None, (jsonify({'error': 'Access denied'}), 403)
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


# ============================================================
# FINANCE AI CORRECTION ASSISTANT
# Finance Correction Center's own AI card (Finance Correction Detail
# page). Distinct routes/role-check from the auditor endpoints above,
# but reuses the SAME _build_case_context/_run_action/caching plumbing
# — nothing about how the AI is called or cached is duplicated.
# ============================================================

# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/finance/explain-issue
# Reuses the exact SAME 'explain_exception' action (prompt, output
# shape, and _clamp_explain_exception_result) already used by the
# auditor's "Explain Exception" — the audit_status verdict + reasoning
# rules are the same regardless of which side is asking.
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/finance/explain-issue', methods=['POST'])
@jwt_required()
def finance_explain_issue(document_id):
    _, err = _require_finance_owner(document_id)
    if err:
        return err
    try:
        response, status = _run_action(document_id, 'explain_exception')
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/finance/generate-response
# Drafts a Finance -> Auditor response for the Finance Response field —
# never auto-submitted, the auditor can edit before resubmitting
# (same "draft only" contract as the auditor's generate-remark/
# prepare-send-back actions).
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/finance/generate-response', methods=['POST'])
@jwt_required()
def finance_generate_response(document_id):
    _, err = _require_finance_owner(document_id)
    if err:
        return err
    try:
        response, status = _run_action(document_id, 'generate_finance_response')
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/finance/recommended-steps
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/finance/recommended-steps', methods=['POST'])
@jwt_required()
def finance_recommended_steps(document_id):
    _, err = _require_finance_owner(document_id)
    if err:
        return err
    try:
        response, status = _run_action(document_id, 'recommended_steps')
        return jsonify(response), status
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# POST /ai-assistant/<document_id>/finance/ask
# Body: {"question": "..."}
# Reuses the exact SAME 'ask' action as the auditor's /ask — an audit
# case fact is the same regardless of which role is asking, so the
# question+context cache key (and any existing cached answer) is
# shared rather than duplicated per role.
# ------------------------------------------------------------
@ai_assistant_bp.route('/<int:document_id>/finance/ask', methods=['POST'])
@jwt_required()
def finance_ask(document_id):
    _, err = _require_finance_owner(document_id)
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
