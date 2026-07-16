import json
import re
import base64
import requests
import fitz  # PyMuPDF
from config import Config

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{Config.GEMINI_MODEL}:generateContent"
GEMINI_MODELS_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_TIMEOUT = 15
GEMINI_VISION_TIMEOUT = 20
PDF_RENDER_ZOOM = 2.0  # ~144 DPI — enough detail for chop/logo/signature, keeps payload small

DOCUMENT_QUALITY_NOTE = """These documents may be:
- Scanned (CamScanner watermark visible)
- Handwritten annotations mixed with typed text
- Low quality with OCR errors like 'O'/'0', 'I'/'1', 'S'/'5' confusion

Guidelines:
- If OCR text seems garbled, still attempt extraction using context
- Return null (not empty string, not '-', not 'N/A') for truly missing fields
- Do NOT invent or guess values from unrelated text
- Prefer LABELED values over positional guessing
- For amounts, always return numeric (float), never string with commas"""

INVOICE_FULL_PROMPT = """You are an expert at extracting structured data from Malaysian SME
business documents AND detecting authenticity signals on them. You are looking
directly at the invoice IMAGE (not OCR text), so read the actual layout.

=== PART 1: FIELD EXTRACTION ===
""" + DOCUMENT_QUALITY_NOTE + """

IMPORTANT RULES:
- The VENDOR is the entity ISSUING the invoice (the seller), typically shown as the company name in the document header at the top
- The BUYER is who the invoice is billed TO ("Bill To", "Invoice To"), which is NOT the vendor
- INVOICE NUMBER: Look for the invoice number in these labels (case-insensitive), in priority order:
  1. "No.", "No :", "No. :", "No:", "Invoice No.", "Invoice No", "Invoice #", "Invoice Number", "Bill No.", "Doc No.", "Tax Invoice No."
  2. If the label is followed by ":" or wide whitespace, the value is what comes after
  3. Value may contain letters, digits, slashes "/", hyphens "-", dots "."
  4. Return the FULL value including all slashes and special chars
  5. Do NOT confuse invoice number with PO Number, Debtor/Customer Code, Contact Person, or a page number
- TOTAL AMOUNT: invoices list several amount lines — Subtotal, SST/GST/Tax, and the final Total.
  1. Return the amount on the line labeled "Total", "Grand Total", "Amount Due", or "Total (incl ...)" —
     this is the FINAL amount the customer must pay, after tax.
  2. NEVER return the "Subtotal"/"Sub Total" line — that is the pre-tax amount, not the total.
  3. NEVER return the SST/GST/tax line itself as the total — that is tax_amount, a separate field.
  4. The total is arithmetically the LARGEST of the amount lines (Total = Subtotal + tax). If unsure
     which line is which, the largest clearly-labeled amount is the total.
- TAX AMOUNT: the SST/GST/service tax amount (e.g. the "SST 6%" or "SST 8%" line), not the percentage itself.
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD

=== PART 2: AUTHENTICITY SIGNALS ===
Detect the following signals AND identify how this document was captured/uploaded.

Signal definitions:
- has_company_chop: Round/square colored physical stamp (e.g. "IQC PASSED",
  "RECEIVED", company chop with red/blue ink). NOT a printed logo.
- has_company_logo: Distinct graphic/visual company logo (icon, stylized mark).
  NOT just text.
- has_company_name: Company's registered name printed clearly, usually in header.
  Typed text counts.
- has_signature: Handwritten signature (cursive strokes, ink pen marks).
  NOT a typed name or printed name.

Upload source definitions:
- phone_photo: Handheld phone photo — visible perspective distortion, uneven
  lighting, shadows, possibly angled or slightly blurred edges
- scanned: Uniform lighting, straight edges, may have CamScanner/scanner
  watermark visible, cleaner than phone photo
- digital_native: Perfectly clean text and lines, no image compression
  artifacts, appears to be direct PDF export from software (SAP, Word, etc.)
- webcam: Low resolution, front-lit, static composition

Be strict — only mark a signal true if clearly visible.

signal_boxes rules:
- Only include a key in signal_boxes for a signal that is true above. If a
  signal is false, omit its key from signal_boxes entirely.
- Each box is [ymin, xmin, ymax, xmax], normalized to a 0-1000 scale relative
  to the full image (top-left is [0,0], bottom-right is [1000,1000]).
- If has_company_chop or has_company_logo is true, the box should tightly
  bound that specific mark (compact box).
- If has_company_name or has_signature is true, the box should bound that
  specific text/mark.

Return ONLY valid JSON, no markdown, no explanation, no code fences. Return
this exact JSON structure:
{
  "invoice_number": "string or null",
  "vendor_name": "string or null (the SELLER shown in the header)",
  "invoice_date": "YYYY-MM-DD or null",
  "total_amount": number or null,
  "tax_amount": number or null,
  "currency": "string or null (e.g. RM, MYR, USD)",
  "has_company_chop": false,
  "has_company_logo": false,
  "has_company_name": false,
  "has_signature": false,
  "upload_source": "phone_photo",
  "notes": "<one short sentence>",
  "signal_boxes": {
    "has_company_chop": [0, 0, 0, 0],
    "has_company_logo": [0, 0, 0, 0],
    "has_company_name": [0, 0, 0, 0],
    "has_signature": [0, 0, 0, 0]
  }
}"""

PO_PROMPT = """You are an expert at extracting structured data from Malaysian SME business documents.
""" + DOCUMENT_QUALITY_NOTE + """

Below is OCR-extracted text from a PURCHASE ORDER (PO) document. Extract the fields listed.

IMPORTANT RULES:
- The VENDOR is the SUPPLIER the PO is issued TO (look for labels like "Bill To Vendor", "Vendor:", "Supplier:", "To:")
- The company shown in the header of a PO is usually the BUYER issuing the order, NOT the vendor
- CRITICAL: PO Number rules:
  1. PO Number is found in a LABELED field, never inferred from other text.
  2. Look for these exact labels: "Doc No.", "PO No.", "P.O. No.", "Purchase Order No.", "Order No.", "PO Number", "Reference No."
  3. The value must be adjacent to (right of or below) the label.
  4. Do NOT extract any substring from:
     - Company names (e.g. "Polymer", "Solutions", "Industries")
     - Product descriptions
     - Address fields
  5. Common Malaysian SME PO number formats:
     - PONNNNNNN (e.g. PO3005713)
     - PO-YYYY-NNNN
     - Numeric-only (e.g. 3005713)
  6. If no clearly labeled PO number field exists, return null. Do NOT guess or extract from unrelated text.
  7. Length is typically 6-12 characters. Reject candidates shorter than 5.
- Total Amount priority (return the FIRST match found):
  1. "Total Payable Incl. Tax (RM)" — highest priority (this is the final amount)
  2. "Grand Total"
  3. "Total (RM)"
  4. "Amount Payable"
  5. "Net Total"
  6. "Total Excl. Tax (RM)" — use only if no tax-inclusive total exists
  Value format:
  - May contain commas: "82,850.00" -> return as number 82850.00
  - May have "RM" or "MYR" prefix — strip it
  - Reject values that appear in item-line "Sub Total" columns
  - Reject "Discount" amounts (they're not the total)
  If the document has both "Total Excl. Tax" and "Total Payable Incl. Tax", ALWAYS return "Total Payable Incl. Tax".
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD
- Return ONLY valid JSON, no markdown, no explanation, no code fences

OCR TEXT:
---
{ocr_text}
---

Return this exact JSON structure:
{{
  "po_number": "string or null",
  "vendor_name": "string or null (the SUPPLIER the PO is issued to)",
  "po_date": "YYYY-MM-DD or null",
  "total_amount": number or null
}}"""

GR_PROMPT = """You are an expert at extracting structured data from Malaysian SME business documents.
""" + DOCUMENT_QUALITY_NOTE + """

Below is OCR-extracted text from a GOODS RECEIPT (GR) document. Extract the fields listed.

IMPORTANT RULES:
- The VENDOR is who DELIVERED the goods (look for "Received From", "Delivered by", "Supplier")
- The company shown in the header is usually the RECEIVING company (the buyer's warehouse), NOT the vendor
- GR Number labels (priority order):
  1. "Doc No." (most common on Malaysian GRN)
  2. "GRN No.", "GR No.", "Goods Receipt No."
  3. "Receipt No.", "Ref No."
  Common formats:
  - PDNNNNNNN (e.g. PD6011652)
  - GRN-YYYY-NNNN
  - Numeric-only
  Do NOT confuse with:
  - PO Number (usually labeled "From Doc No." or "PO Ref")
  - Supplier Ref No. (that's the supplier's invoice reference)
  - Item Code / Part Number
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD
- Return ONLY valid JSON, no markdown, no explanation, no code fences

OCR TEXT:
---
{ocr_text}
---

Return this exact JSON structure:
{{
  "gr_number": "string or null",
  "vendor_name": "string or null (who DELIVERED the goods)",
  "receipt_date": "YYYY-MM-DD or null"
}}"""


def _strip_markdown_fences(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def gemini_key_suffix():
    """Last 4 chars of the configured Gemini key, for log lines only —
    lets two log lines be compared to confirm they used the same key."""
    key = Config.GEMINI_API_KEY
    return key[-4:] if key and len(key) >= 4 else '????'


def log_gemini_request(url, context=''):
    """
    Logs repr(model) and the EXACT request URL right before every
    generateContent call, so a malformed model string (stray "models/"
    prefix, trailing whitespace/newline) is immediately visible in
    production logs instead of only surfacing as a mysterious 404. The
    key is sent via the x-goog-api-key header, never in the URL, so the
    URL is always safe to log in full — no redaction needed.
    """
    label = f" ({context})" if context else ''
    print(f"DEBUG Gemini request{label}: model={Config.GEMINI_MODEL!r} "
          f"url={url!r} key=...{gemini_key_suffix()}")


def log_available_gemini_models():
    """
    Safety net for a 404 from generateContent (wrong/unavailable model name
    for this API key). Makes ONE call to the ListModels endpoint and logs
    which model IDs this key can actually use — no retry, no loop, just
    a diagnostic log line.
    """
    try:
        response = requests.get(
            GEMINI_MODELS_LIST_URL,
            params={"key": Config.GEMINI_API_KEY},
            timeout=10
        )
        response.raise_for_status()
        models = response.json().get('models', [])
        usable = [
            m.get('name') for m in models
            if 'generateContent' in m.get('supportedGenerationMethods', [])
        ]
        print(f"DEBUG available Gemini models: {usable}")
    except Exception as e:
        print(f"DEBUG ListModels call failed: {type(e).__name__}: {e}")


def _call_gemini(template, ocr_text):
    if not Config.GEMINI_API_KEY:
        print("DEBUG Gemini: GEMINI_API_KEY not set, skipping")
        return {}

    try:
        prompt = template.format(ocr_text=ocr_text)
        # Key goes in a header, never the URL, so it can't leak into
        # exception messages, proxy logs, or redirect chains.
        headers = {
            'Content-Type': 'application/json',
            'x-goog-api-key': Config.GEMINI_API_KEY
        }
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json"
            }
        }
        log_gemini_request(GEMINI_URL, context='text extraction')
        response = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=GEMINI_TIMEOUT)
        if response.status_code == 404:
            print(f"DEBUG Gemini call error: 404 Not Found for model '{Config.GEMINI_MODEL}'")
            log_available_gemini_models()
        response.raise_for_status()
        result = response.json()

        text = result['candidates'][0]['content']['parts'][0]['text']
        text = _strip_markdown_fences(text)
        return json.loads(text)

    except Exception as e:
        print(f"DEBUG Gemini call error: {type(e).__name__}: {e}")
        return {}


def prepare_gemini_image_payload(file_path):
    """
    Returns (mime_type, base64_data) for a Gemini inline_data part.

    PDFs are rendered to their FIRST PAGE as a PNG image (PyMuPDF/fitz —
    no system dependency, unlike pdf2image+poppler) rather than sent as
    raw PDF bytes: sending a PDF's raw bytes doesn't reliably produce
    visual-signal detection or [ymin,xmin,ymax,xmax] bounding boxes for
    chop/logo/signature, since that's a rasterized-page-image task, not
    a document-text task. Image files are sent through unchanged.
    """
    ext = file_path.lower().rsplit('.', 1)[-1]
    if ext == 'pdf':
        doc = fitz.open(file_path)
        try:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(PDF_RENDER_ZOOM, PDF_RENDER_ZOOM))
            png_bytes = pix.tobytes('png')
        finally:
            doc.close()
        return 'image/png', base64.b64encode(png_bytes).decode('utf-8')

    with open(file_path, 'rb') as f:
        data = base64.b64encode(f.read()).decode('utf-8')
    mime = 'image/png' if ext == 'png' else 'image/jpeg'
    return mime, data


def gemini_extract_invoice_full(file_path):
    """
    Single merged Gemini vision call for an invoice: extracted fields AND
    authenticity signals in one request/response, so an invoice upload only
    ever spends one Gemini call (avoids the free-tier per-minute limit that
    two separate calls — field extraction + authenticity — used to hit).
    Returns the parsed dict, or None if the call fails for any reason
    (429, timeout, network, bad JSON, PDF render failure) or GEMINI_API_KEY
    is unset.
    """
    if not Config.GEMINI_API_KEY:
        print("DEBUG Gemini: GEMINI_API_KEY not set, skipping merged invoice call")
        return None

    try:
        mime_type, data = prepare_gemini_image_payload(file_path)

        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": data}},
                    {"text": INVOICE_FULL_PROMPT}
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
        log_gemini_request(GEMINI_URL, context='merged invoice extraction+authenticity')
        response = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=GEMINI_VISION_TIMEOUT)
        if response.status_code == 404:
            print(f"DEBUG Gemini merged invoice call error: 404 Not Found for model '{Config.GEMINI_MODEL}'")
            log_available_gemini_models()
        response.raise_for_status()
        text = response.json()['candidates'][0]['content']['parts'][0]['text']
        result = json.loads(_strip_markdown_fences(text))
        print(f"DEBUG Gemini merged invoice+authenticity result: {result}")
        return result
    except Exception as e:
        print(f"DEBUG Gemini merged invoice call error: {type(e).__name__}: {e}")
        return None


def gemini_extract_po(ocr_text):
    result = _call_gemini(PO_PROMPT, ocr_text)
    print(f"DEBUG Gemini extracted po: {result}")
    return result


def gemini_extract_gr(ocr_text):
    result = _call_gemini(GR_PROMPT, ocr_text)
    print(f"DEBUG Gemini extracted gr: {result}")
    return result
