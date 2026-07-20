"""Document-hash-based cache for Gemini extraction results.

Enforces "at most one Gemini extraction call per document" (routes/
documents.py's three upload endpoints) across retries/re-uploads of the
exact same file bytes. DB-backed rather than in-memory: this app runs
under Gunicorn (multiple worker processes don't share memory) on
Render's free tier (process memory is wiped on every redeploy/restart),
so an in-memory dict would not reliably prevent a second Gemini call for
the same document — only a persisted cache does.
"""
import hashlib
import psycopg2.extras
from db import get_db_connection


def compute_file_hash(file_bytes):
    return hashlib.sha256(file_bytes).hexdigest()


def get_cached_gemini_result(file_hash, document_type):
    """Returns the cached Gemini result dict for this exact file's bytes
    + document type, or None if nothing is cached yet (or the lookup
    itself fails — a cache-read error must never block extraction, it
    just means Gemini gets called as if nothing were cached)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT gemini_result FROM gemini_extraction_cache WHERE file_hash = %s AND document_type = %s',
            (file_hash, document_type)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]  # psycopg2 decodes JSONB back into a dict automatically
    except Exception as e:
        print(f'WARNING: Gemini cache lookup failed: {type(e).__name__}: {e}')
    return None


def save_gemini_result_to_cache(file_hash, document_type, gemini_result):
    """Best-effort — a cache write failure must never break the upload
    itself, it just means the next identical upload calls Gemini again."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO gemini_extraction_cache (file_hash, document_type, gemini_result)
               VALUES (%s, %s, %s)
               ON CONFLICT (file_hash, document_type) DO UPDATE SET gemini_result = EXCLUDED.gemini_result''',
            (file_hash, document_type, psycopg2.extras.Json(gemini_result))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'WARNING: Gemini cache write failed: {type(e).__name__}: {e}')
