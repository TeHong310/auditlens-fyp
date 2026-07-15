import re
import json
import base64
import requests
from config import Config
from db import get_db_connection

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
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


def _mime_type(file_path):
    ext = file_path.lower().rsplit('.', 1)[-1]
    if ext == 'pdf':
        return 'application/pdf'
    if ext == 'png':
        return 'image/png'
    return 'image/jpeg'


def _strip_markdown_fences(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _call_gemini_vision(file_path):
    """Call Gemini vision with the document. Returns parsed JSON dict or None."""
    if not Config.GEMINI_API_KEY:
        print("DEBUG Authenticity: GEMINI_API_KEY not set, skipping")
        return None

    try:
        with open(file_path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('utf-8')

        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": _mime_type(file_path), "data": data}},
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
        response = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=GEMINI_TIMEOUT)
        response.raise_for_status()
        text = response.json()['candidates'][0]['content']['parts'][0]['text']
        return json.loads(_strip_markdown_fences(text))
    except Exception as e:
        print(f"DEBUG Authenticity Gemini error: {type(e).__name__}: {e}")
        return None


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


def run_authenticity_check(document_id, file_path, document_type):
    """
    Main entry. NEVER raises — pipeline safe.
    document_type: 'invoice' | 'po' | 'gr' (from upload endpoint, required)
    Returns check_id on success, None on failure.
    """
    try:
        result = _call_gemini_vision(file_path)
        if not result:
            return None

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
            return check_id
        finally:
            conn.close()
    except Exception as e:
        print(f"DEBUG Authenticity error for doc {document_id}: {type(e).__name__}: {e}")
        return None
