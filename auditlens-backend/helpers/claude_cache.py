"""Document-hash-based cache for Claude extraction results — production.

Same design as helpers/gemini_cache.py (DB-backed, not in-memory: this
app runs under Gunicorn — workers don't share memory — on Render's free
tier, where process memory is wiped on every redeploy/restart, so only a
persisted cache reliably prevents a second Claude API call for the same
document). Kept in a SEPARATE table from gemini_extraction_cache (not
touched by this addition) rather than a shared one, to avoid any risk to
the existing, already-working Gemini cache — the extra `provider` column
here exists for parity with the requested cache key shape
(file_hash + document_type + provider) even though, today, every row in
this table has provider='claude'.
"""
import psycopg2.extras
from db import get_db_connection
from helpers.claude_extractor import compute_file_hash  # noqa: F401 (re-exported for convenience)

PROVIDER = 'claude'


def get_cached_claude_result(file_hash, document_type):
    """Returns the cached Claude result dict for this exact file's bytes
    + document type, or None if nothing is cached yet (or the lookup
    itself fails — a cache-read error must never block extraction, it
    just means Claude gets called as if nothing were cached)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT claude_result FROM claude_extraction_cache '
            'WHERE file_hash = %s AND document_type = %s AND provider = %s',
            (file_hash, document_type, PROVIDER)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]  # psycopg2 decodes JSONB back into a dict automatically
    except Exception as e:
        print(f'WARNING: Claude cache lookup failed: {type(e).__name__}: {e}')
    return None


def save_claude_result_to_cache(file_hash, document_type, claude_result):
    """Best-effort — a cache write failure must never break the upload
    itself, it just means the next identical upload calls Claude again."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO claude_extraction_cache (file_hash, document_type, provider, claude_result)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (file_hash, document_type, provider) DO UPDATE SET claude_result = EXCLUDED.claude_result''',
            (file_hash, document_type, PROVIDER, psycopg2.extras.Json(claude_result))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'WARNING: Claude cache write failed: {type(e).__name__}: {e}')
