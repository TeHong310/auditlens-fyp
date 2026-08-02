"""
v10 spec — Human-in-the-Loop evidence-location editing.

The auditor-facing counterpart to helpers/vision_evidence_boxes.py's
AI-only regions: this module owns the authenticity_evidence_corrections
table (see app.py's _ensure_authenticity_evidence_corrections_table for
the schema) — validation, upsert, delete, and lookup. Deliberately kept
separate from helpers/authenticity_check.py: a recheck REPLACES
authenticity_checks.boxes wholesale on every run, while a correction
saved here must survive every recheck untouched (see
run_authenticity_check's docstring — it never writes to this table).

Displayed-region priority is decided by the FRONTEND, not here: this
module always returns exactly what's stored (including
source='auditor_deleted' rows with null coordinates) — the frontend
merges a document's `boxes` (AI) with `evidence_corrections` (this
table) itself, correction winning when present.
"""

import psycopg2.extras
from psycopg2.extras import Json

from db import get_db_connection

# The SAME evidence-type vocabulary helpers/vision_evidence_boxes.py's
# build_vision_evidence_boxes() builds check.boxes[].type as, plus
# handwritten_notes (GR only) — a type with NO automatic detector at
# all (see vision_evidence_boxes.py's own docstring on why handwriting
# is never auto-located), existing solely so an auditor can manually
# flag one. Kept here (not imported from vision_evidence_boxes.py) since
# it is the single source of truth for what's a VALID auditor
# correction target, independent of what AI currently knows how to
# detect — the two lists are related but not required to be identical.
VALID_EVIDENCE_TYPES_BY_DOC_TYPE = {
    'invoice': {'supplier_name', 'buyer_name', 'buyer_received_stamp'},
    'po':      {'buyer_name', 'supplier_name', 'supplier_address'},
    'gr':      {'buyer_name', 'supplier_name', 'supplier_address',
                'qc_passed_stamp', 'key_in_store_stamp', 'handwritten_notes'},
}

_VALID_ACTIONS = ('correct', 'add', 'delete', 'reset')
_COORD_EPSILON = 1e-4


class EvidenceValidationError(ValueError):
    """Raised for any invalid change in a batch — the route layer turns
    this into a 400 with the message as-is, and (per the "prevent
    saving" requirement) NONE of the batch is applied when this is
    raised, not just the offending entry."""
    pass


def _validate_change(document_type, change):
    valid_types = VALID_EVIDENCE_TYPES_BY_DOC_TYPE.get(document_type)
    if valid_types is None:
        raise EvidenceValidationError(f'Invalid document_type: {document_type!r}')

    evidence_type = change.get('evidence_type')
    if evidence_type not in valid_types:
        raise EvidenceValidationError(
            f'{evidence_type!r} is not a valid evidence type for document_type {document_type!r}')

    action = change.get('action')
    if action not in _VALID_ACTIONS:
        raise EvidenceValidationError(f'Invalid action: {action!r}')

    if action in ('correct', 'add'):
        page = change.get('page', 1)
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise EvidenceValidationError('page must be a positive integer')

        for name in ('x', 'y', 'width', 'height'):
            value = change.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise EvidenceValidationError(f'{name} must be a number')

        x, y, width, height = change['x'], change['y'], change['width'], change['height']
        if width <= 0 or height <= 0:
            raise EvidenceValidationError('Box width and height must be greater than zero')
        if (x < -_COORD_EPSILON or y < -_COORD_EPSILON or
                x + width > 1 + _COORD_EPSILON or y + height > 1 + _COORD_EPSILON):
            raise EvidenceValidationError('Box coordinates must fall within the image (0-1 range)')

    return evidence_type, action


def _current_ai_box(cursor, document_id, document_type, evidence_type):
    """A snapshot-ready copy of the CURRENT AI box for this evidence
    type, or None if the AI never located one — read fresh on every
    correction (not cached) so original_ai_box always reflects the
    latest AI result at the moment the auditor corrected it."""
    cursor.execute(
        'SELECT boxes FROM authenticity_checks WHERE document_id = %s AND document_type = %s',
        (document_id, document_type)
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return None
    for box in row[0]:
        if box.get('type') == evidence_type:
            return box
    return None


def get_corrections_for(document_id, document_type):
    """Every saved correction/addition/deletion for this document+type —
    the raw stored rows, x/y/width/height explicitly cast to float (a
    NUMERIC column comes back from psycopg2 as Decimal, which the
    standard json encoder can't serialize)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute('''
        SELECT correction_id, evidence_type, page, x, y, width, height, source,
               original_ai_box, corrected_by, corrected_at
        FROM authenticity_evidence_corrections
        WHERE document_id = %s AND document_type = %s
        ORDER BY evidence_type
    ''', (document_id, document_type))
    rows = cursor.fetchall()
    conn.close()

    out = []
    for row in rows:
        entry = dict(row)
        for key in ('x', 'y', 'width', 'height'):
            entry[key] = float(entry[key]) if entry[key] is not None else None
        entry['corrected_at'] = entry['corrected_at'].isoformat() if entry['corrected_at'] else None
        out.append(entry)
    return out


def apply_evidence_changes(document_id, document_type, changes, user_id):
    """Validates the ENTIRE batch first (never applies a partial batch —
    one invalid change rejects all of them, matching the "prevent
    saving" validation requirement), then applies each in one
    transaction:
      - 'reset':   deletes the correction row entirely — reverts to
                   whatever authenticity_checks.boxes currently has.
      - 'delete':  upserts a source='auditor_deleted' row (null
                   coordinates) — an explicit "auditor removed this
                   location", distinct from no correction ever existing.
      - 'correct'/'add': upserts the new coordinates. The TRUE source is
                   decided here, not trusted from the caller: if an AI
                   box currently exists for this evidence_type it's
                   'auditor_corrected' (with a fresh snapshot saved to
                   original_ai_box), otherwise 'auditor_added'
                   (original_ai_box stays NULL).

    Raises EvidenceValidationError (nothing is written) if any change in
    the batch is invalid. Returns the fresh get_corrections_for() list.
    """
    if not changes:
        return get_corrections_for(document_id, document_type)

    validated = [(*_validate_change(document_type, c), c) for c in changes]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for evidence_type, action, change in validated:
            if action == 'reset':
                cursor.execute('''
                    DELETE FROM authenticity_evidence_corrections
                    WHERE document_id = %s AND document_type = %s AND evidence_type = %s
                ''', (document_id, document_type, evidence_type))
                continue

            if action == 'delete':
                cursor.execute('''
                    INSERT INTO authenticity_evidence_corrections
                        (document_id, document_type, evidence_type, page, x, y, width, height,
                         source, original_ai_box, corrected_by)
                    VALUES (%s, %s, %s, 1, NULL, NULL, NULL, NULL, 'auditor_deleted', NULL, %s)
                    ON CONFLICT (document_id, document_type, evidence_type) DO UPDATE SET
                        page = 1, x = NULL, y = NULL, width = NULL, height = NULL,
                        source = 'auditor_deleted',
                        corrected_by = EXCLUDED.corrected_by, corrected_at = NOW()
                ''', (document_id, document_type, evidence_type, user_id))
                continue

            # 'correct' or 'add'. The frontend's `action` — not a fresh
            # "does an AI box currently exist" lookup — decides the
            # stored source: it already knows the CURRENTLY DISPLAYED
            # region's full history (a plain AI box that still exists in
            # authenticity_checks.boxes is NOT the same thing as "this
            # evidence type's displayed region traces back to that AI
            # box" — once the auditor has explicitly deleted/replaced
            # it, a fresh draw for the same type is honestly
            # 'auditor_added', even though the old AI box is still
            # sitting untouched in that other column). original_ai_box
            # is still always snapshotted when one exists, purely as
            # informational provenance — it does not decide the source.
            ai_box = _current_ai_box(cursor, document_id, document_type, evidence_type)
            source = 'auditor_corrected' if action == 'correct' else 'auditor_added'
            cursor.execute('''
                INSERT INTO authenticity_evidence_corrections
                    (document_id, document_type, evidence_type, page, x, y, width, height,
                     source, original_ai_box, corrected_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id, document_type, evidence_type) DO UPDATE SET
                    page = EXCLUDED.page, x = EXCLUDED.x, y = EXCLUDED.y,
                    width = EXCLUDED.width, height = EXCLUDED.height,
                    source = EXCLUDED.source, original_ai_box = EXCLUDED.original_ai_box,
                    corrected_by = EXCLUDED.corrected_by, corrected_at = NOW()
            ''', (document_id, document_type, evidence_type, change.get('page', 1),
                  change['x'], change['y'], change['width'], change['height'],
                  source, Json(ai_box) if ai_box else None, user_id))

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return get_corrections_for(document_id, document_type)
