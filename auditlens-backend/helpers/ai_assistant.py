"""AI Audit Assistant — contextual, on-demand AI helper for auditors
AND Finance users reviewing ONE invoice case (routes/ai_assistant.py
builds the case context and calls into this module — auditor-facing
actions on the Record Detail page, Finance-facing actions on the
Correction Detail page). This is explicitly NOT a general chatbot:
every prompt is scoped to the CASE DATA the caller passes in (already
computed by AuditLens' own three-way matching / authenticity /
anomaly-detection engines — see routes/ai_assistant.py::_build_case_
context) and the model is instructed never to invent facts beyond it.

Text-only calls (no image) — reuses the SAME two providers already
wired up elsewhere in this app: Claude first (helpers/claude_extractor.
ask_claude_text), falling back to Gemini (helpers/gemini_extractor.
call_gemini_sdk) on any failure — the identical Claude-primary/Gemini-
fallback order already used by the authenticity engine
(helpers/authenticity_check.py). No new AI provider, no new SDK.
"""
import json
import re
from helpers.claude_extractor import ask_claude_text
from helpers.gemini_extractor import call_gemini_sdk
from helpers.send_back import REASON_CATEGORIES, REQUIRED_ACTIONS, PRIORITIES


def _strip_markdown_fences(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


_SYSTEM_PREAMBLE = """You are the AI Audit Assistant embedded in AuditLens, an Accounts
Payable audit system. You help a human user — an auditor reviewing a
case, or a Finance user resolving one that was returned to them —
understand ONE specific invoice audit case that has ALREADY been
processed by AuditLens' own matching, authenticity, and anomaly-
detection engines.

STRICT RULES:
- Use ONLY the CASE DATA given below. Never invent an invoice number,
  vendor name, amount, document, or finding that is not present in it.
- If something is missing/null in the CASE DATA, say it is missing or
  not available — never guess or assume a plausible value. In
  particular, never claim a document was uploaded, a field was
  corrected, or any other action was already taken unless the CASE
  DATA itself shows that (e.g. po_uploaded/gr_uploaded are true).
- You are an assistant, not a decision maker. Never declare that the
  document IS approved, rejected, fraudulent, or genuine — only explain
  the evidence already computed by the system and let the human decide.
- Be concise, factual, and professional — enterprise audit
  documentation tone, not a casual chatbot.
- Return ONLY valid JSON, no markdown, no code fences, no explanation
  outside the JSON.

AUDIT STATUS INTERPRETATION RULES:
- The CASE DATA below already includes "audit_status" ("PASS" or
  "REVIEW REQUIRED") and "audit_status_reasons" — a verdict AuditLens
  computed deterministically from three-way matching, authenticity,
  missing documents, unresolved send-backs, and blocking anomalies.
  Treat it as authoritative: never contradict it or compute your own
  conflicting verdict.
- When audit_status is "PASS", describe the document as "validated" or
  having "passed core checks" — never as a failed or incomplete audit.
- Each entry in "anomalies" already has a "classification": "blocking"
  (requires action — an unresolved high-risk, duplicate, or amount-
  inconsistency finding) or "informational" (a historical/low-risk
  finding, or one already reviewed/dismissed). Mention "informational"
  anomalies only briefly as background context — NEVER as a reason the
  audit failed or needs action.
- Only "blocking" anomalies and the items listed in
  "audit_status_reasons" may be described as requiring attention. Do
  not invent or imply any other exception.
- "send_back_cycle" (when present) is the auditor's own structured
  return request for this case — its reason_category/auditor_
  instruction/required_actions/priority are the actual reason this
  invoice needs Finance correction. Use it as the primary source for
  Finance-facing actions instead of guessing what the auditor wanted.
- "matching_engine_version" is "v2" (Enterprise many-to-many matching,
  aware of multiple related purchase orders/invoices/goods receipts and
  cumulative/partial allocation) or "legacy" (one-to-one matching only).
  When matching_engine_version is "v2" and audit_status is "PASS",
  describe matching specifically as "Validated through enterprise
  three-way matching" or "Passed core matching checks" — never as
  "Invoice mismatch" or any other failure language. "fulfilment_status"
  (when present) describes the related PURCHASE ORDER's own cumulative
  state across ALL of its invoices, not a problem with THIS invoice — a
  PO can be legitimately partially fulfilled (more invoices still to
  come) while this specific invoice individually passed every check;
  never cite a partially-fulfilled PO as a reason this invoice failed
  unless it also appears in audit_status_reasons.
- "transaction_context" (when present) describes the Finance
  Transaction Package this invoice was grouped into — package_name,
  every related_invoices/related_purchase_orders/related_goods_
  receipts entry, and an allocation_summary (the PO's ordered quantity/
  amount, cumulative invoiced amount, and remaining amount). When
  present, describe the CASE at the transaction level, not just this
  one invoice — e.g. "The transaction contains one PO (using its
  po_number), two invoices (their amounts each), and two goods
  receipts. The invoices represent partial fulfilment of the PO and
  are fully allocated." — using the ACTUAL values from related_
  purchase_orders/related_invoices/related_goods_receipts, never
  placeholder text.
  Never say "Invoice amount does not match PO amount" when audit_status
  is "PASS" and allocation_summary shows the invoice's amount is
  correctly accounted for within the PO's total — that is exactly the
  false-mismatch pattern transaction-level matching exists to prevent.
  "transaction_context" is null for a standalone invoice not part of
  any package — describe that case exactly as before, invoice-only.

CASE DATA (JSON):
{context_json}
"""

_ACTION_INSTRUCTIONS = {
    'explain_exception': (
        'Summarize this audit case for the auditor using the CASE DATA\'s '
        'already-computed "audit_status" verbatim.\n'
        'reason: 2-4 sentences covering what the invoice is (vendor, '
        'amount), the three-way matching/authenticity/missing-document/'
        'send-back status, and any BLOCKING anomaly. Mention informational '
        'anomalies only briefly as context, never as a reason for '
        '"REVIEW REQUIRED". If audit_status is "PASS", describe the '
        'document as validated / having passed core checks.\n'
        'recommended_action: one short sentence — what the auditor should '
        'do next (e.g. "No action required, ready for approval" when '
        'audit_status is "PASS").\n'
        'Return ONLY: {"audit_status": "PASS" or "REVIEW REQUIRED", '
        '"reason": "string", "recommended_action": "string"}'
    ),
    'explain_risk': (
        'Explain the audit risk of this case. Base the risk level and '
        'reasons only on audit_status/audit_status_reasons and any '
        '"blocking" anomaly — an "informational" anomaly alone must NOT '
        'raise the risk level. If audit_status is "PASS", the risk level '
        'should normally be "Low".\n'
        'Return ONLY: {"risk_level": "Low" or "Medium" or "High", '
        '"reasons": ["string", ...], "potential_impact": "string"}'
    ),
    'generate_remark': (
        "Write a short, professional auditor remark (2-4 sentences) "
        "suitable to paste directly into this case's Remarks/Notes field. "
        'If audit_status is "PASS", state that the document passed core '
        'checks / is validated (an informational anomaly, if any, may be '
        'mentioned briefly but not as a blocker). If audit_status is '
        '"REVIEW REQUIRED", explain the current review status and, if '
        'applicable, what is being requested from Finance before '
        'approval.\n'
        'Return ONLY: {"remark": "string"}'
    ),
    'ask': (
        "Answer the user's question below using only the CASE DATA. "
        'If the CASE DATA does not contain enough information to answer, '
        'say so explicitly rather than guessing.\n'
        'Return ONLY: {"answer": "string"}'
    ),
    'generate_finance_response': (
        'Write a short, professional DRAFT response (2-4 sentences) from '
        'Finance to the auditor, suitable to paste into the Finance '
        'Response field before resubmitting this case for auditor review '
        '(you are drafting a suggestion — nothing is submitted '
        'automatically).\n'
        'Base it on "send_back_cycle" (the auditor\'s original reason/'
        'instruction/required actions) if present, and on '
        '"audit_status_reasons" otherwise. Describe what has been done to '
        'address them ONLY to the extent the CASE DATA actually supports '
        '(e.g. only say a document was uploaded if po_uploaded/'
        'gr_uploaded show that) — never claim an action was taken that '
        'is not reflected in the CASE DATA.\n'
        'Return ONLY: {"response": "string"}'
    ),
    'recommended_steps': (
        'List the concrete steps Finance should take to resolve this '
        'case, in order, based only on "send_back_cycle" (its '
        'required_actions/auditor_instruction, if present) and '
        '"audit_status_reasons". If audit_status is "PASS", the only '
        'step is that no further action is needed.\n'
        'Return ONLY: {"steps": ["string", ...]}'
    ),
}

_SEND_BACK_INSTRUCTION = (
    'Prepare a Send-Back-to-Finance instruction for this case, to pre-fill '
    'an existing form (the auditor can still edit every field before '
    'sending — you are drafting a suggestion, not sending anything).\n'
    f'reason_category MUST be exactly one of: {list(REASON_CATEGORIES)}\n'
    f'required_actions MUST be a non-empty list using only values from: {list(REQUIRED_ACTIONS)}\n'
    f'priority MUST be exactly one of: {list(PRIORITIES)}\n'
    'instruction: one short professional sentence telling Finance what to do.\n'
    'Base every field only on the CASE DATA (e.g. only use '
    '"missing_document"/"upload_missing_document" if a document is '
    'actually missing).\n'
    'Return ONLY: {"reason_category": "string", "required_actions": '
    '["string", ...], "priority": "string", "instruction": "string"}'
)


def _call_provider(system_prompt, user_prompt, action_label):
    """Claude first, Gemini fallback — returns (parsed_dict, provider)
    or (None, None) if both fail or return unparseable JSON."""
    claude_text = ask_claude_text(system_prompt, user_prompt)
    if claude_text:
        try:
            return json.loads(_strip_markdown_fences(claude_text)), 'claude'
        except (json.JSONDecodeError, ValueError) as e:
            print(f"DEBUG AI ASSISTANT ({action_label}): Claude JSON parse error: {e}")

    gemini_text = call_gemini_sdk(system_prompt + '\n\n' + user_prompt, context=f'ai_assistant:{action_label}')
    if gemini_text:
        try:
            return json.loads(_strip_markdown_fences(gemini_text)), 'gemini'
        except (json.JSONDecodeError, ValueError) as e:
            print(f"DEBUG AI ASSISTANT ({action_label}): Gemini JSON parse error: {e}")

    return None, None


def ask_ai_assistant(action, context, question=None):
    """Runs one AI Audit Assistant action against `context` (the
    structured case dict from routes/ai_assistant.py::_build_case_
    context). Returns (parsed_response_dict, provider_str), or
    (None, None) if both Claude and Gemini fail — the caller turns that
    into a 502.

    action: one of 'explain_exception' | 'explain_risk' |
      'generate_remark' | 'ask' | 'prepare_send_back' (auditor-facing,
      routes/ai_assistant.py's /explain-exception etc.) or
      'generate_finance_response' | 'recommended_steps' (Finance-
      facing, routes/ai_assistant.py's /finance/* endpoints — 'ask'
      and 'explain_exception' are reused as-is by both sides).
    question: required (and only used) when action == 'ask'.
    """
    context_json = json.dumps(context, indent=2, default=str)
    system_prompt = _SYSTEM_PREAMBLE.format(context_json=context_json)

    if action == 'prepare_send_back':
        user_prompt = _SEND_BACK_INSTRUCTION
    elif action == 'ask':
        user_prompt = _ACTION_INSTRUCTIONS['ask'] + f'\n\nUSER QUESTION: {question}'
    else:
        user_prompt = _ACTION_INSTRUCTIONS[action]

    return _call_provider(system_prompt, user_prompt, action)
