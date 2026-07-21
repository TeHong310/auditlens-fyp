"""Regression tests for helpers/authenticity_check.py's upgraded
authentication engine — schema normalization (Claude/Gemini -> one
unified v2 shape with status enums, per-key `required` flags, and
3-axis integrity risk), box flattening, and engine-selection/fallback
logic (Claude primary, Gemini fallback, OCR-text last resort). No real
Anthropic/Gemini API calls, no real DB writes — the Claude/Gemini call
functions and get_db_connection() are monkey-patched with fakes, same
house style as test_ai_router.py.

Usage:
    python tests/extraction/test_authenticity_ai.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import helpers.authenticity_check as ac

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


CLAUDE_RAW = {
    'supplier_identity': {
        'status': 'verified',
        'supplier_name': 'COILCRAFT SINGAPORE PTE LTD',
        'logo_detected': True,
        'address_detected': True,
        'contact_block_detected': True,
    },
    'document_visual_evidence': {
        'company_logo':     {'status': 'detected',     'confidence': 95, 'boxes': [10, 10, 60, 200]},
        'company_name':      {'status': 'detected',     'confidence': 95, 'boxes': [70, 10, 100, 300]},
        'supplier_address': {'status': 'detected',     'confidence': 90, 'boxes': None},
        'stamp':              {'status': 'detected',     'type': 'RECEIVED', 'confidence': 88, 'boxes': [800, 700, 900, 950]},
        'signature':          {'status': 'not_detected', 'confidence': 0,  'boxes': None},
    },
    'integrity_check': {
        'copy_paste_risk': 'low', 'font_consistency': 'low',
        'alteration_risk': 'low', 'reason': 'No visual anomalies found.',
    },
    'overall_result': {'status': 'PASS', 'risk_level': 'LOW', 'reasons': []},
}


# ── Pure-function tests: schema normalization / box flattening ──────────

def run_case_normalize_claude_trusts_shape():
    print('Case: _normalize_visual_result(claude, ...) trusts the v2 schema as-is')
    visual = ac._normalize_visual_result('claude', CLAUDE_RAW, 'invoice')
    check('supplier_name preserved', visual['supplier_identity']['supplier_name'] == 'COILCRAFT SINGAPORE PTE LTD')
    check('supplier status verified', visual['supplier_identity']['status'] == 'verified')
    check('stamp detected (status)', visual['document_visual_evidence']['stamp']['status'] == 'detected')
    check('stamp detected (bool, backward compat)', visual['document_visual_evidence']['stamp']['detected'] is True)
    check('signature not detected', visual['document_visual_evidence']['signature']['detected'] is False)
    check('signature never required', visual['document_visual_evidence']['signature']['required'] is False)
    check('stamp required on invoice', visual['document_visual_evidence']['stamp']['required'] is True)
    check('company_name always required', visual['document_visual_evidence']['company_name']['required'] is True)
    check('risk_level LOW', visual['overall_result']['risk_level'] == 'LOW')
    check('integrity 3-axis carried through', visual['integrity_check']['copy_paste_risk'] == 'low')


def run_case_normalize_claude_stamp_not_required_on_po():
    print('Case: _normalize_visual_result(claude, ..., document_type=po) -> stamp not required')
    visual = ac._normalize_visual_result('claude', CLAUDE_RAW, 'po')
    check('stamp not required on PO', visual['document_visual_evidence']['stamp']['required'] is False)


def run_case_normalize_claude_legacy_boolean_defensive():
    print('Case: _normalize_visual_result(claude, ...) accepts a legacy boolean `detected` shape defensively')
    legacy_shape = {
        'supplier_identity': {'supplier_name_detected': True, 'supplier_name': 'X'},
        'document_visual_evidence': {
            'company_logo': {'detected': True, 'confidence': 80, 'boxes': None},
            'company_name': {'detected': False, 'confidence': 0, 'boxes': None},
            'supplier_address': {'detected': False, 'confidence': 0, 'boxes': None},
            'stamp': {'detected': False, 'confidence': 0, 'boxes': None},
            'signature': {'detected': False, 'confidence': 0, 'boxes': None},
        },
    }
    visual = ac._normalize_visual_result('claude', legacy_shape, 'invoice')
    check('legacy detected=True maps to status=detected', visual['document_visual_evidence']['company_logo']['status'] == 'detected')
    check('supplier status derived from legacy supplier_name_detected', visual['supplier_identity']['status'] == 'verified')


def run_case_normalize_gemini_maps_old_schema():
    print('Case: _normalize_visual_result(gemini, ...) maps the old 4-signal schema to the v2 shape')
    old = {
        'has_company_chop': True, 'has_company_logo': True,
        'has_company_name': True, 'has_signature': False,
        'notes': 'looks fine', 'upload_source': 'scanned',
        'signal_boxes': {'has_company_chop': [1, 2, 3, 4]},
    }
    visual = ac._normalize_visual_result('gemini', old, 'invoice')
    check('stamp.detected mapped from has_company_chop', visual['document_visual_evidence']['stamp']['detected'] is True)
    check('stamp.boxes converted from signal_boxes', visual['document_visual_evidence']['stamp']['boxes'] == [1, 2, 3, 4])
    check('supplier_address defaults to not detected (no Gemini signal for it)',
          visual['document_visual_evidence']['supplier_address']['detected'] is False)
    check('overall status PASS when name detected', visual['overall_result']['status'] == 'PASS')
    check('reasons carries notes through', visual['overall_result']['reasons'] == ['looks fine'])
    check('signature still never required', visual['document_visual_evidence']['signature']['required'] is False)
    check('integrity defaults to low/not-assessed', visual['integrity_check']['copy_paste_risk'] == 'low')


def run_case_flatten_boxes():
    print('Case: _flatten_boxes() converts corner boxes to {type,label,x,y,width,height,confidence}')
    visual = ac._normalize_visual_result('claude', CLAUDE_RAW, 'invoice')
    boxes = ac._flatten_boxes(visual['document_visual_evidence'])
    by_type = {b['type']: b for b in boxes}
    check('3 boxes flattened (logo, name, stamp — address/signature have none)', len(boxes) == 3, boxes)
    check('supplier_logo x/y/width/height/confidence correct',
          by_type.get('supplier_logo') == {
              'type': 'supplier_logo', 'label': 'Company Logo',
              'x': 10, 'y': 10, 'width': 190, 'height': 50, 'confidence': 0.95,
          },
          by_type.get('supplier_logo'))
    check('company_stamp box present with confidence normalized to 0-1',
          by_type.get('company_stamp', {}).get('confidence') == 0.88, by_type.get('company_stamp'))


def run_case_authenticity_is_complete():
    print('Case: _authenticity_is_complete() gates on document_visual_evidence presence')
    check('None is incomplete', ac._authenticity_is_complete(None) is False)
    check('empty dict is incomplete', ac._authenticity_is_complete({}) is False)
    check('missing document_visual_evidence is incomplete',
          ac._authenticity_is_complete({'supplier_identity': {}}) is False)
    check('full Claude result is complete', ac._authenticity_is_complete(CLAUDE_RAW) is True)


def run_case_stamp_required():
    print('Case: _stamp_required() derives from AUTH_RULES per document type')
    check('invoice: chop is important -> required', ac._stamp_required('invoice') is True)
    check('po: chop is optional -> not required', ac._stamp_required('po') is False)
    check('gr: chop is important -> required', ac._stamp_required('gr') is True)
    check('grn alias resolves like gr', ac._stamp_required('grn') is True)


# ── Engine selection tests: Claude primary, Gemini fallback, DB write ──

class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (999,)


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        pass


class _Patched:
    """Context manager that monkey-patches the module-level names
    run_authenticity_check() actually calls, restoring them afterward —
    same technique test_ai_router.py uses for AI_EXTRACTION_PROVIDER."""

    def __init__(self, fake_conn, analyze_claude, call_gemini):
        self.fake_conn = fake_conn
        self.analyze_claude = analyze_claude
        self.call_gemini = call_gemini
        self._originals = {}

    def __enter__(self):
        self._originals = {
            'analyze_document_authenticity': ac.analyze_document_authenticity,
            '_call_gemini_vision':           ac._call_gemini_vision,
            'get_db_connection':             ac.get_db_connection,
            'save_rendered_authenticity_image': ac.save_rendered_authenticity_image,
            'prepare_gemini_image_payload':  ac.prepare_gemini_image_payload,
        }
        ac.analyze_document_authenticity = self.analyze_claude
        ac._call_gemini_vision = self.call_gemini
        ac.get_db_connection = lambda: self.fake_conn
        ac.save_rendered_authenticity_image = lambda *a, **k: None
        ac.prepare_gemini_image_payload = lambda fb, fn: ('image/png', b'fake')
        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(ac, name, value)


def run_case_claude_success_no_gemini_call():
    print('Case: Claude succeeds -> Gemini is never called, row written with engine=claude')
    fake_conn = _FakeConn()
    calls = {'gemini': 0}

    def fake_gemini(fb, fn):
        calls['gemini'] += 1
        return None

    with _Patched(fake_conn, lambda image, doc_type: CLAUDE_RAW, fake_gemini):
        check_id = ac.run_authenticity_check(1, b'fake-bytes', 'test.pdf', 'invoice',
                                              document_consistency={'vendor_match': True})
    check('check_id returned', check_id == 999, check_id)
    check('Gemini never called', calls['gemini'] == 0, calls)
    check('row committed', fake_conn.committed is True)
    params = fake_conn.cursor_obj.executed[0][1]
    check('ai_engine_used stored as claude', params[8] == 'claude', params)


def run_case_claude_fails_falls_back_to_gemini():
    print('Case: Claude fails -> falls back to Gemini, engine=gemini')
    fake_conn = _FakeConn()
    gemini_result = {
        'has_company_chop': True, 'has_company_logo': True, 'has_company_name': True,
        'has_signature': False, 'notes': 'ok', 'upload_source': 'scanned', 'signal_boxes': {},
    }

    with _Patched(fake_conn, lambda image, doc_type: None, lambda fb, fn: gemini_result):
        check_id = ac.run_authenticity_check(1, b'fake-bytes', 'test.pdf', 'invoice')
    check('check_id returned', check_id == 999, check_id)
    params = fake_conn.cursor_obj.executed[0][1]
    check('ai_engine_used stored as gemini', params[8] == 'gemini', params)


def run_case_both_fail_uses_ocr_fallback():
    print('Case: Claude and Gemini both fail -> OCR-text fallback, engine=fallback')
    fake_conn = _FakeConn()

    with _Patched(fake_conn, lambda image, doc_type: None, lambda fb, fn: None):
        check_id = ac.run_authenticity_check(1, b'fake-bytes', 'test.pdf', 'po')
    check('check_id returned', check_id == 999, check_id)
    params = fake_conn.cursor_obj.executed[0][1]
    check('ai_engine_used stored as fallback', params[8] == 'fallback', params)


def run_case_document_consistency_passed_through():
    print('Case: document_consistency is stored as passed in, not recomputed here')
    fake_conn = _FakeConn()
    consistency = {'vendor_match': True, 'po_match': False, 'item_match': None, 'amount_match': True}

    with _Patched(fake_conn, lambda image, doc_type: CLAUDE_RAW, lambda fb, fn: None):
        ac.run_authenticity_check(1, b'fake-bytes', 'test.pdf', 'invoice', document_consistency=consistency)
    params = fake_conn.cursor_obj.executed[0][1]
    stored = json.loads(params[10])
    check('document_consistency stored verbatim', stored == consistency, stored)


if __name__ == '__main__':
    run_case_normalize_claude_trusts_shape()
    run_case_normalize_claude_stamp_not_required_on_po()
    run_case_normalize_claude_legacy_boolean_defensive()
    run_case_normalize_gemini_maps_old_schema()
    run_case_flatten_boxes()
    run_case_authenticity_is_complete()
    run_case_stamp_required()
    run_case_claude_success_no_gemini_call()
    run_case_claude_fails_falls_back_to_gemini()
    run_case_both_fail_uses_ocr_fallback()
    run_case_document_consistency_passed_through()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
