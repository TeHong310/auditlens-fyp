"""Regression tests for the v4 "auditor decision engine" additions:
helpers/authenticity_check.py::_compute_auditor_score() (deterministic
0-100 score + APPROVE/REVIEW/REJECT decision) and
routes/authenticity.py::_cross_document_authenticity_for() (Invoice/PO/
GR supplier-identity + reference-number + item + timeline comparison,
reusing the existing matching engine). No real DB, no real AI calls.

Usage:
    python tests/extraction/test_authenticity_scoring.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import helpers.authenticity_check as ac
import routes.authenticity as ra

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def _visual(supplier_status, evidence_overrides=None, integrity_overrides=None):
    """Builds a minimal normalized `visual` dict (the shape
    _normalize_visual_result() returns) for feeding directly into
    _compute_auditor_score(), without needing a real Claude/Gemini call."""
    evidence = {
        'company_name':     {'detected': True, 'required': True, 'label': 'Company Name'},
        'company_logo':     {'detected': True, 'required': True, 'label': 'Company Logo'},
        'supplier_address': {'detected': True, 'required': True, 'label': 'Supplier Address'},
        'stamp':             {'detected': True, 'required': True, 'label': 'Stamp'},
        'signature':          {'detected': False, 'required': False, 'label': 'Signature'},
    }
    for key, overrides in (evidence_overrides or {}).items():
        evidence[key].update(overrides)

    integrity = {
        'copy_paste_risk': 'low', 'font_consistency': 'low',
        'alignment_consistency': 'low', 'alteration_risk': 'low',
    }
    integrity.update(integrity_overrides or {})

    return {
        'supplier_identity': {'status': supplier_status},
        'document_visual_evidence': evidence,
        'integrity_check': integrity,
    }


# ── _compute_auditor_score ──────────────────────────────────────────────

def run_case_score_high_approves():
    print('Case: everything detected, supplier verified, integrity clean -> high score, APPROVE')
    visual = _visual('verified')
    result = ac._compute_auditor_score(visual)
    check('score is 100 (capped)', result['authenticity_score'] == 100, result)
    check('risk_level LOW', result['risk_level'] == 'LOW')
    check('decision APPROVE', result['decision'] == 'APPROVE')
    check('positive reasons include supplier verified', 'Supplier identity verified' in result['reasons']['positive'])
    check('negative reasons include the always-present, zero-weight signature note',
          any('not required' in n for n in result['reasons']['negative']))


def run_case_score_medium_reviews():
    print('Case: supplier uncertain, logo missing, one integrity axis medium -> REVIEW band')
    visual = _visual('uncertain',
                      evidence_overrides={'company_logo': {'detected': False}, 'stamp': {'required': False}},
                      integrity_overrides={'font_consistency': 'medium'})
    result = ac._compute_auditor_score(visual)
    check('score in REVIEW band (60-84)', 60 <= result['authenticity_score'] <= 84, result)
    check('decision REVIEW', result['decision'] == 'REVIEW')
    check('risk_level MEDIUM', result['risk_level'] == 'MEDIUM')
    check('negative reasons mention missing logo', any('Company Logo not detected' in n for n in result['reasons']['negative']), result['reasons'])


def run_case_score_low_rejects():
    print('Case: supplier not found, nothing detected, integrity all high -> REJECT')
    visual = _visual('not_found',
                      evidence_overrides={
                          'company_name': {'detected': False}, 'company_logo': {'detected': False},
                          'supplier_address': {'detected': False}, 'stamp': {'detected': False},
                      },
                      integrity_overrides={
                          'copy_paste_risk': 'high', 'font_consistency': 'high',
                          'alignment_consistency': 'high', 'alteration_risk': 'high',
                      })
    result = ac._compute_auditor_score(visual)
    check('score is 0 (floored)', result['authenticity_score'] == 0, result)
    check('decision REJECT', result['decision'] == 'REJECT')
    check('risk_level HIGH', result['risk_level'] == 'HIGH')
    check("decision_reason doesn't cite the zero-weight signature note as the top reason",
          'not required' not in result['decision_reason'], result['decision_reason'])


def run_case_score_stamp_not_required_excluded_from_denominator():
    print('Case: stamp not required (e.g. PO) -> excluded from evidence max, not penalized when absent')
    visual_required = _visual('verified', evidence_overrides={'stamp': {'detected': False, 'required': True}})
    visual_not_required = _visual('verified', evidence_overrides={'stamp': {'detected': False, 'required': False}})
    result_required = ac._compute_auditor_score(visual_required)
    result_not_required = ac._compute_auditor_score(visual_not_required)
    check('missing a REQUIRED stamp scores lower than missing a NOT-required one',
          result_required['authenticity_score'] < result_not_required['authenticity_score'],
          (result_required['authenticity_score'], result_not_required['authenticity_score']))
    check('not-required missing stamp produces no "not detected" penalty for it',
          not any('Stamp not detected' in n for n in result_not_required['reasons']['negative']),
          result_not_required['reasons'])


# ── _cross_document_authenticity_for ────────────────────────────────────

class _FakeCursor:
    def __init__(self, checked_rows, comparison):
        self.checked_rows = checked_rows
        self.comparison = comparison
        self._last_query = None

    def execute(self, sql, params=None):
        self._last_query = sql

    def fetchall(self):
        return self.checked_rows


def run_case_cross_document_needs_at_least_two_types():
    print('Case: only 1 document type checked so far -> returns None (nothing to cross-compare)')
    cursor = _FakeCursor([{'document_type': 'invoice', 'ai_visual_result': {'supplier_identity': {'supplier_name': 'X'}}}], None)
    orig = ra._build_comparison
    try:
        result = ra._cross_document_authenticity_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('None when fewer than 2 types checked', result is None, result)


def run_case_cross_document_matching_suppliers_scores_well():
    print('Case: Invoice+PO+GR supplier names all match, comparison fields all match -> high score, no issues')
    checked_rows = [
        {'document_type': 'invoice', 'ai_visual_result': {'supplier_identity': {'supplier_name': 'COILCRAFT SINGAPORE PTE LTD'}}},
        {'document_type': 'po',      'ai_visual_result': {'supplier_identity': {'supplier_name': 'Coilcraft Singapore Pte. Ltd.'}}},
        {'document_type': 'gr',      'ai_visual_result': {'supplier_identity': {'supplier_name': 'COILCRAFT SINGAPORE PTE LTD'}}},
    ]
    cursor = _FakeCursor(checked_rows, None)
    orig = ra._build_comparison
    ra._build_comparison = lambda cur, doc_id: {
        'match_result': {'po_reference_match': True, 'line_items_match': True, 'date_order_valid': True}
    }
    try:
        result = ra._cross_document_authenticity_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('cross_document_score is 100', result['cross_document_score'] == 100, result)
    check('no issues', result['issues'] == [], result)


def run_case_cross_document_mismatched_supplier_flags_issue():
    print('Case: PO supplier differs from Invoice/GR supplier -> flagged as an issue, score penalized')
    checked_rows = [
        {'document_type': 'invoice', 'ai_visual_result': {'supplier_identity': {'supplier_name': 'COILCRAFT SINGAPORE PTE LTD'}}},
        {'document_type': 'po',      'ai_visual_result': {'supplier_identity': {'supplier_name': 'EMITS TECHNOLOGY SDN BHD'}}},
    ]
    cursor = _FakeCursor(checked_rows, None)
    orig = ra._build_comparison
    ra._build_comparison = lambda cur, doc_id: None
    try:
        result = ra._cross_document_authenticity_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('cross_document_score is 0 (only identity check applicable, and it failed)',
          result['cross_document_score'] == 0, result)
    check('issue mentions supplier identity differing', any('Supplier identity differs' in i for i in result['issues']), result)


def run_case_cross_document_reference_mismatch_flagged():
    print('Case: matching engine reports a reference-number mismatch -> flagged as an issue')
    checked_rows = [
        {'document_type': 'invoice', 'ai_visual_result': {'supplier_identity': {'supplier_name': 'X'}}},
        {'document_type': 'po',      'ai_visual_result': {'supplier_identity': {'supplier_name': 'X'}}},
    ]
    cursor = _FakeCursor(checked_rows, None)
    orig = ra._build_comparison
    ra._build_comparison = lambda cur, doc_id: {
        'match_result': {'po_reference_match': False, 'line_items_match': True, 'date_order_valid': None}
    }
    try:
        result = ra._cross_document_authenticity_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('reference mismatch flagged', any('reference numbers' in i for i in result['issues']), result)
    check('date_order_valid=None is skipped, not treated as a failure',
          not any('out of expected order' in i for i in result['issues']), result)


def run_case_cross_document_ignores_date_order():
    print('Case (v5): cross_document_score excludes date_order_valid entirely — a bad date order')
    print('           must not lower it, and must never produce a "dates out of order" issue there')
    checked_rows = [
        {'document_type': 'invoice', 'ai_visual_result': {'supplier_identity': {'supplier_name': 'X'}}},
        {'document_type': 'po',      'ai_visual_result': {'supplier_identity': {'supplier_name': 'X'}}},
    ]
    cursor = _FakeCursor(checked_rows, None)
    orig = ra._build_comparison
    ra._build_comparison = lambda cur, doc_id: {
        'match_result': {'po_reference_match': True, 'line_items_match': True, 'date_order_valid': False}
    }
    try:
        result = ra._cross_document_authenticity_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('cross_document_score is 100 despite date_order_valid=False', result['cross_document_score'] == 100, result)
    check('no date-related issue in cross_document_authenticity',
          not any('out of expected order' in i for i in result['issues']), result)


# ── _workflow_consistency_for (v5: separated from authenticity/cross-document score) ──

def run_case_workflow_consistency_valid_order():
    print('Case: date_order_valid=True -> workflow_consistency_score=100, no issues')
    cursor = _FakeCursor([], None)
    orig = ra._build_comparison
    ra._build_comparison = lambda cur, doc_id: {'match_result': {'date_order_valid': True}}
    try:
        result = ra._workflow_consistency_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('workflow_consistency_score is 100', result['workflow_consistency_score'] == 100, result)
    check('no issues', result['issues'] == [], result)


def run_case_workflow_consistency_invalid_order():
    print('Case: date_order_valid=False -> workflow_consistency_score=0, issue explains the date problem')
    cursor = _FakeCursor([], None)
    orig = ra._build_comparison
    ra._build_comparison = lambda cur, doc_id: {'match_result': {'date_order_valid': False}}
    try:
        result = ra._workflow_consistency_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('workflow_consistency_score is 0', result['workflow_consistency_score'] == 0, result)
    check('issue explains the date order problem', any('out of expected order' in i for i in result['issues']), result)


def run_case_workflow_consistency_not_applicable():
    print('Case: no comparison data or date_order_valid=None -> returns None (not applicable)')
    cursor = _FakeCursor([], None)
    orig = ra._build_comparison
    ra._build_comparison = lambda cur, doc_id: None
    try:
        result_no_comparison = ra._workflow_consistency_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('None when no comparison data at all', result_no_comparison is None, result_no_comparison)

    ra._build_comparison = lambda cur, doc_id: {'match_result': {'date_order_valid': None}}
    try:
        result_none_date = ra._workflow_consistency_for(cursor, 1)
    finally:
        ra._build_comparison = orig
    check('None when date_order_valid itself is None', result_none_date is None, result_none_date)


if __name__ == '__main__':
    run_case_score_high_approves()
    run_case_score_medium_reviews()
    run_case_score_low_rejects()
    run_case_score_stamp_not_required_excluded_from_denominator()
    run_case_cross_document_needs_at_least_two_types()
    run_case_cross_document_matching_suppliers_scores_well()
    run_case_cross_document_mismatched_supplier_flags_issue()
    run_case_cross_document_reference_mismatch_flagged()
    run_case_cross_document_ignores_date_order()
    run_case_workflow_consistency_valid_order()
    run_case_workflow_consistency_invalid_order()
    run_case_workflow_consistency_not_applicable()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
