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

import helpers.ai_assistant as haa
import routes.ai_assistant as ra

FAILURES = []


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
    rows, anomalies rows, review_records rows."""

    def __init__(self, doc_row, authenticity_rows, anomaly_rows, history_rows):
        self._queue = [('one', doc_row), ('many', authenticity_rows),
                        ('many', anomaly_rows), ('many', history_rows)]
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
                    'currency': 'RM', 'invoice_date': '2026-07-01'},
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
                    'currency': 'RM', 'invoice_date': '2026-07-01'},
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
        response, status = ra._run_action(1, 'explain_exception')
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
        response, status = ra._run_action(1, 'explain_exception')
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
        response, status = ra._run_action(1, 'explain_exception')
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
        response, status = ra._run_action(999, 'explain_exception')
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
