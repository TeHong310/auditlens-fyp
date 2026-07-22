"""AI Audit Assistant — contextual, on-demand AI helper for auditors
reviewing ONE invoice case (routes/ai_assistant.py builds the case
context and calls into this module). This is explicitly NOT a general
chatbot: every prompt is scoped to the CASE DATA the caller passes in
(already computed by AuditLens' own three-way matching / authenticity /
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
Payable audit system. You help a human auditor understand ONE specific
invoice audit case that has ALREADY been processed by AuditLens' own
matching, authenticity, and anomaly-detection engines.

STRICT RULES:
- Use ONLY the CASE DATA given below. Never invent an invoice number,
  vendor name, amount, document, or finding that is not present in it.
- If something is missing/null in the CASE DATA, say it is missing or
  not available — never guess or assume a plausible value.
- You are an assistant, not a decision maker. Never declare that the
  document IS approved, rejected, fraudulent, or genuine — only explain
  the evidence already computed by the system and let the human auditor
  decide.
- Be concise, factual, and professional — enterprise audit
  documentation tone, not a casual chatbot.
- Return ONLY valid JSON, no markdown, no code fences, no explanation
  outside the JSON.

CASE DATA (JSON):
{context_json}
"""

_ACTION_INSTRUCTIONS = {
    'explain_exception': (
        'Summarize this audit case in 3-5 sentences: what the invoice is, '
        'the vendor and amount, which supporting documents are missing (if '
        'any), the three-way matching result, the authenticity result (if '
        'available), the anomaly result (if available), and the impact on '
        'approval.\nReturn ONLY: {"answer": "string"}'
    ),
    'explain_risk': (
        'Explain the audit risk of this case. Base the risk level and '
        'reasons only on the CASE DATA (missing documents, matching '
        'mismatches, authenticity warnings, anomalies). If nothing is '
        'unusual, the risk is "Low".\n'
        'Return ONLY: {"risk_level": "Low" or "Medium" or "High", '
        '"reasons": ["string", ...], "potential_impact": "string"}'
    ),
    'generate_remark': (
        "Write a short, professional auditor remark (2-4 sentences) "
        "suitable to paste directly into this case's Remarks/Notes field, "
        'explaining the current review status and, if applicable, what is '
        'being requested from Finance before approval.\n'
        'Return ONLY: {"remark": "string"}'
    ),
    'ask': (
        "Answer the auditor's question below using only the CASE DATA. "
        'If the CASE DATA does not contain enough information to answer, '
        'say so explicitly rather than guessing.\n'
        'Return ONLY: {"answer": "string"}'
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
      'generate_remark' | 'ask' | 'prepare_send_back'.
    question: required (and only used) when action == 'ask'.
    """
    context_json = json.dumps(context, indent=2, default=str)
    system_prompt = _SYSTEM_PREAMBLE.format(context_json=context_json)

    if action == 'prepare_send_back':
        user_prompt = _SEND_BACK_INSTRUCTION
    elif action == 'ask':
        user_prompt = _ACTION_INSTRUCTIONS['ask'] + f'\n\nAUDITOR QUESTION: {question}'
    else:
        user_prompt = _ACTION_INSTRUCTIONS[action]

    return _call_provider(system_prompt, user_prompt, action)
