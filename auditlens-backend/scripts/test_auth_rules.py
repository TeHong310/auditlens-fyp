"""Unit-level tests for helpers/auth_rules.py — the document-type-aware
authentication scoring engine (Task 7 of the "Authentication Logic
Architecture" improvement).

Pure in-process tests: no Gemini call, no document upload, no DB
connection — compute_authentication() only takes plain booleans and
returns a plain dict, so it's testable in complete isolation from the
rest of the pipeline. Matches this repo's existing dependency-free
scripts/ convention (no pytest).

Usage:
    python scripts/test_auth_rules.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.auth_rules import compute_authentication

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_1_invoice_pass():
    """Task 7 Case 1: Invoice — company name + logo + invoice number,
    no signature. Expected: PASS."""
    print('Case 1: Invoice, name+logo+number present, no signature -> PASS')
    result = compute_authentication('invoice', {
        'company_name': True,
        'company_logo': True,
        'company_chop': False,
        'signature':    False,
        'doc_number':   True,
    })
    check('status is PASS', result['authentication_status'] == 'PASS', result)
    check('score >= invoice pass threshold (70)', result['authentication_score'] >= 70, result['authentication_score'])
    sig = next(d for d in result['signal_details'] if d['name'] == 'Signature')
    check('signature is optional category', sig['category'] == 'optional', sig)
    check('signature message explains it is not required',
          'not required' in sig.get('message', ''), sig)


def run_case_2_po_pass():
    """Task 7 Case 2: PO — company name + PO number, no chop/signature.
    Expected: PASS."""
    print('Case 2: PO, name+number present, no chop/signature -> PASS')
    result = compute_authentication('po', {
        'company_name': True,
        'company_logo': False,
        'company_chop': False,
        'signature':    False,
        'doc_number':   True,
    })
    check('status is PASS', result['authentication_status'] == 'PASS', result)
    check('score >= po pass threshold (70)', result['authentication_score'] >= 70, result['authentication_score'])
    chop = next(d for d in result['signal_details'] if d['name'] == 'Company Chop')
    check('company_chop is optional for PO', chop['category'] == 'optional', chop)


def run_case_3_gr_pass():
    """Task 7 Case 3: GR — company name + GR number + receiving stamp
    (company_chop), no signature. Expected: PASS."""
    print('Case 3: GR, name+number+chop present, no signature -> PASS')
    result = compute_authentication('gr', {
        'company_name': True,
        'company_logo': False,
        'company_chop': True,
        'signature':    False,
        'doc_number':   True,
    })
    check('status is PASS', result['authentication_status'] == 'PASS', result)
    check('score >= gr pass threshold (80)', result['authentication_score'] >= 80, result['authentication_score'])
    chop = next(d for d in result['signal_details'] if d['name'] == 'Company Chop')
    check('company_chop is important for GR (weighted higher than signature)',
          chop['category'] == 'important', chop)


def run_case_4_unknown_doc_fail():
    """Task 7 Case 4: Unknown document type, missing company identity.
    Expected: FAIL."""
    print('Case 4: Unknown document type, no company identity -> FAIL')
    result = compute_authentication('unknown_type', {
        'company_name': False,
        'company_logo': False,
        'company_chop': False,
        'signature':    False,
        'doc_number':   False,
    })
    check('status is FAIL', result['authentication_status'] == 'FAIL', result)
    check('score < 50', result['authentication_score'] < 50, result['authentication_score'])


def run_bonus_invoice_all_signals_present():
    """Sanity check: every configured signal present -> perfect 100 score."""
    print('Bonus: Invoice, every signal present -> 100/100 PASS')
    result = compute_authentication('invoice', {
        'company_name': True,
        'company_logo': True,
        'company_chop': True,
        'signature':    True,
        'doc_number':   True,
    })
    check('score is exactly 100', result['authentication_score'] == 100, result['authentication_score'])
    check('status is PASS', result['authentication_status'] == 'PASS', result)
    check('no signal_details entry has a message key when all detected',
          all('message' not in d for d in result['signal_details']), result['signal_details'])


def run_bonus_gr_review_band():
    """Sanity check: GR with only the two required signals (no chop) lands
    in REVIEW, not PASS (80 threshold) or FAIL (50 threshold) — confirms
    the middle band is reachable, not just the two extremes."""
    print('Bonus: GR, only required signals present -> REVIEW (not PASS/FAIL)')
    result = compute_authentication('gr', {
        'company_name': True,
        'company_logo': False,
        'company_chop': False,
        'signature':    False,
        'doc_number':   True,
    })
    check('status is REVIEW', result['authentication_status'] == 'REVIEW', result)
    check('score is between FAIL(50) and PASS(80) thresholds',
          50 <= result['authentication_score'] < 80, result['authentication_score'])


def run_bonus_missing_required_signal_has_message():
    """Sanity check: a missing required signal gets an explanatory message
    distinct from an optional one (Task 4)."""
    print('Bonus: missing required signal message differs from missing optional')
    result = compute_authentication('invoice', {
        'company_name': False,
        'company_logo': False,
        'company_chop': False,
        'signature':    False,
        'doc_number':   False,
    })
    name = next(d for d in result['signal_details'] if d['name'] == 'Company Name')
    sig = next(d for d in result['signal_details'] if d['name'] == 'Signature')
    check('missing required signal message says "required"',
          'required for' in name.get('message', ''), name)
    check('missing optional signal message says "not required"',
          'not required' in sig.get('message', ''), sig)
    check('required-signal message differs from optional-signal message',
          name.get('message') != sig.get('message'), (name, sig))


if __name__ == '__main__':
    run_case_1_invoice_pass()
    run_case_2_po_pass()
    run_case_3_gr_pass()
    run_case_4_unknown_doc_fail()
    run_bonus_invoice_all_signals_present()
    run_bonus_gr_review_band()
    run_bonus_missing_required_signal_has_message()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} check(s) FAILED:')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
