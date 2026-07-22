"""Regression tests for the AI Audit Assistant (helpers/ai_assistant.py,
routes/ai_assistant.py) — the auditor-triggered "Explain Exception" /
"Explain Risk" / "Generate Audit Remark" / "Prepare Send Back
Instruction" / "Ask" feature on the Record Detail page.

No real DB, no real Claude/Gemini calls — every external call
(ask_claude_text, call_gemini_sdk, get_db_connection, _build_comparison,
_classify_exception) is monkey-patched with fakes/stubs, same style as
tests/extraction/test_authenticity_siblings.py.

Usage:
    python tests/extraction/test_ai_assistant.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask

import helpers.ai_assistant as haa
import routes.ai_assistant as ra

FAILURES = []

# _require_finance_owner()/_require_auditor() call jsonify() on the
# rejection path, which needs a Flask application context — a minimal
# throwaway app is enough (no blueprint registration, no real DB).
_test_app = Flask(__name__)


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class _Patched:
    """Context manager that monkey-patches a module's attributes for the
    duration of the `with` block and restores them afterward — same
    pattern used by test_authenticity_siblings.py's _Patched class."""

    def __init__(self, module, **overrides):
        self.module = module
        self.overrides = overrides
        self._originals = {}

    def __enter__(self):
        for name, value in self.overrides.items():
            self._originals[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(self.module, name, value)


# ============================================================
# helpers/ai_assistant.py — ask_ai_assistant() provider fallback
# ============================================================

def run_case_claude_success_never_calls_gemini():
    print('Case: Claude returns valid JSON -> used directly, Gemini never called')
    gemini_called = []
    with _Patched(haa,
                  ask_claude_text=lambda sp, up, **k: '{"answer": "Claude answer"}',
                  call_gemini_sdk=lambda *a, **k: gemini_called.append(1) or '{"answer": "Gemini answer"}'):
        result, provider = haa.ask_ai_assistant('explain_exception', {'invoice_number': 'INV-1'})
    check('provider is claude', provider == 'claude', provider)
    check('answer came from Claude', result == {'answer': 'Claude answer'}, result)
    check('Gemini was never called', gemini_called == [], gemini_called)


def run_case_claude_none_falls_back_to_gemini():
    print('Case: Claude unavailable (returns None) -> falls back to Gemini')
    with _Patched(haa,
                  ask_claude_text=lambda sp, up, **k: None,
                  call_gemini_sdk=lambda *a, **k: '{"answer": "Gemini answer"}'):
        result, provider = haa.ask_ai_assistant('explain_exception', {'invoice_number': 'INV-1'})
    check('provider is gemini', provider == 'gemini', provider)
    check('answer came from Gemini', result == {'answer': 'Gemini answer'}, result)


def run_case_claude_bad_json_falls_back_to_gemini():
    print('Case: Claude returns unparseable JSON -> falls back to Gemini')
    with _Patched(haa,
                  ask_claude_text=lambda sp, up, **k: 'not json at all',
                  call_gemini_sdk=lambda *a, **k: '{"answer": "Gemini answer"}'):
        result, provider = haa.ask_ai_assistant('explain_exception', {'invoice_number': 'INV-1'})
    check('provider is gemini after Claude JSON failure', provider == 'gemini', provider)
    check('answer came from Gemini', result == {'answer': 'Gemini answer'}, result)


def run_case_both_providers_fail_returns_none():
    print('Case: both Claude and Gemini fail -> (None, None)')
    with _Patched(haa,
                  ask_claude_text=lambda sp, up, **k: None,
                  call_gemini_sdk=lambda *a, **k: None):
        result, provider = haa.ask_ai_assistant('explain_exception', {'invoice_number': 'INV-1'})
    check('result is None', result is None, result)
    check('provider is None', provider is None, provider)


def run_case_markdown_fences_are_stripped():
    print('Case: Claude wraps its JSON in ```json fences -> still parses')
    with _Patched(haa,
                  ask_claude_text=lambda sp, up, **k: '```json\n{"remark": "ok"}\n```',
                  call_gemini_sdk=lambda *a, **k: None):
        result, provider = haa.ask_ai_assistant('generate_remark', {'invoice_number': 'INV-1'})
    check('fenced JSON parsed correctly', result == {'remark': 'ok'}, result)
    check('provider is claude', provider == 'claude', provider)


def run_case_ask_action_includes_question_in_prompt():
    print("Case: 'ask' action includes the auditor's question in the prompt sent to Claude")
    captured = {}

    def fake_ask_claude_text(system_prompt, user_prompt, **k):
        captured['user_prompt'] = user_prompt
        return '{"answer": "Yes"}'
    with _Patched(haa, ask_claude_text=fake_ask_claude_text, call_gemini_sdk=lambda *a, **k: None):
        haa.ask_ai_assistant('ask', {'invoice_number': 'INV-1'}, question='Can this invoice be approved?')
    check('question text reached the prompt',
          'Can this invoice be approved?' in captured.get('user_prompt', ''), captured)


def run_case_send_back_prompt_lists_valid_enums():
    print('Case: prepare_send_back prompt references the canonical enum lists')
    captured = {}

    def fake_ask_claude_text(system_prompt, user_prompt, **k):
        captured['user_prompt'] = user_prompt
        return '{"reason_category": "missing_document", "required_actions": ["upload_missing_document"], "priority": "normal", "instruction": "Please upload."}'
    with _Patched(haa, ask_claude_text=fake_ask_claude_text, call_gemini_sdk=lambda *a, **k: None):
        result, provider = haa.ask_ai_assistant('prepare_send_back', {'invoice_number': 'INV-1'})
    check('prompt mentions missing_document category', 'missing_document' in captured.get('user_prompt', ''))
    check('parsed result has expected keys',
          set(result.keys()) == {'reason_category', 'required_actions', 'priority', 'instruction'}, result)


# ============================================================
# routes/ai_assistant.py — _clamp_send_back_result()
# ============================================================

def run_case_clamp_passes_through_valid_values():
    print('Case: clamp leaves a fully valid AI result unchanged')
    ai_result = {'reason_category': 'invoice_po_gr_mismatch', 'required_actions': ['verify_amount_or_quantity'],
                 'priority': 'high', 'instruction': 'Please verify the amount.'}
    clamped = ra._clamp_send_back_result(ai_result, exception_info=None)
    check('reason_category unchanged', clamped['reason_category'] == 'invoice_po_gr_mismatch', clamped)
    check('required_actions unchanged', clamped['required_actions'] == ['verify_amount_or_quantity'], clamped)
    check('priority unchanged', clamped['priority'] == 'high', clamped)
    check('instruction unchanged', clamped['instruction'] == 'Please verify the amount.', clamped)


def run_case_clamp_invalid_reason_category_falls_back():
    print('Case: an invalid/hallucinated reason_category is clamped to a safe default')
    ai_result = {'reason_category': 'the_ai_made_this_up', 'required_actions': ['upload_missing_document'],
                 'priority': 'normal', 'instruction': 'x'}
    clamped_missing_doc = ra._clamp_send_back_result(
        ai_result, exception_info={'type': 'missing_document', 'severity': 'medium'})
    check('falls back to missing_document when the case IS a missing-document exception',
          clamped_missing_doc['reason_category'] == 'missing_document', clamped_missing_doc)

    clamped_other = ra._clamp_send_back_result(ai_result, exception_info={'type': 'mismatch', 'severity': 'high'})
    check('falls back to other for a non-missing-document exception',
          clamped_other['reason_category'] == 'other', clamped_other)


def run_case_clamp_invalid_required_actions_falls_back():
    print('Case: required_actions containing only invalid/unknown values falls back')
    ai_result = {'reason_category': 'missing_document', 'required_actions': ['delete_everything', 'not_a_real_action'],
                 'priority': 'normal', 'instruction': 'x'}
    clamped = ra._clamp_send_back_result(ai_result, exception_info=None)
    check('falls back to upload_missing_document for a missing_document reason',
          clamped['required_actions'] == ['upload_missing_document'], clamped)


def run_case_clamp_invalid_priority_falls_back():
    print('Case: an invalid priority falls back based on exception severity')
    ai_result = {'reason_category': 'missing_document', 'required_actions': ['upload_missing_document'],
                 'priority': 'urgent!!!', 'instruction': 'x'}
    clamped_medium = ra._clamp_send_back_result(ai_result, exception_info={'type': 'missing_document', 'severity': 'medium'})
    check('medium-severity exception -> medium priority', clamped_medium['priority'] == 'medium', clamped_medium)

    clamped_default = ra._clamp_send_back_result(ai_result, exception_info=None)
    check('no exception info -> normal priority', clamped_default['priority'] == 'normal', clamped_default)


def run_case_clamp_empty_instruction_gets_default_text():
    print('Case: a blank instruction is replaced with a safe default sentence')
    ai_result = {'reason_category': 'missing_document', 'required_actions': ['upload_missing_document'],
                 'priority': 'normal', 'instruction': '   '}
    clamped = ra._clamp_send_back_result(ai_result, exception_info=None)
    check('instruction is non-empty', bool(clamped['instruction'].strip()), clamped)


def run_case_clamp_handles_none_result():
    print('Case: clamp never crashes even if the AI returned nothing usable at all')
    clamped = ra._clamp_send_back_result(None, exception_info=None)
    check('reason_category defaults to other', clamped['reason_category'] == 'other', clamped)
    check('required_actions defaults to [other]', clamped['required_actions'] == ['other'], clamped)
    check('priority defaults to normal', clamped['priority'] == 'normal', clamped)


# ============================================================
# routes/ai_assistant.py — _cache_key() determinism
# ============================================================

def run_case_cache_key_deterministic():
    print('Case: same context + question always hashes to the same key')
    context = {'invoice_number': 'INV-1', 'amount': 100.0}
    k1 = ra._cache_key(context, None)
    k2 = ra._cache_key(context, None)
    check('identical inputs produce identical hash', k1 == k2, (k1, k2))


def run_case_cache_key_changes_with_question():
    print('Case: a different question produces a different cache key')
    context = {'invoice_number': 'INV-1'}
    k1 = ra._cache_key(context, 'Why does this fail?')
    k2 = ra._cache_key(context, 'Can this be approved?')
    check('different questions hash differently', k1 != k2, (k1, k2))


def run_case_cache_key_changes_with_context():
    print('Case: a changed case (e.g. matching_status flipped) produces a different cache key')
    k1 = ra._cache_key({'matching_status': 'PARTIAL'}, None)
    k2 = ra._cache_key({'matching_status': 'PASS'}, None)
    check('different case data hashes differently', k1 != k2, (k1, k2))


# ============================================================
# routes/ai_assistant.py — _build_case_context() field mapping
# ============================================================

class _FakeCursor:
    """Returns canned rows in the FIXED order _build_case_context()
    issues its queries after _build_comparison (mocked below, so it
    never touches the cursor itself): documents row, authenticity_checks
    rows, anomalies rows, review_records rows, latest send_back_cycles
    row (cycle_row=None by default — no structured return on record)."""

    def __init__(self, doc_row, authenticity_rows, anomaly_rows, history_rows, cycle_row=None):
        self._queue = [('one', doc_row), ('many', authenticity_rows),
                        ('many', anomaly_rows), ('many', history_rows), ('one', cycle_row)]
        self._current = None

    def execute(self, sql, params=None):
        self._current = self._queue.pop(0)

    def fetchone(self):
        kind, value = self._current
        return value

    def fetchall(self):
        kind, value = self._current
        return value


def run_case_build_case_context_missing_documents_and_exception():
    print('Case: _build_case_context reports missing PO/GR and the classified exception')
    fake_comparison = {
        'invoice': {'invoice_no': 'INV-1', 'vendor_name': 'Coilcraft', 'total_amount': 500.0,
                    'currency': 'RM', 'invoice_date': '2026-07-01',
                    'uploaded_at': '2026-07-01T09:00:00', 'ocr_confidence': 95.0},
        'po': None, 'gr': None,
        'match_result': {'overall_status': 'PARTIAL', 'vendor_match': None, 'amount_match': None,
                          'po_reference_match': None, 'line_items_match': None, 'line_items_price_match': None},
    }
    doc_row = {'document_id': 1, 'uploaded_at': None, 'status': 'under_review'}
    classified = (3, 'missing_document', 'Missing PO and GR', 'Invoice uploaded but PO and GR not yet received', 'medium')

    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[], anomaly_rows=[], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: fake_comparison,
                  _classify_exception=lambda c, d, cmp: classified):
        context = ra._build_case_context(cursor, 1)

    check('invoice_number mapped', context['invoice_number'] == 'INV-1', context)
    check('vendor mapped', context['vendor'] == 'Coilcraft', context)
    check('missing_documents lists both PO and GR', context['missing_documents'] == ['Purchase Order', 'Goods Receipt'], context)
    check('matching_status mapped', context['matching_status'] == 'PARTIAL', context)
    check('exception type mapped', context['exception']['type'] == 'missing_document', context)
    check('document_status mapped', context['document_status'] == 'under_review', context)


def run_case_build_case_context_returns_none_when_no_comparison():
    print('Case: _build_case_context returns None when the invoice document does not exist')
    cursor = _FakeCursor(doc_row=None, authenticity_rows=[], anomaly_rows=[], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: None):
        context = ra._build_case_context(cursor, 999)
    check('returns None for a nonexistent document', context is None, context)


def run_case_build_case_context_clean_pass_has_no_exception():
    print('Case: a clean PASS record has no missing_documents and no exception')
    fake_comparison = {
        'invoice': {'invoice_no': 'INV-2', 'vendor_name': 'Vendor B', 'total_amount': 100.0,
                    'currency': 'RM', 'invoice_date': '2026-07-01',
                    'uploaded_at': '2026-07-01T09:00:00', 'ocr_confidence': 95.0},
        'po': {'po_no': 'PO-1'}, 'gr': {'gr_no': 'GR-1'},
        'match_result': {'overall_status': 'PASS', 'vendor_match': True, 'amount_match': True,
                          'po_reference_match': True, 'line_items_match': True, 'line_items_price_match': True},
    }
    doc_row = {'document_id': 2, 'uploaded_at': None, 'status': 'under_review'}
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[], anomaly_rows=[], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: fake_comparison,
                  _classify_exception=lambda c, d, cmp: None):
        context = ra._build_case_context(cursor, 2)
    check('no missing documents', context['missing_documents'] == [], context)
    check('no exception', context['exception'] is None, context)


def run_case_build_case_context_includes_send_back_cycle_when_present():
    print('Case: _build_case_context includes the auditor\'s structured send-back request when one exists')
    fake_comparison = {
        'invoice': {'invoice_no': 'INV-3', 'vendor_name': 'Vendor C', 'total_amount': 200.0,
                    'currency': 'RM', 'invoice_date': '2026-07-01',
                    'uploaded_at': '2026-07-01T09:00:00', 'ocr_confidence': 95.0},
        'po': None, 'gr': None,
        'match_result': {'overall_status': 'PARTIAL', 'vendor_match': None, 'amount_match': None,
                          'po_reference_match': None, 'line_items_match': None, 'line_items_price_match': None},
    }
    doc_row = {'document_id': 3, 'uploaded_at': None, 'status': 'returned'}
    cycle_row = {
        'cycle_number': 2, 'return_reason_category': 'missing_document', 'reason_other_note': None,
        'auditor_instruction': 'Please upload the Purchase Order and Goods Receipt.',
        'required_actions': ['upload_missing_document'], 'priority': 'medium',
        'response_due_date': None, 'cycle_status': 'action_required', 'sent_back_at': None,
    }
    classified = (3, 'missing_document', 'Missing PO and GR', 'x', 'medium')
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[], anomaly_rows=[], history_rows=[], cycle_row=cycle_row)
    with _Patched(ra, _build_comparison=lambda c, d: fake_comparison, _classify_exception=lambda c, d, cmp: classified):
        context = ra._build_case_context(cursor, 3)

    check('send_back_cycle populated', context['send_back_cycle'] is not None, context)
    check('auditor_instruction passed through',
          context['send_back_cycle']['auditor_instruction'] == 'Please upload the Purchase Order and Goods Receipt.', context)
    check('required_actions passed through',
          context['send_back_cycle']['required_actions'] == ['upload_missing_document'], context)
    check('priority passed through', context['send_back_cycle']['priority'] == 'medium', context)


def run_case_build_case_context_send_back_cycle_none_when_never_returned():
    print('Case: send_back_cycle is None for a document that was never sent back via the structured form')
    fake_comparison = {
        'invoice': {'invoice_no': 'INV-4', 'vendor_name': 'Vendor D', 'total_amount': 300.0,
                    'currency': 'RM', 'invoice_date': '2026-07-01',
                    'uploaded_at': '2026-07-01T09:00:00', 'ocr_confidence': 95.0},
        'po': {'po_no': 'PO-1'}, 'gr': {'gr_no': 'GR-1'},
        'match_result': {'overall_status': 'PASS', 'vendor_match': True, 'amount_match': True,
                          'po_reference_match': True, 'line_items_match': True, 'line_items_price_match': True},
    }
    doc_row = {'document_id': 4, 'uploaded_at': None, 'status': 'under_review'}
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[], anomaly_rows=[], history_rows=[])  # cycle_row defaults to None
    with _Patched(ra, _build_comparison=lambda c, d: fake_comparison, _classify_exception=lambda c, d, cmp: None):
        context = ra._build_case_context(cursor, 4)
    check('send_back_cycle is None', context['send_back_cycle'] is None, context)


# ============================================================
# helpers/ai_assistant.py — Finance-facing actions (generate_finance_
# response, recommended_steps) round-trip through ask_ai_assistant
# ============================================================

def run_case_generate_finance_response_action_round_trips():
    print("Case: 'generate_finance_response' action round-trips through Claude")
    with _Patched(haa,
                  ask_claude_text=lambda sp, up, **k: '{"response": "Purchase Order and Goods Receipt have been uploaded."}',
                  call_gemini_sdk=lambda *a, **k: None):
        result, provider = haa.ask_ai_assistant('generate_finance_response', {'invoice_number': 'INV-1'})
    check('response returned', result == {'response': 'Purchase Order and Goods Receipt have been uploaded.'}, result)
    check('provider is claude', provider == 'claude', provider)


def run_case_recommended_steps_action_round_trips():
    print("Case: 'recommended_steps' action round-trips through Claude")
    with _Patched(haa,
                  ask_claude_text=lambda sp, up, **k: '{"steps": ["Upload the missing PO", "Upload the missing GR", "Resubmit to auditor"]}',
                  call_gemini_sdk=lambda *a, **k: None):
        result, provider = haa.ask_ai_assistant('recommended_steps', {'invoice_number': 'INV-1'})
    check('steps returned', result == {'steps': ['Upload the missing PO', 'Upload the missing GR', 'Resubmit to auditor']}, result)


def run_case_generate_finance_response_prompt_references_send_back_cycle():
    print("Case: 'generate_finance_response' prompt tells the AI to ground itself in send_back_cycle")
    captured = {}

    def fake_ask_claude_text(system_prompt, user_prompt, **k):
        captured['user_prompt'] = user_prompt
        return '{"response": "ok"}'
    with _Patched(haa, ask_claude_text=fake_ask_claude_text, call_gemini_sdk=lambda *a, **k: None):
        haa.ask_ai_assistant('generate_finance_response', {'invoice_number': 'INV-1'})
    check('prompt references send_back_cycle', 'send_back_cycle' in captured.get('user_prompt', ''), captured)


# ============================================================
# routes/ai_assistant.py — _require_finance_owner()
# ============================================================

class _OwnerCursor:
    def __init__(self, uploaded_by, doc_exists=True):
        self.uploaded_by = uploaded_by
        self.doc_exists = doc_exists

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (self.uploaded_by,) if self.doc_exists else None


class _OwnerConn:
    def __init__(self, uploaded_by, doc_exists=True):
        self._cursor = _OwnerCursor(uploaded_by, doc_exists)

    def cursor(self, **kwargs):
        return self._cursor

    def close(self):
        pass


def run_case_require_finance_owner_accepts_the_actual_uploader():
    print('Case: a finance_executive who uploaded this document is accepted')
    with _test_app.app_context(), _Patched(
                  ra,
                  get_jwt_identity=lambda: 5,
                  get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
                  get_db_connection=lambda: _OwnerConn(uploaded_by=5)):
        user, err = ra._require_finance_owner(1)
    check('accepted (no error)', err is None, err)
    check('user returned', user is not None, user)


def run_case_require_finance_owner_rejects_a_different_finance_user():
    print("Case: a finance_executive who did NOT upload this document is rejected (403)")
    with _test_app.app_context(), _Patched(
                  ra,
                  get_jwt_identity=lambda: 5,
                  get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
                  get_db_connection=lambda: _OwnerConn(uploaded_by=99)):
        user, err = ra._require_finance_owner(1)
    check('rejected with 403', err is not None and err[1] == 403, err)


def run_case_require_finance_owner_rejects_non_finance_role():
    print("Case: a non-finance role (e.g. auditor) is rejected (403), no DB lookup needed")
    with _test_app.app_context(), _Patched(
                  ra,
                  get_jwt_identity=lambda: 5,
                  get_user_by_id=lambda uid: {'user_id': 5, 'role': 'auditor'}):
        user, err = ra._require_finance_owner(1)
    check('rejected with 403', err is not None and err[1] == 403, err)


def run_case_require_finance_owner_404_for_missing_document():
    print('Case: a nonexistent document returns 404')
    with _test_app.app_context(), _Patched(
                  ra,
                  get_jwt_identity=lambda: 5,
                  get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
                  get_db_connection=lambda: _OwnerConn(uploaded_by=None, doc_exists=False)):
        user, err = ra._require_finance_owner(999)
    check('rejected with 404', err is not None and err[1] == 404, err)


# ============================================================
# routes/ai_assistant.py — _classify_anomaly() blocking vs informational
# ============================================================

def run_case_classify_anomaly_reviewed_is_always_informational():
    print('Case: a reviewed/dismissed anomaly is informational regardless of severity or type')
    check('reviewed high-severity duplicate -> informational',
          ra._classify_anomaly({'status': 'reviewed', 'severity': 'high', 'anomaly_type': 'duplicate'}) == 'informational')
    check('dismissed high-severity amount -> informational',
          ra._classify_anomaly({'status': 'dismissed', 'severity': 'high', 'anomaly_type': 'amount'}) == 'informational')


def run_case_classify_anomaly_pending_high_severity_is_blocking():
    print('Case: a pending high-severity anomaly is blocking regardless of type')
    check('pending high-severity round-number anomaly -> blocking',
          ra._classify_anomaly({'status': 'pending', 'severity': 'high', 'anomaly_type': 'round'}) == 'blocking')


def run_case_classify_anomaly_pending_duplicate_or_amount_is_blocking():
    print('Case: a pending duplicate/amount anomaly is blocking even at low/medium severity')
    check('pending low-severity duplicate -> blocking (unresolved duplicate)',
          ra._classify_anomaly({'status': 'pending', 'severity': 'low', 'anomaly_type': 'duplicate'}) == 'blocking')
    check('pending medium-severity amount -> blocking (amount inconsistency)',
          ra._classify_anomaly({'status': 'pending', 'severity': 'medium', 'anomaly_type': 'amount'}) == 'blocking')


def run_case_classify_anomaly_pending_low_pattern_is_informational():
    print('Case: a pending low/medium-severity round/weekend pattern is informational')
    check('pending low-severity round-number pattern -> informational',
          ra._classify_anomaly({'status': 'pending', 'severity': 'low', 'anomaly_type': 'round'}) == 'informational')
    check('pending medium-severity weekend pattern -> informational',
          ra._classify_anomaly({'status': 'pending', 'severity': 'medium', 'anomaly_type': 'weekend'}) == 'informational')


# ============================================================
# routes/ai_assistant.py — _build_case_context() audit_status
# (the 4 scenarios the task explicitly asks to test)
# ============================================================

def _pass_comparison(invoice_no='INV-PASS'):
    return {
        'invoice': {'invoice_no': invoice_no, 'vendor_name': 'Vendor A', 'total_amount': 100.0,
                    'currency': 'RM', 'invoice_date': '2026-07-01',
                    'uploaded_at': '2026-07-01T09:00:00', 'ocr_confidence': 95.0},
        'po': {'po_no': 'PO-1'}, 'gr': {'gr_no': 'GR-1'},
        'match_result': {'overall_status': 'PASS', 'vendor_match': True, 'amount_match': True,
                          'po_reference_match': True, 'line_items_match': True, 'line_items_price_match': True},
    }


def run_case_audit_status_full_pass_document():
    print('Case 1/4: Full PASS document (matching PASS, authenticity PASS, no missing docs, no anomalies) -> PASS')
    doc_row = {'document_id': 1, 'uploaded_at': None, 'status': 'under_review'}
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[{'document_type': 'invoice', 'authenticity_status': 'passed', 'risk_level': 'low'}],
                          anomaly_rows=[], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: _pass_comparison(), _classify_exception=lambda c, d, cmp: None):
        context = ra._build_case_context(cursor, 1)
    check('audit_status is PASS', context['audit_status'] == 'PASS', context)


def run_case_audit_status_full_pass_with_historical_reviewed_duplicate_is_still_pass():
    print('Case 1b/4: PASS document + a REVIEWED historical duplicate anomaly -> still PASS (the original bug report)')
    doc_row = {'document_id': 1, 'uploaded_at': None, 'status': 'under_review'}
    reviewed_duplicate = {'anomaly_type': 'duplicate', 'severity': 'medium', 'detected_pattern': 'similar invoice found',
                           'ai_explanation': 'x', 'status': 'reviewed'}
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[{'document_type': 'invoice', 'authenticity_status': 'passed', 'risk_level': 'low'}],
                          anomaly_rows=[reviewed_duplicate], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: _pass_comparison(), _classify_exception=lambda c, d, cmp: None):
        context = ra._build_case_context(cursor, 1)
    check('audit_status is still PASS despite the historical duplicate finding',
          context['audit_status'] == 'PASS', context)
    check('the anomaly is classified informational, not blocking',
          context['anomalies'][0]['classification'] == 'informational', context['anomalies'])


def run_case_audit_status_missing_po_gr_document():
    print('Case 2/4: Missing PO/GR document -> REVIEW REQUIRED')
    fake_comparison = {
        'invoice': {'invoice_no': 'INV-MISSING', 'vendor_name': 'Vendor A', 'total_amount': 100.0,
                    'currency': 'RM', 'invoice_date': '2026-07-01',
                    'uploaded_at': '2026-07-01T09:00:00', 'ocr_confidence': 95.0},
        'po': None, 'gr': None,
        'match_result': {'overall_status': 'PARTIAL', 'vendor_match': None, 'amount_match': None,
                          'po_reference_match': None, 'line_items_match': None, 'line_items_price_match': None},
    }
    doc_row = {'document_id': 2, 'uploaded_at': None, 'status': 'under_review'}
    classified = (3, 'missing_document', 'Missing PO and GR', 'Invoice uploaded but PO and GR not yet received', 'medium')
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[], anomaly_rows=[], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: fake_comparison, _classify_exception=lambda c, d, cmp: classified):
        context = ra._build_case_context(cursor, 2)
    check('audit_status is REVIEW REQUIRED', context['audit_status'] == 'REVIEW REQUIRED', context)
    check('reasons mention the missing documents',
          any('Missing' in r for r in context['audit_status_reasons']), context['audit_status_reasons'])


def run_case_audit_status_duplicate_invoice_document():
    print('Case 3/4: Duplicate invoice document (pending, unresolved) -> REVIEW REQUIRED')
    doc_row = {'document_id': 3, 'uploaded_at': None, 'status': 'under_review'}
    pending_duplicate = {'anomaly_type': 'duplicate', 'severity': 'medium', 'detected_pattern': 'possible duplicate',
                          'ai_explanation': 'x', 'status': 'pending'}
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[{'document_type': 'invoice', 'authenticity_status': 'passed', 'risk_level': 'low'}],
                          anomaly_rows=[pending_duplicate], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: _pass_comparison(), _classify_exception=lambda c, d, cmp: None):
        context = ra._build_case_context(cursor, 3)
    check('audit_status is REVIEW REQUIRED for an unresolved duplicate',
          context['audit_status'] == 'REVIEW REQUIRED', context)
    check('the anomaly is classified blocking', context['anomalies'][0]['classification'] == 'blocking', context['anomalies'])
    check('reasons mention the unresolved duplicate anomaly',
          any('duplicate' in r for r in context['audit_status_reasons']), context['audit_status_reasons'])


def run_case_audit_status_sent_back_document():
    print('Case 4/4: Sent-back document (status=returned) -> REVIEW REQUIRED')
    doc_row = {'document_id': 4, 'uploaded_at': None, 'status': 'returned'}
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[{'document_type': 'invoice', 'authenticity_status': 'passed', 'risk_level': 'low'}],
                          anomaly_rows=[], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: _pass_comparison(), _classify_exception=lambda c, d, cmp: None):
        context = ra._build_case_context(cursor, 4)
    check('audit_status is REVIEW REQUIRED for a sent-back document',
          context['audit_status'] == 'REVIEW REQUIRED', context)
    check('reasons mention the send-back',
          any('sent back' in r.lower() for r in context['audit_status_reasons']), context['audit_status_reasons'])


def run_case_audit_status_authenticity_warning_forces_review():
    print('Case: an authenticity warning alone (everything else clean) -> REVIEW REQUIRED')
    doc_row = {'document_id': 5, 'uploaded_at': None, 'status': 'under_review'}
    cursor = _FakeCursor(doc_row=doc_row, authenticity_rows=[{'document_type': 'invoice', 'authenticity_status': 'warning', 'risk_level': 'medium'}],
                          anomaly_rows=[], history_rows=[])
    with _Patched(ra, _build_comparison=lambda c, d: _pass_comparison(), _classify_exception=lambda c, d, cmp: None):
        context = ra._build_case_context(cursor, 5)
    check('audit_status is REVIEW REQUIRED for an authenticity warning',
          context['audit_status'] == 'REVIEW REQUIRED', context)


# ============================================================
# routes/ai_assistant.py — _clamp_explain_exception_result()
# ============================================================

def run_case_clamp_explain_exception_overrides_wrong_ai_verdict():
    print("Case: the AI's own audit_status guess is IGNORED — the deterministic context value always wins")
    context = {'audit_status': 'PASS', 'audit_status_reasons': ['All core checks passed and no blocking findings']}
    ai_result = {'audit_status': 'REVIEW REQUIRED', 'reason': 'AI incorrectly thinks this needs review',
                 'recommended_action': 'AI incorrectly suggests holding it'}
    clamped = ra._clamp_explain_exception_result(ai_result, context)
    check('audit_status is forced to the deterministic PASS, not the AI\'s guess',
          clamped['audit_status'] == 'PASS', clamped)


def run_case_clamp_explain_exception_fills_blank_fields():
    print('Case: blank reason/recommended_action from the AI get sensible defaults')
    context = {'audit_status': 'PASS', 'audit_status_reasons': ['All core checks passed and no blocking findings']}
    clamped = ra._clamp_explain_exception_result({'reason': '', 'recommended_action': ''}, context)
    check('reason falls back to the deterministic reasons', bool(clamped['reason'].strip()), clamped)
    check('recommended_action falls back to a PASS-appropriate default',
          'ready for approval' in clamped['recommended_action'].lower(), clamped)

    review_context = {'audit_status': 'REVIEW REQUIRED', 'audit_status_reasons': ['Missing: Purchase Order']}
    clamped_review = ra._clamp_explain_exception_result({'reason': '', 'recommended_action': ''}, review_context)
    check('REVIEW REQUIRED default recommended_action is not the PASS message',
          'ready for approval' not in clamped_review['recommended_action'].lower(), clamped_review)


def run_case_clamp_explain_exception_handles_none_result():
    print('Case: clamp never crashes even if the AI returned nothing usable at all')
    context = {'audit_status': 'REVIEW REQUIRED', 'audit_status_reasons': ['Missing: Purchase Order']}
    clamped = ra._clamp_explain_exception_result(None, context)
    check('audit_status still comes from context', clamped['audit_status'] == 'REVIEW REQUIRED', clamped)
    check('reason is non-empty', bool(clamped['reason'].strip()), clamped)
    check('recommended_action is non-empty', bool(clamped['recommended_action'].strip()), clamped)


# ============================================================
# routes/ai_assistant.py — _run_action() cache hit/miss behaviour
# ============================================================

def run_case_run_action_cache_hit_never_calls_ai():
    print('Case: a cache hit returns the cached response without calling the AI at all')
    ai_call_count = []
    with _Patched(ra,
                  get_db_connection=lambda: object(),
                  _build_case_context=lambda c, d: {'invoice_number': 'INV-1'},
                  _cache_key=lambda ctx, q: 'fixed-hash',
                  _get_cached=lambda c, doc_id, action, h: {'answer': 'cached answer'},
                  ask_ai_assistant=lambda *a, **k: ai_call_count.append(1) or ({'answer': 'fresh'}, 'claude')):
        # get_db_connection().cursor(...) needs to work too — patch conn.
        class _Conn:
            def cursor(self, **k): return None
            def close(self): pass
        ra.get_db_connection = lambda: _Conn()
        # 'ask' is used here (not 'explain_exception') because this test
        # is about the generic cache mechanism, not the explain_exception-
        # specific audit_status clamp tested separately below.
        response, status = ra._run_action(1, 'ask')
    check('status is 200', status == 200, status)
    check('response is served from cache', response.get('cached') is True, response)
    check('AI was never called', ai_call_count == [], ai_call_count)


def run_case_run_action_cache_miss_calls_ai_and_saves():
    print('Case: a cache miss calls the AI once and saves the result to cache')
    save_calls = []

    class _Conn:
        def cursor(self, **k): return None
        def close(self): pass

    with _Patched(ra,
                  get_db_connection=lambda: _Conn(),
                  _build_case_context=lambda c, d: {'invoice_number': 'INV-1'},
                  _cache_key=lambda ctx, q: 'fixed-hash',
                  _get_cached=lambda c, doc_id, action, h: None,
                  ask_ai_assistant=lambda action, ctx, question=None: ({'answer': 'fresh answer'}, 'claude'),
                  _save_cache=lambda doc_id, action, h, resp: save_calls.append(resp)):
        response, status = ra._run_action(1, 'ask')
    check('status is 200', status == 200, status)
    check('response reflects the fresh AI call', response.get('answer') == 'fresh answer', response)
    check('response is marked not cached', response.get('cached') is False, response)
    check('result was saved to cache exactly once', len(save_calls) == 1, save_calls)


def run_case_run_action_ai_failure_returns_502():
    print('Case: when both providers fail, _run_action returns a 502')
    class _Conn:
        def cursor(self, **k): return None
        def close(self): pass
    with _Patched(ra,
                  get_db_connection=lambda: _Conn(),
                  _build_case_context=lambda c, d: {'invoice_number': 'INV-1'},
                  _cache_key=lambda ctx, q: 'fixed-hash',
                  _get_cached=lambda c, doc_id, action, h: None,
                  ask_ai_assistant=lambda action, ctx, question=None: (None, None)):
        response, status = ra._run_action(1, 'ask')
    check('status is 502', status == 502, status)
    check('response has an error message', 'error' in response, response)


def run_case_run_action_document_not_found_returns_404():
    print('Case: a nonexistent document returns 404 without calling the AI')
    ai_call_count = []
    class _Conn:
        def cursor(self, **k): return None
        def close(self): pass
    with _Patched(ra,
                  get_db_connection=lambda: _Conn(),
                  _build_case_context=lambda c, d: None,
                  ask_ai_assistant=lambda *a, **k: ai_call_count.append(1)):
        response, status = ra._run_action(999, 'ask')
    check('status is 404', status == 404, status)
    check('AI was never called', ai_call_count == [], ai_call_count)


if __name__ == '__main__':
    run_case_claude_success_never_calls_gemini()
    run_case_claude_none_falls_back_to_gemini()
    run_case_claude_bad_json_falls_back_to_gemini()
    run_case_both_providers_fail_returns_none()
    run_case_markdown_fences_are_stripped()
    run_case_ask_action_includes_question_in_prompt()
    run_case_send_back_prompt_lists_valid_enums()

    run_case_clamp_passes_through_valid_values()
    run_case_clamp_invalid_reason_category_falls_back()
    run_case_clamp_invalid_required_actions_falls_back()
    run_case_clamp_invalid_priority_falls_back()
    run_case_clamp_empty_instruction_gets_default_text()
    run_case_clamp_handles_none_result()

    run_case_cache_key_deterministic()
    run_case_cache_key_changes_with_question()
    run_case_cache_key_changes_with_context()

    run_case_build_case_context_missing_documents_and_exception()
    run_case_build_case_context_returns_none_when_no_comparison()
    run_case_build_case_context_clean_pass_has_no_exception()
    run_case_build_case_context_includes_send_back_cycle_when_present()
    run_case_build_case_context_send_back_cycle_none_when_never_returned()

    run_case_generate_finance_response_action_round_trips()
    run_case_recommended_steps_action_round_trips()
    run_case_generate_finance_response_prompt_references_send_back_cycle()

    run_case_require_finance_owner_accepts_the_actual_uploader()
    run_case_require_finance_owner_rejects_a_different_finance_user()
    run_case_require_finance_owner_rejects_non_finance_role()
    run_case_require_finance_owner_404_for_missing_document()

    run_case_classify_anomaly_reviewed_is_always_informational()
    run_case_classify_anomaly_pending_high_severity_is_blocking()
    run_case_classify_anomaly_pending_duplicate_or_amount_is_blocking()
    run_case_classify_anomaly_pending_low_pattern_is_informational()

    run_case_audit_status_full_pass_document()
    run_case_audit_status_full_pass_with_historical_reviewed_duplicate_is_still_pass()
    run_case_audit_status_missing_po_gr_document()
    run_case_audit_status_duplicate_invoice_document()
    run_case_audit_status_sent_back_document()
    run_case_audit_status_authenticity_warning_forces_review()

    run_case_clamp_explain_exception_overrides_wrong_ai_verdict()
    run_case_clamp_explain_exception_fills_blank_fields()
    run_case_clamp_explain_exception_handles_none_result()

    run_case_run_action_cache_hit_never_calls_ai()
    run_case_run_action_cache_miss_calls_ai_and_saves()
    run_case_run_action_ai_failure_returns_502()
    run_case_run_action_document_not_found_returns_404()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
