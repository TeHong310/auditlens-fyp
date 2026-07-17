import re
import os
import json
from config import Config
from db import get_db_connection
from helpers.gemini_extractor import call_gemini_sdk, prepare_gemini_image_payload, GEMINI_VISION_TIMEOUT_MS

# Where a PDF's rendered first page (the same image sent to Gemini vision)
# gets saved so the frontend can display it and draw the overlay markers —
# PDFs can't be shown directly in an <img> tag. Deterministic filename
# (document_id_document_type.png), overwritten on every re-check, so no DB
# column is needed to look it up later — see GET /authenticity/<id>/image.
AUTHENTICITY_IMAGE_DIR = os.path.join(Config.UPLOAD_FOLDER, 'authenticity')

AUTHENTICITY_PROMPT = """You are analyzing a business document from a Malaysian SME.

Detect the following signals AND identify how this document was captured/uploaded.

Return ONLY JSON, no markdown fences:
{
  "has_company_chop": <bool>,
  "has_company_logo": <bool>,
  "has_company_name": <bool>,
  "has_signature": <bool>,
  "upload_source": "phone_photo" | "scanned" | "digital_native" | "webcam",
  "notes": "<one short sentence>",
  "signal_boxes": {
    "has_company_chop": [ymin, xmin, ymax, xmax],
    "has_company_logo": [ymin, xmin, ymax, xmax],
    "has_company_name": [ymin, xmin, ymax, xmax],
    "has_signature": [ymin, xmin, ymax, xmax]
  }
}

Signal definitions:
- has_company_chop: Round/square colored physical stamp (e.g. "IQC PASSED",
  "RECEIVED", company chop with red/blue ink). NOT a printed logo.
- has_company_logo: Distinct graphic/visual company logo (icon, stylized mark).
  NOT just text.
- has_company_name: Company's registered name printed clearly, usually in header.
  Typed text counts.
- has_signature: Handwritten signature (cursive strokes, ink pen marks).
  NOT a typed name or printed name.

signal_boxes rules:
- If a signal is true, include its key in signal_boxes with a tight
  bounding box around that specific mark/text.
- If a signal is false, you MAY still include its key with a box if you
  can identify a specific, plausible location for it — e.g. a blank
  signature line, an empty area where a company chop/logo would
  typically appear on this type of document. This helps the auditor see
  exactly where to look. If there's no sensible specific location to
  point at, omit the key entirely (do not include it with a null or
  empty value).
- Each box is [ymin, xmin, ymax, xmax], normalized to a 0-1000 scale relative
  to the full image (top-left is [0,0], bottom-right is [1000,1000]) —
  standard Gemini bounding box format.
- If has_company_chop or has_company_logo is present, the box should
  tightly bound that specific mark (compact box); if absent, bound the
  empty area where it would go.
- If has_company_name or has_signature is present, the box should bound
  that specific text/mark (can be wider for text fields, but stay tight
  to the actual characters, not the whole document); if absent, bound
  the blank line/space where it would go.

Upload source definitions:
- phone_photo: Handheld phone photo — visible perspective distortion, uneven
  lighting, shadows, possibly angled or slightly blurred edges
- scanned: Uniform lighting, straight edges, may have CamScanner/scanner
  watermark visible, cleaner than phone photo
- digital_native: Perfectly clean text and lines, no image compression
  artifacts, appears to be direct PDF export from software (SAP, Word, etc.)
- webcam: Low resolution, front-lit, static composition

Be strict — only mark signal true if clearly visible."""


def _strip_markdown_fences(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _call_gemini_vision(file_bytes, file_name):
    """
    Call Gemini vision with the document via the google-genai SDK (not raw
    HTTP — see call_gemini_sdk in gemini_extractor.py for why). PDFs are
    rendered to their first page as an image first (see
    prepare_gemini_image_payload) so chop/logo/signature and their
    bounding boxes can actually be detected — raw PDF bytes don't
    reliably produce that. Takes raw bytes (from DB, not a file path) so
    it has no dependency on the local filesystem. Returns parsed JSON
    dict or None.
    """
    try:
        image = prepare_gemini_image_payload(file_bytes, file_name)
        text = call_gemini_sdk(
            AUTHENTICITY_PROMPT, image=image, context='authenticity',
            timeout_ms=GEMINI_VISION_TIMEOUT_MS,
        )
        if text is None:
            return None
        return json.loads(_strip_markdown_fences(text))
    except Exception as e:
        print(f"DEBUG Authenticity Gemini error: {type(e).__name__}: {e}")
        return None


COMPANY_SUFFIX_RE = re.compile(
    r'(?:sdn\.?\s*bhd\.?|berhad|enterprise|trading|corporation|corp\.?|ltd\.?)',
    re.IGNORECASE
)


def _fallback_from_ocr_text(ocr_text):
    """Non-Gemini fallback used when Gemini vision is unavailable/fails.
    Only has_company_name can be inferred from plain OCR text; chop/logo/
    signature are visual marks we have no positional data for here, so they
    default to False rather than guessing. signal_boxes stays empty since
    OCR text alone carries no coordinates.
    """
    text = ocr_text or ''
    has_name = bool(COMPANY_SUFFIX_RE.search(text))
    return {
        'has_company_chop': False,
        'has_company_logo': False,
        'has_company_name': has_name,
        'has_signature': False,
        'upload_source': None,
        'notes': ('Automated fallback (Gemini unavailable): checked OCR text only. '
                  'Visual signals (chop/logo/signature) could not be verified.'),
        'signal_boxes': {},
    }


def _compute_authenticity_status(document_type, signals):
    """
    Returns 'passed' or 'warning' based on document type rules.

    Invoice: needs company_name AND (chop OR signature)
    PO/GR:   needs company_name only
    Unknown doc type: always passes (soft gate, defensive default)
    """
    has_name = signals.get('has_company_name', False)
    has_chop = signals.get('has_company_chop', False)
    has_sig = signals.get('has_signature', False)

    doc_type = (document_type or '').lower()

    if doc_type == 'invoice':
        passed = has_name and (has_chop or has_sig)
    elif doc_type in ('po', 'gr', 'grn'):
        passed = has_name
    else:
        # Unknown doc type — default to passing (soft gate)
        passed = True

    return 'passed' if passed else 'warning'


def save_rendered_authenticity_image(document_id, document_type, file_bytes, file_name):
    """
    If file_name is a PDF, render its first page from file_bytes (the
    same rendering prepare_gemini_image_payload does for the Gemini
    vision call) and cache it to local disk. Image uploads (jpg/png) are
    served from their original bytes directly instead — see
    GET /authenticity/<id>/image — so nothing is cached for those.

    Takes raw bytes (from DB) rather than a file path — this cache
    directory is itself on Render's ephemeral disk, so it's just a
    same-process speedup, not the source of truth; it's always
    rebuildable from the DB-stored bytes after a restart.

    Never raises — a render/save failure here must not break the
    authenticity row write. Runs unconditionally (regardless of whether
    Gemini itself succeeds) since the rendered page is useful for display
    even when Gemini fails and the fallback heuristic is used.
    """
    if not file_name.lower().endswith('.pdf'):
        return
    try:
        mime_type, image_bytes = prepare_gemini_image_payload(file_bytes, file_name)
        os.makedirs(AUTHENTICITY_IMAGE_DIR, exist_ok=True)
        out_path = os.path.join(AUTHENTICITY_IMAGE_DIR, f'{document_id}_{document_type}.png')
        with open(out_path, 'wb') as f:
            f.write(image_bytes)
        print(f"DEBUG Authenticity: saved rendered PDF page image to {out_path}")
    except Exception as e:
        print(f"DEBUG Authenticity: failed to save rendered PDF image: {type(e).__name__}: {e}")


def run_authenticity_check(document_id, file_bytes, file_name, document_type, ocr_text=None,
                            precomputed_result=None, skip_gemini=False):
    """
    Main entry. NEVER raises — pipeline safe.
    document_type: 'invoice' | 'po' | 'gr' (from upload endpoint, required)
    file_bytes/file_name: the raw bytes of the uploaded document (from
      DB, not a disk path) and its original filename (used to detect
      PDF vs image) — Render's disk is ephemeral, so bytes are always
      the source of truth here.
    Always writes/updates an authenticity_checks row, using Gemini vision
    when available and falling back to OCR-text heuristics when it isn't
    (429/timeout/network/parse failure) — Gemini is best-effort, not a
    hard dependency.

    precomputed_result: authenticity signals already obtained from a Gemini
      vision call made elsewhere (e.g. the merged invoice extraction +
      authenticity call) — skips calling Gemini again here.
    skip_gemini: the caller already tried a Gemini vision call for this
      document and it failed — go straight to the OCR-text fallback
      instead of retrying Gemini (a retry would likely just hit the same
      429/timeout again and spend a call for nothing).

    Returns check_id on success, None on failure (e.g. document doesn't exist).
    """
    try:
        save_rendered_authenticity_image(document_id, document_type, file_bytes, file_name)

        if precomputed_result:
            result = precomputed_result
            engine = 'gemini'
            print("DEBUG Authenticity: engine=gemini (merged call)")
        else:
            result = None if skip_gemini else _call_gemini_vision(file_bytes, file_name)
            if result:
                engine = 'gemini'
                print("DEBUG Authenticity: engine=gemini")
            else:
                engine = 'fallback'
                print("DEBUG Authenticity: gemini failed (no result — see error above, "
                      "or GEMINI_API_KEY unset), using fallback")
                result = _fallback_from_ocr_text(ocr_text)

        chop = bool(result.get('has_company_chop', False))
        logo = bool(result.get('has_company_logo', False))
        name = bool(result.get('has_company_name', False))
        sig = bool(result.get('has_signature', False))
        upload_source = result.get('upload_source')
        notes = result.get('notes', '')

        status = _compute_authenticity_status(document_type, result)

        # Keep any well-formed [ymin,xmin,ymax,xmax] Gemini returned, for
        # a PRESENT signal or a MISSING one — a missing signal can still
        # have a plausible location (e.g. a blank signature line, an
        # empty area where a chop would go), and the frontend now draws
        # those as a red marker (vs. green for present) so the auditor
        # can see exactly where to look. Only requirement is a
        # well-formed box; presence/absence is decided separately by the
        # has_company_chop/logo/name/signature booleans above.
        raw_boxes = result.get('signal_boxes') or {}
        signal_box_keys = ('has_company_chop', 'has_company_logo', 'has_company_name', 'has_signature')
        signal_boxes = {
            key: raw_boxes[key]
            for key in signal_box_keys
            if isinstance(raw_boxes.get(key), list) and len(raw_boxes[key]) == 4
        }

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO authenticity_checks
                (document_id, has_company_chop, has_company_logo, has_company_name,
                 has_signature, document_type, upload_source, authenticity_status,
                 ai_notes, signal_boxes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id, document_type) DO UPDATE SET
                    has_company_chop = EXCLUDED.has_company_chop,
                    has_company_logo = EXCLUDED.has_company_logo,
                    has_company_name = EXCLUDED.has_company_name,
                    has_signature = EXCLUDED.has_signature,
                    upload_source = EXCLUDED.upload_source,
                    authenticity_status = EXCLUDED.authenticity_status,
                    ai_notes = EXCLUDED.ai_notes,
                    signal_boxes = EXCLUDED.signal_boxes,
                    created_at = NOW()
                RETURNING check_id
            ''', (document_id, chop, logo, name, sig,
                  document_type, upload_source, status, notes, json.dumps(signal_boxes)))
            check_id = cursor.fetchone()[0]
            conn.commit()
            print(f"DEBUG Authenticity: doc={document_id} type={document_type} "
                  f"chop={chop} sig={sig} logo={logo} name={name} "
                  f"source={upload_source} status={status} boxes={list(signal_boxes.keys())}")
            print(f"DEBUG Authenticity: saved row for doc={document_id} "
                  f"status={status} engine={engine}")
            return check_id
        finally:
            conn.close()
    except Exception as e:
        print(f"DEBUG Authenticity error for doc {document_id}: {type(e).__name__}: {e}")
        return None
