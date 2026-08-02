"""Regression test for app.py::_ensure_document_review_steps_table() —
the exact startup migration whose absence in production caused
"relation document_review_steps does not exist" when routes/reviews.py
::mark_review_step tried to INSERT into it.

Uses the real local Postgres dev DB (same permitted convention as
tests/extraction/test_audit_review_steps_timeline.py) so the test
actually exercises CREATE TABLE IF NOT EXISTS against a real database
connection/pool, not a mock — a mock would never have caught this bug
in the first place (the fake-DB tests for the mark_review_step ROUTE
never call the migration function at all).

Usage:
    python tests/extraction/test_document_review_steps_migration.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
import io
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from db import get_db_connection

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def _skip_if_db_unavailable(fn):
    def wrapped():
        try:
            conn = get_db_connection()
            conn.close()
        except Exception as e:
            print(f'  SKIP {fn.__name__} (no DB available: {type(e).__name__})')
            return
        fn()
    return wrapped


def _table_exists():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('document_review_steps')")
    exists = cur.fetchone()[0] is not None
    conn.close()
    return exists


@_skip_if_db_unavailable
def run_case_migration_creates_the_table_from_scratch():
    print('Case: dropping the table (simulating a fresh/un-migrated Render Postgres) -> the migration recreates it')
    import app  # noqa: F401 - importing app.py itself is what would break if the migration regressed; also gives us the function under test

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS document_review_steps')
    conn.commit()
    conn.close()
    check('table dropped for the test', not _table_exists())

    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        app._ensure_document_review_steps_table()
    output = log.getvalue()

    check('table exists after running the migration', _table_exists())
    check('startup log confirms verified creation (not just "no exception raised")',
          'document_review_steps table ready (verified present)' in output, output)
    check('no ERROR/WARNING logged on a clean create', 'ERROR' not in output and 'WARNING' not in output, output)


@_skip_if_db_unavailable
def run_case_migration_is_idempotent_and_preserves_existing_rows():
    print('Case: running the migration again against an ALREADY-migrated table with real rows -> no error, no data loss')
    import app  # noqa: F401

    conn = get_db_connection()
    cur = conn.cursor()
    # A real row needs real document_id/user_id FKs - reuse the fixture
    # auditor account and INV-NBI-2026-0017 if present; skip gracefully
    # if this dev DB doesn't have that seeded fixture.
    cur.execute("SELECT user_id FROM users WHERE email = 'vision_fixture_auditor@x.com'")
    auditor = cur.fetchone()
    cur.execute("SELECT document_id FROM documents WHERE file_name = 'INV-NBI-2026-0017.pdf'")
    doc = cur.fetchone()
    conn.close()
    if not auditor or not doc:
        print('  SKIP (INV-NBI-2026-0017 fixture not present in this DB)')
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO document_review_steps (document_id, step, reviewed_by)
           VALUES (%s, 'three_way_matching', %s)
           ON CONFLICT (document_id, step) DO NOTHING''',
        (doc[0], auditor[0])
    )
    conn.commit()
    cur.execute('SELECT COUNT(*) FROM document_review_steps WHERE document_id = %s', (doc[0],))
    before_count = cur.fetchone()[0]
    conn.close()

    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        app._ensure_document_review_steps_table()
    output = log.getvalue()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM document_review_steps WHERE document_id = %s', (doc[0],))
    after_count = cur.fetchone()[0]
    cur.execute('DELETE FROM document_review_steps WHERE document_id = %s', (doc[0],))
    conn.commit()
    conn.close()

    check('re-running the migration raises no error', 'WARNING' not in output, output)
    check('the existing row survived the re-run (no data loss)', after_count == before_count and after_count >= 1,
          (before_count, after_count))


@_skip_if_db_unavailable
def run_case_migration_does_not_leak_the_connection_on_failure():
    print('Case: a migration failure (simulated) still returns its connection to the pool via conn.close() in finally')
    import app  # noqa: F401
    import db

    # Force the CREATE TABLE statement itself to fail without touching
    # real DB state, by monkey-patching get_db_connection() to hand back
    # a connection whose cursor() raises - proves the finally: conn.
    # close() path runs even when the try block never reaches its own
    # conn.close() call.
    class _ExplodingCursor:
        def execute(self, *a, **k):
            raise RuntimeError('simulated CREATE TABLE failure')

    class _FakeConn:
        def __init__(self):
            self.closed_called = False

        def cursor(self):
            return _ExplodingCursor()

        def commit(self):
            pass

        def close(self):
            self.closed_called = True

    fake_conn = _FakeConn()
    original = app.get_db_connection
    app.get_db_connection = lambda: fake_conn
    try:
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            app._ensure_document_review_steps_table()
        output = log.getvalue()
    finally:
        app.get_db_connection = original

    check('failure is logged as a WARNING (not a silent crash)', 'WARNING' in output, output)
    check('conn.close() was still called despite the failure (no pool leak)', fake_conn.closed_called)


if __name__ == '__main__':
    run_case_migration_creates_the_table_from_scratch()
    run_case_migration_is_idempotent_and_preserves_existing_rows()
    run_case_migration_does_not_leak_the_connection_on_failure()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
