"""Regression tests for routes/authenticity.py::
generate_authenticity_if_missing() — the generalized auto-trigger (used by
generate_invoice_authenticity_if_missing() for 'invoice', and called
directly by routes/documents.py's upload_purchase_order()/
upload_goods_receipt() for 'po'/'gr') that runs the EXISTING authenticity
engine (run_authenticity_check) right after a successful PO/GR extraction,
instead of only on-demand when an auditor opens a record.

No real Anthropic/Gemini API calls, no real DB writes — get_db_connection,
_document_consistency_for, _extracted_vendor_name_for, and
run_authenticity_check are monkey-patched with fakes, same house style as
test_authenticity_auto_trigger.py.

Usage:
    python tests/extraction/test_authenticity_po_gr_auto_trigger.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import routes.authenticity as az

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class _FakeCursor:
    def __init__(self, existing_row):
        self.existing_row = existing_row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.existing_row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, existing_row):
        self.cursor_obj = _FakeCursor(existing_row)
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self.cursor_obj

    def close(self):
        self.closed = True


class _Patched:
    def __init__(self, existing_row, run_check, document_consistency=None, extracted_vendor_name=None):
        self.existing_row = existing_row
        self.run_check = run_check
        self.document_consistency = document_consistency
        self.extracted_vendor_name = extracted_vendor_name
        self._originals = {}
        self.fake_conn = None

    def __enter__(self):
        self._originals = {
            'get_db_connection':      az.get_db_connection,
            '_document_consistency_for':  az._document_consistency_for,
            '_extracted_vendor_name_for': az._extracted_vendor_name_for,
            'run_authenticity_check': az.run_authenticity_check,
        }
        self.fake_conn = _FakeConn(self.existing_row)
        az.get_db_connection = lambda: self.fake_conn
        az._document_consistency_for = lambda cursor, document_id: self.document_consistency
        az._extracted_vendor_name_for = lambda cursor, document_id, document_type: self.extracted_vendor_name
        az.run_authenticity_check = self.run_check
        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(az, name, value)


def run_case_po_missing_row_triggers_the_existing_engine_once():
    print('Case: PO — no authenticity_checks row exists -> run_authenticity_check is called exactly once')
    calls = []

    def fake_run_check(document_id, file_bytes, file_name, document_type,
                        document_consistency=None, extracted_vendor_name=None, use_cache=True):
        calls.append((document_id, file_bytes, file_name, document_type, document_consistency, extracted_vendor_name))
        return 501

    with _Patched(existing_row=None, run_check=fake_run_check,
                  document_consistency={'vendor_match': True}, extracted_vendor_name='Zenith Industries'):
        result = az.generate_authenticity_if_missing(68, b'fake-po-bytes', 'po68.pdf', 'po')

    check('returns the new check_id', result == 501, result)
    check('run_authenticity_check called exactly once', len(calls) == 1, calls)
    check('called with document_type=po', calls[0][3] == 'po', calls)
    check('called with the exact file bytes passed in (no DB re-fetch)', calls[0][1] == b'fake-po-bytes', calls)


def run_case_gr_missing_row_triggers_the_existing_engine_once():
    print('Case: GR — no authenticity_checks row exists -> run_authenticity_check is called exactly once')
    calls = []

    def fake_run_check(document_id, file_bytes, file_name, document_type,
                        document_consistency=None, extracted_vendor_name=None, use_cache=True):
        calls.append((document_id, file_bytes, file_name, document_type))
        return 502

    with _Patched(existing_row=None, run_check=fake_run_check):
        result = az.generate_authenticity_if_missing(68, b'fake-gr-bytes', 'gr68.pdf', 'gr')

    check('returns the new check_id', result == 502, result)
    check('run_authenticity_check called exactly once', len(calls) == 1, calls)
    check('called with document_type=gr', calls[0][3] == 'gr', calls)


def run_case_existing_row_skips_and_makes_zero_ai_calls():
    print('Case: a PO authenticity_checks row already exists -> the engine is NEVER called (zero AI cost)')
    calls = []

    def fake_run_check(*a, **k):
        calls.append((a, k))
        return 999

    with _Patched(existing_row={'exists': True}, run_check=fake_run_check):
        result = az.generate_authenticity_if_missing(66, b'fake-po-bytes', 'po66.pdf', 'po')

    check('returns None (skipped)', result is None, result)
    check('run_authenticity_check NEVER called', len(calls) == 0, calls)


def run_case_engine_returning_none_does_not_raise():
    print('Case: run_authenticity_check() itself fails (returns None) -> no exception')
    def fake_run_check(*a, **k):
        return None

    with _Patched(existing_row=None, run_check=fake_run_check):
        result = az.generate_authenticity_if_missing(68, b'fake-gr-bytes', 'gr68.pdf', 'gr')

    check('returns None, does not raise', result is None, result)


def run_case_engine_raising_an_exception_is_caught():
    print('Case: run_authenticity_check() raises an exception -> caught, upload is never broken')
    def fake_run_check(*a, **k):
        raise RuntimeError('simulated Claude/Gemini outage')

    with _Patched(existing_row=None, run_check=fake_run_check):
        try:
            result = az.generate_authenticity_if_missing(68, b'fake-po-bytes', 'po68.pdf', 'po')
            raised = False
        except Exception:
            result = None
            raised = True

    check('exception is swallowed, not propagated', raised is False)
    check('returns None on failure', result is None, result)


def run_case_invoice_wrapper_still_delegates_correctly():
    print('Case: generate_invoice_authenticity_if_missing() still works unchanged (thin wrapper)')
    calls = []

    def fake_run_check(document_id, file_bytes, file_name, document_type,
                        document_consistency=None, extracted_vendor_name=None, use_cache=True):
        calls.append(document_type)
        return 503

    with _Patched(existing_row=None, run_check=fake_run_check):
        result = az.generate_invoice_authenticity_if_missing(68, b'fake-invoice-bytes', 'invoice68.pdf')

    check('returns the new check_id', result == 503, result)
    check('delegates with document_type=invoice', calls == ['invoice'], calls)


if __name__ == '__main__':
    run_case_po_missing_row_triggers_the_existing_engine_once()
    run_case_gr_missing_row_triggers_the_existing_engine_once()
    run_case_existing_row_skips_and_makes_zero_ai_calls()
    run_case_engine_returning_none_does_not_raise()
    run_case_engine_raising_an_exception_is_caught()
    run_case_invoice_wrapper_still_delegates_correctly()

    print(f'\n{"=" * 60}')
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
