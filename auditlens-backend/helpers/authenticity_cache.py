"""Document-hash-based cache for the AI authenticity engine's RAW
result — production. Same design as helpers/claude_cache.py (DB-backed,
not in-memory: Gunicorn workers don't share memory, and Render's free
tier wipes process memory on every redeploy/restart).

Kept in its own table (authenticity_result_cache), separate from
claude_extraction_cache/gemini_extraction_cache — this caches the
AUTHENTICITY engine's output (supplier identity / visual evidence /
integrity), a completely different call from field extraction.

What's cached is the RAW engine output (pre-normalization) plus which
engine produced it — NOT the final normalized/merged result. Schema
normalization (helpers/authenticity_check.py::_normalize_visual_result)
also folds in the currently-extracted vendor_name for the supplier
cross-check, which can change independently of the document image
itself (e.g. a later extraction correction) — recomputing that step
fresh on every cache hit means a vendor-name fix doesn't require
invalidating this cache.

Keyed by (file_hash, document_type, authenticity_version) —
`authenticity_version` (see helpers/claude_extractor.py::
CLAUDE_AUTHENTICITY_PROMPT_VERSION) intentionally invalidates every old
cache entry whenever the prompt/schema changes meaningfully, so a stale
cached shape from a previous prompt version is never served.

Only successful Claude/Gemini results are ever cached — the OCR-text-
only fallback (helpers/authenticity_check.py::_fallback_from_ocr_text)
is a transient-unavailability path, not something worth "remembering"
for a full document.
"""
import psycopg2.extras
from db import get_db_connection


def get_cached_authenticity_result(file_hash, document_type, authenticity_version):
    """Returns (engine, raw_result) for this exact file's bytes +
    document type + prompt version, or (None, None) if nothing is
    cached yet (or the lookup itself fails — a cache-read error must
    never block the authenticity check, it just means the engine gets
    called as if nothing were cached)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT engine, raw_result FROM authenticity_result_cache '
            'WHERE file_hash = %s AND document_type = %s AND authenticity_version = %s',
            (file_hash, document_type, authenticity_version)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0], row[1]  # psycopg2 decodes JSONB back into a dict automatically
    except Exception as e:
        print(f'WARNING: authenticity cache lookup failed: {type(e).__name__}: {e}')
    return None, None


def save_authenticity_result_to_cache(file_hash, document_type, authenticity_version, engine, raw_result):
    """Best-effort — a cache write failure must never break the
    authenticity check itself, it just means the next identical
    document calls the engine again."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO authenticity_result_cache
               (file_hash, document_type, authenticity_version, engine, raw_result)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (file_hash, document_type, authenticity_version)
               DO UPDATE SET engine = EXCLUDED.engine, raw_result = EXCLUDED.raw_result, created_at = NOW()''',
            (file_hash, document_type, authenticity_version, engine, psycopg2.extras.Json(raw_result))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'WARNING: authenticity cache write failed: {type(e).__name__}: {e}')
