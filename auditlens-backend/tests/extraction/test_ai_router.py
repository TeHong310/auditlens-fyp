"""Regression tests for helpers/ai_extractor_router.py — the routing
DECISIONS themselves (which provider gets called, when Gemini fallback
triggers and why), not real API calls. claude_call/gemini_call are
simple counting stubs, so these tests prove the actual cost-control
mechanism (a provider that isn't needed is never invoked) without
touching the network or either SDK.

Usage:
    python tests/extraction/test_ai_router.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import helpers.ai_extractor_router as router

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def _make_calls(claude_return, gemini_return):
    calls = {'claude': 0, 'gemini': 0}

    def claude_call():
        calls['claude'] += 1
        return claude_return

    def gemini_call():
        calls['gemini'] += 1
        return gemini_return

    return calls, claude_call, gemini_call


COMPLETE_RESULT = {
    'vendor_name': 'COILCRAFT SINGAPORE PTE LTD',
    'total_amount': 8020.00,
    'currency': 'USD',
    'line_items': [{'description': 'CHIP INDUCTORS', 'part_number': '0603DC-12NXGRW', 'quantity': 4000}],
}


def run_case_claude_complete_gemini_never_called():
    print('Case: CLAUDE mode, complete result — Gemini never invoked')
    router.AI_EXTRACTION_PROVIDER = 'CLAUDE'
    calls, claude_call, gemini_call = _make_calls(COMPLETE_RESULT, {'should': 'not be used'})

    result, provider_used, reason = router.route_ai_extraction('invoice', claude_call, gemini_call)

    check('claude_call invoked exactly once', calls['claude'] == 1, calls)
    check('gemini_call NEVER invoked', calls['gemini'] == 0, calls)
    check('provider_used == CLAUDE', provider_used == 'CLAUDE', provider_used)
    check('fallback reason is None', reason is None, reason)
    check('result is Claude\'s result', result == COMPLETE_RESULT, result)


def run_case_claude_missing_vendor_falls_back():
    print('Case: CLAUDE mode, missing vendor_name — falls back to Gemini')
    router.AI_EXTRACTION_PROVIDER = 'CLAUDE'
    incomplete = dict(COMPLETE_RESULT, vendor_name=None)
    gemini_result = {'vendor_name': 'COILCRAFT SINGAPORE PTE LTD', 'total_amount': 8020.0, 'line_items': [{}]}
    calls, claude_call, gemini_call = _make_calls(incomplete, gemini_result)

    result, provider_used, reason = router.route_ai_extraction('invoice', claude_call, gemini_call)

    check('claude_call invoked exactly once', calls['claude'] == 1, calls)
    check('gemini_call invoked exactly once (fallback)', calls['gemini'] == 1, calls)
    check('provider_used == GEMINI', provider_used == 'GEMINI', provider_used)
    check('fallback reason mentions vendor_name', reason == 'missing vendor_name', reason)
    check('result is Gemini\'s result', result == gemini_result, result)


def run_case_claude_missing_total_amount_falls_back():
    print('Case: CLAUDE mode, missing total_amount — falls back to Gemini')
    router.AI_EXTRACTION_PROVIDER = 'CLAUDE'
    incomplete = dict(COMPLETE_RESULT, total_amount=None)
    calls, claude_call, gemini_call = _make_calls(incomplete, {'vendor_name': 'X', 'total_amount': 1, 'line_items': [{}]})

    result, provider_used, reason = router.route_ai_extraction('invoice', claude_call, gemini_call)
    check('fallback reason mentions total_amount', reason == 'missing total_amount', reason)
    check('gemini_call invoked exactly once', calls['gemini'] == 1, calls)


def run_case_claude_zero_line_items_falls_back():
    print('Case: CLAUDE mode, zero line_items — falls back to Gemini')
    router.AI_EXTRACTION_PROVIDER = 'CLAUDE'
    incomplete = dict(COMPLETE_RESULT, line_items=[])
    calls, claude_call, gemini_call = _make_calls(incomplete, {'vendor_name': 'X', 'total_amount': 1, 'line_items': [{}]})

    result, provider_used, reason = router.route_ai_extraction('invoice', claude_call, gemini_call)
    check('fallback reason mentions line_items', reason == 'zero line_items', reason)
    check('gemini_call invoked exactly once', calls['gemini'] == 1, calls)


def run_case_claude_api_error_falls_back():
    """Claude returning None represents ANY hard failure per helpers/
    claude_extractor.py's fail-soft contract: invalid JSON, API error,
    timeout, or no API key — all collapse to None before the router ever
    sees them."""
    print('Case: CLAUDE mode, Claude returns None (API error/timeout/invalid JSON) — falls back to Gemini')
    router.AI_EXTRACTION_PROVIDER = 'CLAUDE'
    calls, claude_call, gemini_call = _make_calls(None, {'vendor_name': 'X', 'total_amount': 1, 'line_items': [{}]})

    result, provider_used, reason = router.route_ai_extraction('invoice', claude_call, gemini_call)
    check('fallback reason mentions invalid/empty result', reason == 'invalid JSON or empty result', reason)
    check('provider_used == GEMINI', provider_used == 'GEMINI', provider_used)


def run_case_gemini_mode_claude_never_called():
    print('Case: GEMINI mode — Claude never invoked at all')
    router.AI_EXTRACTION_PROVIDER = 'GEMINI'
    calls, claude_call, gemini_call = _make_calls({'should': 'not be used'}, COMPLETE_RESULT)

    result, provider_used, reason = router.route_ai_extraction('invoice', claude_call, gemini_call)

    check('claude_call NEVER invoked', calls['claude'] == 0, calls)
    check('gemini_call invoked exactly once', calls['gemini'] == 1, calls)
    check('provider_used == GEMINI', provider_used == 'GEMINI', provider_used)
    check('fallback reason is None (not a fallback, the configured provider)', reason is None, reason)


def run_case_hybrid_mode_same_as_claude():
    print('Case: HYBRID mode — same Claude-first-then-fallback behavior as CLAUDE')
    router.AI_EXTRACTION_PROVIDER = 'HYBRID'
    calls, claude_call, gemini_call = _make_calls(COMPLETE_RESULT, {'should': 'not be used'})

    result, provider_used, reason = router.route_ai_extraction('invoice', claude_call, gemini_call)
    check('claude_call invoked exactly once', calls['claude'] == 1, calls)
    check('gemini_call NEVER invoked (Claude was complete)', calls['gemini'] == 0, calls)
    check('provider_used == CLAUDE', provider_used == 'CLAUDE', provider_used)


if __name__ == '__main__':
    _original_provider = router.AI_EXTRACTION_PROVIDER
    try:
        run_case_claude_complete_gemini_never_called()
        run_case_claude_missing_vendor_falls_back()
        run_case_claude_missing_total_amount_falls_back()
        run_case_claude_zero_line_items_falls_back()
        run_case_claude_api_error_falls_back()
        run_case_gemini_mode_claude_never_called()
        run_case_hybrid_mode_same_as_claude()
    finally:
        router.AI_EXTRACTION_PROVIDER = _original_provider

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
