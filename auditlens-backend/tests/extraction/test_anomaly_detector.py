"""Regression tests for the Anomaly Detection page overview additions:
helpers/anomaly_detector.py::_log_detection_run (records every
detection run, including clean ones, so "Last Analysed" doesn't depend
on an anomaly actually being found) and routes/anomalies.py::
_run_full_anomaly_analysis (the "Run Anomaly Analysis" button's
backend: re-runs the EXISTING, untouched run_anomaly_detection() across
every invoice). No real DB, no real AI calls — get_db_connection and
run_anomaly_detection are monkey-patched with fakes/stubs.

Usage:
    python tests/extraction/test_anomaly_detector.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import helpers.anomaly_detector as ad
import routes.anomalies as ra

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


# ── _log_detection_run ──

class _FakeCursorLog:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None


class _FakeConnLog:
    def __init__(self, cursor, raise_on_commit=False):
        self._cursor = cursor
        self.raise_on_commit = raise_on_commit
        self.committed = False
        self.closed = False

    def cursor(self, **kwargs):
        return self._cursor

    def commit(self):
        if self.raise_on_commit:
            raise RuntimeError('simulated DB failure')
        self.committed = True

    def close(self):
        self.closed = True


def run_case_log_detection_run_inserts_a_row():
    print('Case: _log_detection_run inserts one row with the given document_id/count')
    cursor = _FakeCursorLog()
    conn = _FakeConnLog(cursor)
    original = ad.get_db_connection
    ad.get_db_connection = lambda: conn
    try:
        ad._log_detection_run(42, 3)
    finally:
        ad.get_db_connection = original
    check('exactly one INSERT executed', len(cursor.executed) == 1, cursor.executed)
    sql, params = cursor.executed[0]
    check('INSERT targets anomaly_detection_runs', 'anomaly_detection_runs' in sql, sql)
    check('params are (document_id, anomalies_found)', params == (42, 3), params)
    check('transaction committed', conn.committed)


def run_case_log_detection_run_swallows_db_errors():
    print('Case: a DB failure while logging never raises (pipeline-safe)')
    cursor = _FakeCursorLog()
    conn = _FakeConnLog(cursor, raise_on_commit=True)
    original = ad.get_db_connection
    ad.get_db_connection = lambda: conn
    try:
        try:
            ad._log_detection_run(42, 0)
            check('no exception propagated', True)
        except Exception as e:
            check('no exception propagated', False, f'{type(e).__name__}: {e}')
    finally:
        ad.get_db_connection = original


# ── _run_full_anomaly_analysis ──

class _FakeCursorIds:
    """Returns document_id rows for the initial DISTINCT query."""
    def __init__(self, document_ids):
        self.document_ids = document_ids

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return [{'document_id': d} for d in self.document_ids]


class _FakeCursorDelete:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append(('DELETE', params[0] if params else None, ' '.join(sql.split())))


class _FakeConnSimple:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


class _PatchedRun:
    """Patches routes.anomalies.get_db_connection to hand out a
    DISTINCT-ids cursor first, then a fresh delete-cursor on every
    subsequent call (matching _run_full_anomaly_analysis opening a new
    connection per document for the delete step) — and patches
    run_anomaly_detection to a fake with configurable per-document
    results (list of created ids, or an exception to raise)."""

    def __init__(self, document_ids, detection_results):
        self.document_ids = document_ids
        self.detection_results = detection_results  # {document_id: [ids] or Exception}
        self.delete_log = []
        self.detection_calls = []
        self._call_count = 0
        self._originals = {}

    def __enter__(self):
        self._originals = {
            'get_db_connection': ra.get_db_connection,
            'run_anomaly_detection': ra.run_anomaly_detection,
        }

        def fake_get_db_connection():
            self._call_count += 1
            if self._call_count == 1:
                return _FakeConnSimple(_FakeCursorIds(self.document_ids))
            return _FakeConnSimple(_FakeCursorDelete(self.delete_log))
        ra.get_db_connection = fake_get_db_connection

        def fake_run_anomaly_detection(document_id):
            self.detection_calls.append(document_id)
            result = self.detection_results.get(document_id, [])
            if isinstance(result, Exception):
                raise result
            return result
        ra.run_anomaly_detection = fake_run_anomaly_detection

        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(ra, name, value)


def run_case_loops_every_invoice_and_deletes_before_redetect():
    print('Case: every invoice is deleted-then-redetected exactly once')
    with _PatchedRun([1, 2, 3], {1: [10], 2: [], 3: [11, 12]}) as p:
        result = ra._run_full_anomaly_analysis()
    check('run_anomaly_detection called once per document, in order',
          p.detection_calls == [1, 2, 3], p.detection_calls)
    check('DELETE issued for every document before its redetect',
          [d for _, d, _sql in p.delete_log] == [1, 2, 3], p.delete_log)
    check('documents_analyzed reflects the real count', result['documents_analyzed'] == 3, result)
    check('anomalies_found aggregates across all documents (1+0+2=3)',
          result['anomalies_found'] == 3, result)
    check('errors is 0 when nothing raises', result['errors'] == 0, result)


def run_case_delete_only_clears_pending_anomalies_preserving_review_history():
    print('Case: the pre-redetect DELETE only targets pending anomalies — reviewed/dismissed rows (audit history) are never wiped by a re-run')
    with _PatchedRun([1], {1: []}) as p:
        ra._run_full_anomaly_analysis()
    check('exactly one DELETE issued', len(p.delete_log) == 1, p.delete_log)
    _, doc_id, sql = p.delete_log[0]
    check('DELETE targets the right document', doc_id == 1, p.delete_log)
    check("DELETE is scoped to status = 'pending' — reviewed/dismissed anomalies survive a re-run",
          "status = 'pending'" in sql, sql)


def run_case_one_document_failure_does_not_abort_the_batch():
    print('Case: one document raising does not stop the others from being analysed')
    with _PatchedRun([1, 2, 3], {1: [10], 2: RuntimeError('boom'), 3: [11]}) as p:
        result = ra._run_full_anomaly_analysis()
    check('all three documents still attempted', p.detection_calls == [1, 2, 3], p.detection_calls)
    check('errors counts exactly the one failure', result['errors'] == 1, result)
    check('anomalies_found only counts the successful documents (1+1=2)',
          result['anomalies_found'] == 2, result)
    check('documents_analyzed still reflects the full set', result['documents_analyzed'] == 3, result)


def run_case_no_invoices_is_a_clean_noop():
    print('Case: no invoices with extracted fields yet -> zero everything, no crash')
    with _PatchedRun([], {}) as p:
        result = ra._run_full_anomaly_analysis()
    check('no detection calls made', p.detection_calls == [], p.detection_calls)
    check('documents_analyzed is 0', result['documents_analyzed'] == 0, result)
    check('anomalies_found is 0', result['anomalies_found'] == 0, result)
    check('errors is 0', result['errors'] == 0, result)


if __name__ == '__main__':
    run_case_log_detection_run_inserts_a_row()
    run_case_log_detection_run_swallows_db_errors()
    run_case_loops_every_invoice_and_deletes_before_redetect()
    run_case_delete_only_clears_pending_anomalies_preserving_review_history()
    run_case_one_document_failure_does_not_abort_the_batch()
    run_case_no_invoices_is_a_clean_noop()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
