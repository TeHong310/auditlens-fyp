"""Claude Vision extraction — TEST MODE ONLY.

NOT wired into the production upload pipeline (routes/documents.py still
calls Gemini only, unchanged). This module exists so Claude extraction
quality can be evaluated manually, on demand, via scripts/
test_claude_extraction.py, without spending API credits on every normal
upload or during automated test runs.

Image rendering (PDF -> PNG, or pass-through for jpg/png) is NOT
duplicated here — prepare_gemini_image_payload() from gemini_extractor.py
is reused as-is, so this module has no PDF-handling logic of its own to
keep in sync with the real one.
"""
import base64
import json
import re
import hashlib
import os
from anthropic import Anthropic
from config import Config

CLAUDE_TIMEOUT_S = 60

_client = None


def _get_claude_client():
    """Lazily-built, shared Anthropic client — mirrors gemini_extractor.
    py's _get_client() pattern. Returns None (never raises) if
    ANTHROPIC_API_KEY isn't set, so callers can check-and-skip instead of
    handling an exception."""
    if not Config.ANTHROPIC_API_KEY:
        return None
    global _client
    if _client is None:
        _client = Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _client


def _strip_markdown_fences(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


_DOCUMENT_TYPE_LABELS = {
    'invoice': 'an INVOICE (a bill from a supplier to a buyer)',
    'po':      'a PURCHASE ORDER (a buyer\'s order raised against a supplier)',
    'gr':      'a GOODS RECEIPT (confirmation that ordered goods were received)',
}

CLAUDE_SYSTEM_PROMPT = """You are an enterprise AP (Accounts Payable) automation AI analyzing a
procurement document IMAGE visually. You are NOT performing OCR — you
are understanding document layout, structure, and business context
before extracting anything.

VENDOR:
- Identify the SUPPLIER issuing this document (who is billing/selling) —
  NEVER the Bill To / Ship To / Buyer / Customer / Purchaser company.
- Use the letterhead, logo area, and supplier address block to identify
  the true vendor entity.
- Correct obvious OCR/scan spelling noise in the company name using
  context — e.g. if "COLCRAFT", "COILCRAFT", "COILCRAF" all appear to
  refer to the same entity on this document, resolve to the one real,
  most plausible full name rather than returning the noisiest variant
  verbatim.

AMOUNT:
Priority order for total_amount (use the first that applies):
1. Grand Total
2. Total Amount
3. Invoice Total
4. Amount Due
Ignore: subtotal, tax-only values, unit prices — none of these are the total.
Never assume a currency — read the actual symbol/code printed next to the amount.

LINE ITEMS:
- Read the complete item table visually, row by row.
- Never summarize multiple rows into one entry.
- Never return only the first row when more exist — if the table has 5
  rows, line_items must have 5 entries.
- Extract EVERY row as its own line_items entry, in the order printed.
- Preserve for each row: description, part_number, quantity, amount.
- part_number is the PRIMARY key used for cross-document (Invoice/PO/GR)
  matching — never omit it when it is visible on the document. If the
  same code also functions as an item/SKU code, also fill item_code with
  the same value.

Return null (not empty string, not "N/A") for any field you cannot
confidently extract — never guess.

Return ONLY valid JSON, no markdown, no code fences, no explanation —
exactly this structure:
{
  "invoice_number": "string or null",
  "vendor_name": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "po_number": "string or null",
  "po_reference": "string or null",
  "receipt_date": "YYYY-MM-DD or null",
  "total_amount": number or null,
  "tax_amount": number or null,
  "currency": "string or null",
  "line_items": [
    {
      "description": "string",
      "part_number": "string or null",
      "item_code": "string or null",
      "quantity": number or null,
      "unit_price": number or null,
      "amount": number or null
    }
  ]
}"""


def extract_with_claude_test(image, document_type):
    """TEST-MODE Claude Vision extraction. Makes ONE real Anthropic API
    call — only ever invoked explicitly (scripts/test_claude_extraction.
    py), never automatically.

    image: (mime_type, raw_bytes) tuple — same shape gemini_extractor.
      py's prepare_gemini_image_payload() returns; reuse that function to
      build this argument rather than re-rendering a PDF here.
    document_type: 'invoice' | 'po' | 'gr'.

    Returns the parsed dict on success, or None if the call fails for
    any reason (no API key, network, timeout, bad JSON) — same
    fail-soft contract as gemini_extract_*_full().
    """
    client = _get_claude_client()
    if client is None:
        print("DEBUG CLAUDE REQUEST | skipped: ANTHROPIC_API_KEY not set")
        return None

    mime_type, image_bytes = image
    doc_label = _DOCUMENT_TYPE_LABELS.get(document_type, document_type)
    user_text = f"This document is {doc_label}. Extract the fields per the schema in your instructions."

    print(f"DEBUG CLAUDE REQUEST | model={Config.CLAUDE_MODEL!r} | document_type={document_type!r} | "
          f"mime={mime_type} | image_size_kb={len(image_bytes) / 1024:.1f}")

    try:
        response = client.messages.create(
            model=Config.CLAUDE_MODEL,
            max_tokens=4096,
            temperature=0,
            system=CLAUDE_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode('utf-8'),
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }],
            timeout=CLAUDE_TIMEOUT_S,
        )
    except Exception as e:
        print(f"DEBUG CLAUDE REQUEST error: {type(e).__name__}: {e}")
        return None

    text = "".join(block.text for block in response.content if getattr(block, 'type', None) == 'text')
    _raw_preview = text if len(text) <= 3000 else text[:3000] + '...<truncated>'
    print(f"DEBUG CLAUDE RESPONSE | document_type={document_type} | text={_raw_preview!r}")

    try:
        result = json.loads(_strip_markdown_fences(text))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"DEBUG CLAUDE RESPONSE parse error: {type(e).__name__}: {e}")
        return None

    return result


# ============================================================
# TEST-ONLY local cache — prevents re-spending a real API call when the
# manual test script is run again against the same file. File-based
# (not a DB table): this is a developer-run script, single process, no
# Gunicorn workers to share state across, so a Postgres-backed cache
# (like helpers/gemini_cache.py's, built for the production multi-worker
# case) would be unnecessary schema surface for a test-only tool. NEVER
# imported by routes/documents.py or any production code path.
# ============================================================
_TEST_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', '.claude_test_cache')


def compute_file_hash(file_bytes):
    return hashlib.sha256(file_bytes).hexdigest()


def _test_cache_path(file_hash, document_type):
    return os.path.join(_TEST_CACHE_DIR, f'{file_hash}_{document_type}.json')


def get_cached_test_result(file_hash, document_type):
    path = _test_cache_path(file_hash, document_type)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_test_result_to_cache(file_hash, document_type, result):
    os.makedirs(_TEST_CACHE_DIR, exist_ok=True)
    path = _test_cache_path(file_hash, document_type)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2)
    except OSError as e:
        print(f"WARNING: could not write Claude test cache: {type(e).__name__}: {e}")
