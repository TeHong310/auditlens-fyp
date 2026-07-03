import re
import json
import base64
import requests
from config import Config
from db import get_db_connection

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
GEMINI_TIMEOUT = 20

AUTHENTICITY_PROMPT = """You are analyzing a business document from a Malaysian SME (invoice, purchase order, or goods received note).

Detect these 3 visual authenticity signals. Be strict — only mark true if clearly visible.

Return ONLY JSON, no markdown fences:
{
  "has_company_chop": <bool>,
  "has_company_logo": <bool>,
  "has_company_name": <bool>,
  "notes": "<one short sentence describing what you see>"
}

Definitions:
- has_company_chop: A round, square, or oval colored stamp/seal with company name or department label (e.g. "IQC PASSED", "RECEIVED", company chop). NOT just a printed logo.
- has_company_logo: A distinct visual/graphic company logo (an image, icon, or stylized mark). NOT just text.
- has_company_name: The company's full registered name printed clearly (usually in header). Typed text counts.
"""


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


def run_authenticity_check(document_id, file_path):
    """
    Main entry. NEVER raises — pipeline safe.
    Returns check_id on success, None on failure.
    """
    try:
        result = _call_gemini_vision(file_path)
        if not result:
            return None

        chop = bool(result.get('has_company_chop', False))
        logo = bool(result.get('has_company_logo', False))
        name = bool(result.get('has_company_name', False))
        notes = result.get('notes', '')

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO authenticity_checks
                (document_id, has_company_chop, has_company_logo, has_company_name, ai_notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (document_id) DO UPDATE SET
                    has_company_chop = EXCLUDED.has_company_chop,
                    has_company_logo = EXCLUDED.has_company_logo,
                    has_company_name = EXCLUDED.has_company_name,
                    ai_notes = EXCLUDED.ai_notes,
                    created_at = NOW()
                RETURNING check_id
            ''', (document_id, chop, logo, name, notes))
            check_id = cursor.fetchone()[0]
            conn.commit()
            print(f"DEBUG Authenticity: doc={document_id} chop={chop} logo={logo} name={name}")
            return check_id
        finally:
            conn.close()
    except Exception as e:
        print(f"DEBUG Authenticity error for doc {document_id}: {type(e).__name__}: {e}")
        return None
