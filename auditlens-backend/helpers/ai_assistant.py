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
  If "audit_status_reasons" mentions a non-blocking anomaly (it does
  whenever one exists — see below), you MUST still name it: a "PASS"
  verdict means core checks passed, NOT that nothing is on record.
- Each entry in "anomalies" already has a "classification":
    - "blocking": requires action before approval — an unresolved
      high-risk, duplicate, or amount-inconsistency finding.
    - "non_blocking": a real, currently-recorded finding that does NOT
      by itself block approval — it may have status "pending" (not yet
      reviewed) or "reviewed" (an auditor has already examined it and
      the finding still stands on the record). EITHER WAY it is an
      EXISTING finding, never a cleared one. State its own status
      ("pending" or "reviewed") explicitly whenever you mention it.
    - "dismissed": the auditor explicitly ruled this finding a false
      positive. This is the ONLY classification you may describe as
      dismissed, cleared, ruled out, or no longer relevant.
  Mention "non_blocking" anomalies briefly as background context, not
  as a reason the audit failed — but NEVER omit, hide, or reword them
  as if they don't exist. A "reviewed" anomaly is not a "dismissed" one
  and must never be described using dismissed/cleared language.
- Banned phrasing whenever at least one "blocking" or "non_blocking"
  anomaly is present (i.e. anything not "dismissed"): "no anomalies",
  "no risks", "not considered a concern", "no further action
  required", or any equivalent implying nothing is on record. These
  phrases are only accurate when EVERY anomaly for this case is
  "dismissed" or the anomalies list is empty.
- Only "blocking" anomalies and the items listed in
  "audit_status_reasons" may be described as requiring attention before
  approval. Do not invent or imply any other exception — but a "non_
  blocking" anomaly still gets named as an existing, non-blocking
  finding, never invented as a blocker and never erased from the
  narrative either.
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
        'send-back status, and any BLOCKING anomaly. Mention non_blocking '
        'anomalies only briefly as context (state whether each is '
        '"pending" or "reviewed" — a reviewed finding still exists, it is '
        'not cleared), never as a reason for "REVIEW REQUIRED", and never '
        'omit one that is present. If audit_status is "PASS", describe the '
        'document as validated / having passed core checks — but if a '
        'non_blocking anomaly exists, say so explicitly rather than '
        'implying nothing is on record.\n'
        'recommended_action: one short sentence — what the auditor should '
        'do next (e.g. "No action required, ready for approval" ONLY when '
        'audit_status is "PASS" AND there is no non_blocking anomaly on '
        'record; otherwise something like "Ready for final Auditor '
        'decision — review the recorded anomaly before finalizing").\n'
        'Return ONLY: {"audit_status": "PASS" or "REVIEW REQUIRED", '
        '"reason": "string", "recommended_action": "string"}'
    ),
    'explain_risk': (
        'Explain the audit risk of this case, covering the FULL audit '
        'context already computed for it — three-way matching_details, '
        'missing_documents, the authenticity result, and every anomaly '
        'finding — not a generic one-line summary. Base the risk LEVEL '
        'and the reasons that justify it only on audit_status/'
        'audit_status_reasons and any "blocking" anomaly (name its '
        'anomaly_type and severity explicitly, e.g. "a high-severity '
        'duplicate-invoice finding") — a "non_blocking" anomaly alone '
        'must NOT raise the risk level, but should still be named briefly '
        'in potential_impact as low-risk background (state whether it is '
        '"pending" or "reviewed" — reviewed does not mean cleared) rather '
        'than omitted. '
        'If an authenticity check has status "warning", name it as a '
        'reason. If audit_status is "PASS", the risk level should '
        'normally be "Low".\n'
        'Return ONLY: {"risk_level": "Low" or "Medium" or "High", '
        '"reasons": ["string", ...], "potential_impact": "string"}'
    ),
    'approval_assessment': (
        'Assess this case\'s readiness for auditor approval. Base it on '
        'the FULL case data already computed for it — the three-way '
        'matching result, missing documents, the authenticity result, '
        'every anomaly/risk finding, and the financial figures — but '
        'write like an auditor talking to another auditor: short, plain, '
        'professional language. NEVER write raw field/technical names '
        '(e.g. "matching_details", "anomaly_type", "severity", '
        '"audit_status_reasons", "po_amount", "amount_match") anywhere '
        'in your output — translate them into plain terms instead. For '
        'example: a "duplicate" anomaly becomes "possible duplicate '
        'invoice"; an "amount" or "round" anomaly becomes "unusual '
        'invoice amount"; a "weekend" anomaly becomes "invoice dated on '
        'a weekend"; state severity as "high/medium/low risk", never the '
        'word "severity" itself; describe amounts in plain sentences '
        '(e.g. "Invoice amount does not match the Purchase Order '
        'amount") rather than field=value pairs.\n'
        'Each of blocking_issues / passed_checks / risk_context / '
        'recommended_next_steps MUST have AT MOST 4 items, each a short '
        'phrase or one short sentence — never a paragraph. Pick only '
        'the 3-4 MOST important points, not every possible one.\n'
        'blocking_issues: unresolved problems that actually prevent '
        'approval right now (a missing document, a mismatch, an '
        'authenticity concern, an unresolved BLOCKING risk finding) — '
        'empty only when this case is otherwise clean. A "non_blocking" '
        'anomaly (whether "pending" or already "reviewed") is NEVER a '
        'blocking issue — put it in risk_context instead, never here. Only '
        'a "dismissed" anomaly may be treated as fully resolved.\n'
        'passed_checks: the checks that ARE already satisfied, e.g. '
        '"Vendor name matches", "Amount matches the Purchase Order", '
        '"All required documents uploaded", "Authenticity check '
        'passed". Only list checks that genuinely passed — never repeat '
        'a blocking issue or a risk_context item here.\n'
        'risk_context: every non-blocking ("non_blocking") finding worth '
        'the auditor knowing about even though it does NOT prevent '
        'approval — e.g. a '
        'weekend-dated invoice, a low-severity unusual-amount pattern, or '
        'an unusually round amount. State each one\'s own status '
        '("pending" or "reviewed") explicitly — a reviewed finding is '
        'STILL a real finding on the record, never describe it as '
        'resolved, cleared, or no longer relevant (only a "dismissed" '
        'finding may be described that way, and dismissed findings do '
        'not belong in risk_context at all). Empty when there are none — '
        'do not invent one. NEVER put a blocking issue here, and do NOT '
        'suggest resolving a risk_context item in recommended_next_steps '
        'unless that same item is ALSO listed in blocking_issues.\n'
        'recommended_next_steps: the concrete next step(s) the auditor '
        'should take before deciding — e.g. request a specific missing '
        'document, verify an amount with Finance/the vendor, review a '
        'flagged BLOCKING risk finding. Never recommend action on a '
        'non-blocking risk_context item. If nothing is blocking AND there '
        'is no non_blocking anomaly on record either, the only step is '
        'that no further action is needed. If nothing is blocking BUT a '
        'non_blocking anomaly IS on record, say the case is ready for the '
        'Auditor\'s final decision while still naming that anomaly — never '
        'say "no further action required" while a non_blocking anomaly '
        'exists.\n'
        'You are assessing readiness, not deciding — approval_readiness '
        'in your response is informational only and will be verified '
        'against the case\'s own deterministic status before being shown '
        'to the auditor; the human auditor always makes the actual '
        'approval / send back / need-review decision, never this '
        'assessment.\n'
        'Return ONLY: {"approval_readiness": "Ready" or "Not Ready" or '
        '"Requires Review", "blocking_issues": ["string", ...], '
        '"passed_checks": ["string", ...], "risk_context": ["string", '
        '...], "recommended_next_steps": ["string", ...]}'
    ),
    'generate_remark': (
        "Write a short, professional auditor remark (2-4 sentences) "
        "suitable to paste directly into this case's Remarks/Notes field. "
        'If audit_status is "PASS", state that the document passed core '
        'checks / is validated (a non_blocking anomaly, if any, may be '
        'mentioned briefly but not as a blocker — state whether it is '
        '"pending" or "reviewed"; a reviewed finding is still on record, '
        'never call it dismissed or cleared unless its status is actually '
        '"dismissed"). If audit_status is '
        '"REVIEW REQUIRED", name the SPECIFIC item(s) actually driving '
        'that status from audit_status_reasons instead of a generic '
        '"needs review" statement — e.g. the mismatched field from '
        'matching_details, the specific missing_documents entry, the '
        'authenticity concern, or the blocking anomaly (its anomaly_type '
        'and severity) — and note what is being requested from Finance '
        'before approval if applicable.\n'
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
    'Base every field only on the CASE DATA — pick reason_category/'
    'required_actions to match whichever item in audit_status_reasons is '
    'ACTUALLY driving this case, using the FULL context (matching_details, '
    'missing_documents, the authenticity result, and every anomaly '
    'finding), not only missing documents:\n'
    '  - a missing_documents entry -> "missing_document" / '
    '"upload_missing_document"\n'
    '  - a matching_details mismatch (vendor/amount/PO reference/line '
    'items) -> "invoice_po_gr_mismatch" / "correct_extracted_information" '
    '(or "verify_amount_or_quantity" specifically for an amount mismatch)\n'
    '  - a "blocking" anomaly with anomaly_type "duplicate" -> '
    '"possible_duplicate_invoice" / "confirm_duplicate_submission"\n'
    '  - a "blocking" anomaly with anomaly_type "amount" or "round" -> '
    '"amount_or_quantity_requires_verification" / '
    '"verify_amount_or_quantity"\n'
    '  - an authenticity result with status "warning" -> '
    '"authenticity_evidence_requires_clarification" / '
    '"provide_written_explanation"\n'
    'If more than one applies, choose the one matching the FIRST/primary '
    'entry in audit_status_reasons for reason_category, and add the other '
    'relevant required_actions alongside it (required_actions may list '
    'more than one). Mention every item you are drafting this for in the '
    'instruction sentence, not just the first. priority should be "high" '
    'when a blocking anomaly is high-severity or an authenticity warning '
    'is present, "medium" for another blocking finding, "normal" '
    'otherwise.\n'
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
      'approval_assessment' | 'generate_remark' | 'ask' |
      'prepare_send_back' (auditor-facing, routes/ai_assistant.py's
      /explain-exception etc.) or 'generate_finance_response' |
      'recommended_steps' (Finance-facing, routes/ai_assistant.py's
      /finance/* endpoints — 'ask' and 'explain_exception' are reused
      as-is by both sides).
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
