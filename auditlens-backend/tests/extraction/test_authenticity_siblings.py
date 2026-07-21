"""Regression tests for routes/authenticity.py's sibling-check trigger
(_ensure_sibling_checks) — the actual fix for "only Invoice shows up on
the Authenticity page": visiting one document_type's authenticity check
now opportunistically checks the other uploaded types for the same
document_id too, instead of only ever checking whatever document_type
was explicitly requested. No real DB, no real AI calls —
get_db_connection/_lookup_file_info/_document_consistency_for/
run_authenticity_check are all monkey-patched with fakes/stubs.

Usage:
    python tests/extraction/test_authenticity_siblings.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import routes.authenticity as ra

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class _FakeCursor:
    """A fresh instance of this is handed out by every fake
    get_db_connection() call — matches _ensure_sibling_checks opening a
    new connection per sibling type it examines."""

    def __init__(self, existing_types):
        self.existing_types = existing_types
        self.doc_type = None

    def execute(self, sql, params=None):
        # 'SELECT 1 FROM authenticity_checks WHERE document_id = %s AND document_type = %s'
        if params and len(params) > 1:
            self.doc_type = params[1]

    def fetchone(self):
        return (1,) if self.doc_type in self.existing_types else None


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor

    def close(self):
        pass


class _Patched:
    def __init__(self, existing_types, files_present, consistency_fails=False):
        """
        existing_types: set of document_type strings that already have an
          authenticity_checks row (should be SKIPPED, never re-checked).
        files_present: set of document_type strings that have an
          uploaded file at all (_lookup_file_info returns something).
        """
        self.existing_types = existing_types
        self.files_present = files_present
        self.consistency_fails = consistency_fails
        self.run_calls = []
        self._originals = {}

    def __enter__(self):
        self._originals = {
            'get_db_connection':         ra.get_db_connection,
            '_lookup_file_info':         ra._lookup_file_info,
            '_document_consistency_for': ra._document_consistency_for,
            'run_authenticity_check':    ra.run_authenticity_check,
        }

        existing_types = self.existing_types
        files_present = self.files_present

        ra.get_db_connection = lambda: _FakeConn(_FakeCursor(existing_types))

        def fake_lookup_file_info(cursor, document_id, doc_type):
            if doc_type not in files_present:
                return None
            return {'file_bytes': b'fake-bytes', 'file_name': 'test.pdf'}
        ra._lookup_file_info = fake_lookup_file_info

        def fake_document_consistency_for(cursor, document_id):
            return None if self.consistency_fails else {'vendor_match': True}
        ra._document_consistency_for = fake_document_consistency_for

        def fake_run_authenticity_check(document_id, file_bytes, file_name, doc_type, document_consistency=None):
            self.run_calls.append(doc_type)
            return 123
        ra.run_authenticity_check = fake_run_authenticity_check

        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(ra, name, value)


def run_case_missing_sibling_gets_checked():
    print('Case: PO has no row yet and a file exists -> gets checked')
    with _Patched(existing_types=set(), files_present={'po'}) as p:
        ra._ensure_sibling_checks(1, primary_type='invoice')
    check('po was checked', 'po' in p.run_calls, p.run_calls)
    check('gr was NOT checked (no file uploaded)', 'gr' not in p.run_calls, p.run_calls)
    check('primary type (invoice) never re-checked as a sibling', 'invoice' not in p.run_calls, p.run_calls)


def run_case_existing_sibling_not_rechecked():
    print('Case: PO already has a row -> not re-checked (idempotent)')
    with _Patched(existing_types={'po'}, files_present={'po', 'gr'}) as p:
        ra._ensure_sibling_checks(1, primary_type='invoice')
    check('po (already checked) skipped', 'po' not in p.run_calls, p.run_calls)
    check('gr (missing, file present) checked', 'gr' in p.run_calls, p.run_calls)


def run_case_no_file_no_check():
    print('Case: sibling type has no uploaded file at all -> never checked')
    with _Patched(existing_types=set(), files_present=set()) as p:
        ra._ensure_sibling_checks(1, primary_type='invoice')
    check('nothing checked when no PO/GR files exist', p.run_calls == [], p.run_calls)


def run_case_all_three_types_checked_independently():
    print('Case: uploading Invoice+PO+GR then viewing any one -> all three end up checked')
    with _Patched(existing_types=set(), files_present={'po', 'gr'}) as p:
        ra._ensure_sibling_checks(1, primary_type='invoice')
    check('both siblings (po, gr) checked in one call', sorted(p.run_calls) == ['gr', 'po'], p.run_calls)


def run_case_sibling_failure_does_not_raise():
    print('Case: a sibling check that raises internally does not propagate')
    with _Patched(existing_types=set(), files_present={'po'}) as p:
        def boom(*a, **k):
            raise RuntimeError('simulated Claude/Gemini failure')
        ra.run_authenticity_check = boom
        try:
            ra._ensure_sibling_checks(1, primary_type='invoice')
            check('no exception propagated', True)
        except Exception as e:
            check('no exception propagated', False, f'{type(e).__name__}: {e}')


if __name__ == '__main__':
    run_case_missing_sibling_gets_checked()
    run_case_existing_sibling_not_rechecked()
    run_case_no_file_no_check()
    run_case_all_three_types_checked_independently()
    run_case_sibling_failure_does_not_raise()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
