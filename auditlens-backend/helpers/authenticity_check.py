import re
import json
import requests
from config import Config
from db import get_db_connection
from helpers.gemini_extractor import (
    log_available_gemini_models, log_gemini_request, prepare_gemini_image_payload
)

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{Config.GEMINI_MODEL}:generateContent"
GEMINI_TIMEOUT = 20

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
- Only include a key in signal_boxes for a signal that is true above. If a
  signal is false, omit its key from signal_boxes entirely (do not include
  it with a null or empty value).
- Each box is [ymin, xmin, ymax, xmax], normalized to a 0-1000 scale relative
  to the full image (top-left is [0,0], bottom-right is [1000,1000]) —
  standard Gemini bounding box format.
- If has_company_chop or has_company_logo is true, the box should tightly
  bound that specific mark (compact box).
- If has_company_name or has_signature is true, the box should bound that
  specific text/mark (can be wider for text fields, but stay tight to the
  actual characters, not the whole document).

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


def _call_gemini_vision(file_path):
    """
    Call Gemini vision with the document. PDFs are rendered to their first
    page as an image first (see prepare_gemini_image_payload in
    gemini_extractor.py) so chop/logo/signature and their bounding boxes
    can actually be detected — raw PDF bytes don't reliably produce that.
    Returns parsed JSON dict or None.
    """
    if not Config.GEMINI_API_KEY:
        print("DEBUG Authenticity: GEMINI_API_KEY not set, skipping")
        return None

    try:
        mime_type, data = prepare_gemini_image_payload(file_path)

        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": data}},
                    {"text": AUTHENTICITY_PROMPT}
                ]
            }],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json"
            }
        }
        headers = {
            'Content-Type': 'application/json',
            'x-goog-api-key': Config.GEMINI_API_KEY
        }
        log_gemini_request(GEMINI_URL, context='authenticity')
        response = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=GEMINI_TIMEOUT)
        if response.status_code == 404:
            print(f"DEBUG Authenticity Gemini error: 404 Not Found for model '{Config.GEMINI_MODEL}'")
            log_available_gemini_models()
        response.raise_for_status()
        text = response.json()['candidates'][0]['content']['parts'][0]['text']
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


def run_authenticity_check(document_id, file_path, document_type, ocr_text=None,
                            precomputed_result=None, skip_gemini=False):
    """
    Main entry. NEVER raises — pipeline safe.
    document_type: 'invoice' | 'po' | 'gr' (from upload endpoint, required)
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
        if precomputed_result:
            result = precomputed_result
            engine = 'gemini'
            print("DEBUG Authenticity: engine=gemini (merged call)")
        else:
            result = None if skip_gemini else _call_gemini_vision(file_path)
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

        # Only keep a box for a signal we've actually parsed as present,
        # and only if Gemini gave a well-formed [ymin,xmin,ymax,xmax] —
        # keeps signal_boxes' keys consistent with the boolean flags
        # regardless of what Gemini actually returned.
        raw_boxes = result.get('signal_boxes') or {}
        present_signals = {
            'has_company_chop': chop,
            'has_company_logo': logo,
            'has_company_name': name,
            'has_signature': sig,
        }
        signal_boxes = {
            key: raw_boxes[key]
            for key, is_present in present_signals.items()
            if is_present and isinstance(raw_boxes.get(key), list) and len(raw_boxes[key]) == 4
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
