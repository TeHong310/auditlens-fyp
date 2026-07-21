import re
import os
import json
from config import Config
from db import get_db_connection
from helpers.gemini_extractor import (
    call_gemini_sdk, prepare_gemini_image_payload, GEMINI_VISION_TIMEOUT_MS,
    GeminiRateLimitError,
)
from helpers.claude_extractor import analyze_document_authenticity
from helpers.auth_rules import AUTH_RULES, _normalize_doc_type

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

    Raises GeminiRateLimitError (instead of returning None) if the call
    is still rate-limited after call_gemini_sdk's built-in retries — lets
    run_authenticity_check tell "temporarily rate-limited" apart from a
    permanent failure, so the OCR-text fallback's notes can say so.
    """
    try:
        image = prepare_gemini_image_payload(file_bytes, file_name)
        text = call_gemini_sdk(
            AUTHENTICITY_PROMPT, image=image, context='authenticity',
            timeout_ms=GEMINI_VISION_TIMEOUT_MS,
            on_rate_limit='raise',
        )
        if text is None:
            return None
        return json.loads(_strip_markdown_fences(text))
    except GeminiRateLimitError:
        raise
    except Exception as e:
        print(f"DEBUG Authenticity Gemini error: {type(e).__name__}: {e}")
        return None


COMPANY_SUFFIX_RE = re.compile(
    r'(?:sdn\.?\s*bhd\.?|berhad|enterprise|trading|corporation|corp\.?|ltd\.?)',
    re.IGNORECASE
)


def _fallback_from_ocr_text(ocr_text, rate_limited=False):
    """Non-Gemini fallback used when Gemini vision is unavailable/fails.
    Only has_company_name can be inferred from plain OCR text; chop/logo/
    signature are visual marks we have no positional data for here, so they
    default to False rather than guessing. signal_boxes stays empty since
    OCR text alone carries no coordinates.

    rate_limited: the failure was specifically a 429 that persisted even
      after call_gemini_sdk's automatic retries — a TEMPORARY condition
      (the free-tier per-minute limit clears on its own within a minute
      or two), not a permanent one, so the notes text says so and points
      at "Re-check" instead of implying Gemini is unavailable.
    """
    text = ocr_text or ''
    has_name = bool(COMPANY_SUFFIX_RE.search(text))
    if rate_limited:
        notes = ('Automated fallback: Gemini hit the free-tier rate limit and stayed '
                  'rate-limited after automatic retries — this is temporary, not a '
                  'permanent failure. Checked OCR text only for now; visual signals '
                  '(chop/logo/signature) could not be verified. Use "Re-check" in a '
                  'minute or two once the rate limit has cleared.')
    else:
        notes = ('Automated fallback (Gemini unavailable): checked OCR text only. '
                  'Visual signals (chop/logo/signature) could not be verified.')
    return {
        'has_company_chop': False,
        'has_company_logo': False,
        'has_company_name': has_name,
        'has_signature': False,
        'upload_source': None,
        'notes': notes,
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


# Display labels for the new named bounding-box list (spec section 6/7) —
# also the frontend's 5-color legend keys (Blue=logo, Green=name,
# Purple=address, Red=stamp, Orange=signature).
_BOX_LABELS = {
    'company_logo':     'Supplier Logo',
    'company_name':     'Company Name',
    'supplier_address':  'Supplier Address',
    'stamp':             'Stamp/Chop',
    'signature':          'Signature',
}


def _stamp_required(document_type):
    """Whether a stamp/chop is normally expected for this document type —
    derived from helpers/auth_rules.py's AUTH_RULES (required/important
    tiers) instead of asking the model to guess, since that config
    already encodes exactly this per-doc-type domain knowledge (e.g. a
    Goods Receipt's chop matters far more than a PO's)."""
    doc_type = _normalize_doc_type(document_type)
    rules = AUTH_RULES.get(doc_type)
    if not rules:
        return False
    return 'company_chop' in rules.get('required', []) or 'company_chop' in rules.get('important', [])


def _normalize_visual_result(engine, raw):
    """Maps either engine's raw output onto ONE unified schema (the
    section-1 JSON shape) so DB storage and the frontend never need to
    branch on which engine produced the result.

    engine == 'claude': raw is already close to the target shape —
      still defensively defaulted in case a sub-object is missing.
    engine in ('gemini', 'fallback'): raw is the OLD 4-signal schema
      (has_company_chop/logo/name/signature + signal_boxes) — mapped
      into the same shape with tampering/address/contact left
      "not assessed" (neither older path evaluates those)."""
    raw = raw or {}

    if engine == 'claude':
        supplier = raw.get('supplier_identity') or {}
        evidence_raw = raw.get('document_visual_evidence') or {}
        integrity = raw.get('integrity_check') or {}
        overall = raw.get('overall_result') or {}

        evidence = {}
        for key in _BOX_LABELS:
            item = evidence_raw.get(key) or {}
            box = item.get('boxes')
            entry = {
                'detected':   bool(item.get('detected', False)),
                'confidence': item.get('confidence') or 0,
                'boxes':      box if isinstance(box, list) and len(box) == 4 else None,
            }
            if key == 'stamp':
                entry['type'] = item.get('type') or ''
            evidence[key] = entry

        return {
            'supplier_identity': {
                'supplier_name_detected': bool(supplier.get('supplier_name_detected', False)),
                'supplier_name':          supplier.get('supplier_name'),
                'logo_detected':          bool(supplier.get('logo_detected', False)),
                'address_detected':       bool(supplier.get('address_detected', False)),
                'contact_block_detected': bool(supplier.get('contact_block_detected', False)),
            },
            'document_visual_evidence': evidence,
            'integrity_check': {
                'suspicious_edit':    bool(integrity.get('suspicious_edit', False)),
                'inconsistent_font':  bool(integrity.get('inconsistent_font', False)),
                'abnormal_alignment': bool(integrity.get('abnormal_alignment', False)),
                'suspicious_overlay': bool(integrity.get('suspicious_overlay', False)),
                'confidence':         integrity.get('confidence') or 0,
            },
            'overall_result': {
                'status':     overall.get('status') or 'REVIEW',
                'risk_level': overall.get('risk_level') or 'LOW',
                'reasons':    overall.get('reasons') or [],
            },
        }

    # 'gemini' (old AUTHENTICITY_PROMPT fallback) or 'fallback' (OCR-text-only)
    old_boxes = raw.get('signal_boxes') or {}

    def _box(old_key):
        box = old_boxes.get(old_key)
        return box if isinstance(box, list) and len(box) == 4 else None

    name = bool(raw.get('has_company_name', False))
    logo = bool(raw.get('has_company_logo', False))
    chop = bool(raw.get('has_company_chop', False))
    sig = bool(raw.get('has_signature', False))

    return {
        'supplier_identity': {
            'supplier_name_detected': name,
            'supplier_name':          None,
            'logo_detected':          logo,
            'address_detected':       False,
            'contact_block_detected': False,
        },
        'document_visual_evidence': {
            'company_logo':     {'detected': logo, 'confidence': 70 if logo else 0, 'boxes': _box('has_company_logo')},
            'company_name':     {'detected': name, 'confidence': 70 if name else 0, 'boxes': _box('has_company_name')},
            'supplier_address': {'detected': False, 'confidence': 0, 'boxes': None},
            'stamp':             {'detected': chop, 'type': '', 'confidence': 70 if chop else 0, 'boxes': _box('has_company_chop')},
            'signature':          {'detected': sig, 'confidence': 70 if sig else 0, 'boxes': _box('has_signature')},
        },
        'integrity_check': {
            'suspicious_edit': False, 'inconsistent_font': False,
            'abnormal_alignment': False, 'suspicious_overlay': False,
            'confidence': 0,
        },
        'overall_result': {
            'status':     'PASS' if name else 'REVIEW',
            'risk_level': 'LOW',
            'reasons':    [raw['notes']] if raw.get('notes') else [],
        },
    }


def _flatten_boxes(evidence):
    """Converts the unified schema's per-category boxes into the flat
    named list the frontend overlay draws (spec section 6):
    [{"name": "...", "x": ..., "y": ..., "width": ..., "height": ...}].
    Box coordinates are the same 0-1000-normalized scale already used by
    the legacy signal_boxes column — only the shape (corner pair vs.
    x/y/width/height) differs, so no new coordinate system is introduced."""
    boxes = []
    for key, label in _BOX_LABELS.items():
        box = (evidence.get(key) or {}).get('boxes')
        if not (isinstance(box, list) and len(box) == 4):
            continue
        ymin, xmin, ymax, xmax = box
        boxes.append({'name': label, 'x': xmin, 'y': ymin, 'width': xmax - xmin, 'height': ymax - ymin})
    return boxes


def _authenticity_is_complete(result):
    """Fallback-worthiness check for Claude's authenticity result —
    mirrors ai_extractor_router.py's _completeness_check but for this
    schema: a None/empty result, or one missing the core visual-evidence
    object entirely (malformed response), is not usable."""
    if not result:
        return False
    return isinstance(result.get('document_visual_evidence'), dict) and bool(result.get('document_visual_evidence'))


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


def run_authenticity_check(document_id, file_bytes, file_name, document_type, document_consistency=None):
    """
    Main entry — Claude Vision primary, Gemini fallback, OCR-text-only as
    the last-resort safety net. NEVER raises — pipeline safe. Called
    on-demand only (GET /authenticity/<id> on first view, or
    POST /authenticity/<id>/recheck) — NOT automatically at upload time
    (see routes/documents.py, which no longer calls this).

    document_type: 'invoice' | 'po' | 'gr' (required)
    file_bytes/file_name: the raw bytes of the uploaded document (from
      DB, not a disk path) and its original filename (used to detect
      PDF vs image) — Render's disk is ephemeral, so bytes are always
      the source of truth here.
    document_consistency: the already-computed {vendor_match, po_match,
      item_match, amount_match, overall_status} dict from
      routes/auditor.py's _build_comparison() — this function only
      stores it, it does not compute cross-document consistency itself
      (Claude is not asked to reason across Invoice/PO/GR).

    Returns check_id on success, None on failure (e.g. document doesn't exist).
    """
    try:
        save_rendered_authenticity_image(document_id, document_type, file_bytes, file_name)
        image = prepare_gemini_image_payload(file_bytes, file_name)

        claude_result = analyze_document_authenticity(image, document_type)
        if _authenticity_is_complete(claude_result):
            engine = 'claude'
            raw_result = claude_result
            print("DEBUG Authenticity: engine=claude")
        else:
            rate_limited = False
            gemini_result = None
            try:
                gemini_result = _call_gemini_vision(file_bytes, file_name)
            except GeminiRateLimitError:
                rate_limited = True
            if gemini_result:
                engine = 'gemini'
                raw_result = gemini_result
                print("DEBUG Authenticity: engine=gemini (Claude fallback)")
            else:
                engine = 'fallback'
                raw_result = _fallback_from_ocr_text(None, rate_limited=rate_limited)
                print("DEBUG Authenticity: Claude and Gemini both unavailable, using OCR-text fallback"
                      + (" (rate-limited)" if rate_limited else ""))

        visual = _normalize_visual_result(engine, raw_result)
        evidence = visual['document_visual_evidence']
        evidence['stamp']['stamp_required'] = _stamp_required(document_type)

        chop = evidence['stamp']['detected']
        logo = evidence['company_logo']['detected']
        name = evidence['company_name']['detected']
        sig = evidence['signature']['detected']
        risk_level = visual['overall_result'].get('risk_level') or 'LOW'
        notes = '; '.join(visual['overall_result'].get('reasons') or []) or None

        status = _compute_authenticity_status(document_type, {
            'has_company_name': name, 'has_company_chop': chop, 'has_signature': sig,
        })
        boxes = _flatten_boxes(evidence)

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO authenticity_checks
                (document_id, has_company_chop, has_company_logo, has_company_name,
                 has_signature, document_type, authenticity_status, ai_notes,
                 ai_engine_used, ai_visual_result, document_consistency, risk_level, boxes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id, document_type) DO UPDATE SET
                    has_company_chop = EXCLUDED.has_company_chop,
                    has_company_logo = EXCLUDED.has_company_logo,
                    has_company_name = EXCLUDED.has_company_name,
                    has_signature = EXCLUDED.has_signature,
                    authenticity_status = EXCLUDED.authenticity_status,
                    ai_notes = EXCLUDED.ai_notes,
                    ai_engine_used = EXCLUDED.ai_engine_used,
                    ai_visual_result = EXCLUDED.ai_visual_result,
                    document_consistency = EXCLUDED.document_consistency,
                    risk_level = EXCLUDED.risk_level,
                    boxes = EXCLUDED.boxes,
                    created_at = NOW()
                RETURNING check_id
            ''', (document_id, chop, logo, name, sig, document_type, status, notes,
                  engine, json.dumps(visual),
                  json.dumps(document_consistency) if document_consistency else None,
                  risk_level, json.dumps(boxes)))
            check_id = cursor.fetchone()[0]
            conn.commit()
            print(f"DEBUG AUTH AI RESULT\n"
                  f"vendor={visual['supplier_identity'].get('supplier_name')}\n"
                  f"logo={logo}\nstamp={chop}\nsignature={sig}\ntampering={risk_level}")
            print(f"DEBUG Authenticity: saved row for doc={document_id} type={document_type} "
                  f"status={status} engine={engine} risk={risk_level} boxes={len(boxes)}")
            return check_id
        finally:
            conn.close()
    except Exception as e:
        print(f"DEBUG Authenticity error for doc {document_id}: {type(e).__name__}: {e}")
        return None
