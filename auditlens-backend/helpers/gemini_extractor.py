import json
import re
import requests
import fitz  # PyMuPDF
from google import genai
from google.genai import types, errors
from config import Config

# ListModels is still a plain REST call — confirmed working even with
# this account's "AQ."-prefixed key (only generateContent rejects it over
# raw HTTP), so there's no need to route it through the SDK too.
GEMINI_MODELS_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_TIMEOUT_MS = 15_000
GEMINI_VISION_TIMEOUT_MS = 20_000
PDF_RENDER_ZOOM = 2.0  # ~144 DPI — enough detail for chop/logo/signature, keeps payload small

_client = None


def _get_client():
    """
    Lazily-built, shared google.genai.Client — the single client used by
    every Gemini call in the codebase (field extraction, authenticity,
    anomaly explanation), constructed once per process from
    Config.GEMINI_API_KEY.

    Required because this account's Gemini key uses Google's newer "AQ."
    prefix format. Those keys are rejected (404/401) by raw HTTP calls to
    generativelanguage.googleapis.com's generateContent endpoint — even
    though the key is valid and the model is confirmed available via
    ListModels — but work correctly through the official SDK, which
    handles "AQ." keys' auth internally. This is why every generateContent
    call in this codebase goes through the SDK, not requests/urllib.
    """
    global _client
    if _client is None:
        _client = genai.Client(api_key=Config.GEMINI_API_KEY)
    return _client

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

DOCUMENT_NUMBER_NOTE = """
- Document numbers are not always under a formal label like "Invoice No.": real
  documents often use a bare label immediately followed by the value, e.g.
  "INVOICE:  IX107587" or "Doc No.  PD6011823" — treat any short label
  ending in ":" or immediately followed by whitespace-then-value as a valid
  label if it names the document type (invoice/doc/ref/no).
- Ignore unrelated numbers near the document number that look similar,
  e.g. "Co.Reg.No" (company registration number), "Tel", "Fax", a page
  number, or a customer/account code — these are NEVER the document number
  even if they appear close by."""

CURRENCY_NOTE = """
- CURRENCY & MULTI-CURRENCY TOTALS: identify the currency of the primary total
  (e.g. RM, MYR, USD, US$) and return it in the "currency" field.
  Some documents show BOTH an original-currency total AND a converted
  local-currency total via an exchange rate, e.g. "TOTAL (US$) 8,020.00"
  together with "EXCHANGE RATE=1.2670 ... TOTAL= 10,161.34 (RM)", or a PO
  showing "Total Payable Incl. Tax (RM) 32,946.16" alongside "USD 8,020".
  1. The real transaction amount is always the ORIGINAL-currency total —
     the one NOT computed from an exchange rate.
  2. Do NOT return the exchange-rate-converted value as total_amount, even
     if it is labeled "Total" and even if it is the larger number.
  3. Set "currency" to the ORIGINAL currency (e.g. "USD"), not the
     converted one.
  4. If only one currency appears on the document, use that currency and
     ignore this rule."""

AUTHENTICITY_SIGNALS_BLOCK = """=== PART 2: AUTHENTICITY SIGNALS ===
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
- If a signal is true, include its key in signal_boxes with a tight
  bounding box around that specific mark/text.
- If a signal is false, you MAY still include its key with a box if you
  can identify a specific, plausible location for it — e.g. a blank
  signature line, an empty area where a company chop/logo would
  typically appear. This helps the auditor see exactly where to look.
  If there's no sensible specific location to point at, omit the key
  entirely.
- Each box is [ymin, xmin, ymax, xmax], normalized to a 0-1000 scale relative
  to the full image (top-left is [0,0], bottom-right is [1000,1000]).
- If has_company_chop or has_company_logo is present, the box should
  tightly bound that specific mark (compact box); if absent, bound the
  empty area where it would go.
- If has_company_name or has_signature is present, the box should bound
  that specific text/mark; if absent, bound the blank line/space where
  it would go.

Return ONLY valid JSON, no markdown, no explanation, no code fences. Return
this exact JSON structure:
"""

AUTHENTICITY_JSON_TAIL = """  "has_company_chop": false,
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

INVOICE_FULL_PROMPT = """You are an expert at extracting structured data from Malaysian SME
business documents AND detecting authenticity signals on them. You are looking
directly at the invoice IMAGE (not OCR text), so read the actual layout.

=== PART 1: FIELD EXTRACTION ===
""" + DOCUMENT_QUALITY_NOTE + """

IMPORTANT RULES:
- The VENDOR is the entity ISSUING the invoice (the seller), typically shown as the company name in the document header at the top
- The BUYER is who the invoice is billed TO ("Bill To", "Invoice To"), which is NOT the vendor
- INVOICE NUMBER: Look for the invoice number in these labels (case-insensitive), in priority order:
  1. "No.", "No :", "No. :", "No:", "Invoice No.", "Invoice No", "Invoice #", "Invoice Number", "Bill No.", "Doc No.", "Tax Invoice No.", or a bare "INVOICE:" label
  2. If the label is followed by ":" or wide whitespace, the value is what comes after
  3. Value may contain letters, digits, slashes "/", hyphens "-", dots "."
  4. Return the FULL value including all slashes and special chars
  5. Do NOT confuse invoice number with PO Number, Debtor/Customer Code, Contact Person, or a page number""" + DOCUMENT_NUMBER_NOTE + """
- PO REFERENCE: the PO number THIS invoice is billing against (often labeled "PO No.", "PO Ref", "Your PO No.") — this is
  NOT the invoice's own number.
- ITEM DESCRIPTION and QUANTITY: from the FIRST line-item row of the goods/services table (columns like
  "Description"/"Qty"). If multiple rows exist, use the first row.
- TOTAL AMOUNT: invoices list several amount lines — Subtotal, SST/GST/Tax, and the final Total.
  1. Return the amount on the line labeled "Total", "Grand Total", "Amount Due", or "Total (incl ...)" —
     this is the FINAL amount the customer must pay, after tax.
  2. NEVER return the "Subtotal"/"Sub Total" line — that is the pre-tax amount, not the total.
  3. NEVER return the SST/GST/tax line itself as the total — that is tax_amount, a separate field.
  4. The total is arithmetically the LARGEST of the amount lines (Total = Subtotal + tax). If unsure
     which line is which, the largest clearly-labeled amount is the total.""" + CURRENCY_NOTE + """
- TAX AMOUNT: the SST/GST/service tax amount (e.g. the "SST 6%" or "SST 8%" line), not the percentage itself.
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD

""" + AUTHENTICITY_SIGNALS_BLOCK + """{
  "invoice_number": "string or null",
  "vendor_name": "string or null (the SELLER shown in the header)",
  "invoice_date": "YYYY-MM-DD or null",
  "total_amount": number or null (the ORIGINAL-currency total, never an exchange-rate-converted value),
  "tax_amount": number or null,
  "currency": "string or null (the ORIGINAL currency of total_amount, e.g. RM, MYR, USD)",
  "po_reference": "string or null (the PO number this invoice bills against)",
  "item_description": "string or null (first line-item row)",
  "quantity": number or null (first line-item row),
""" + AUTHENTICITY_JSON_TAIL

PO_FULL_PROMPT = """You are an expert at extracting structured data from Malaysian SME
business documents AND detecting authenticity signals on them. You are looking
directly at the PURCHASE ORDER (PO) IMAGE (not OCR text), so read the actual layout.

=== PART 1: FIELD EXTRACTION ===
""" + DOCUMENT_QUALITY_NOTE + """

IMPORTANT RULES:
- The VENDOR is the SUPPLIER the PO is issued TO (look for labels like "Bill To Vendor", "Vendor:", "Supplier:", "To:")
- The company shown in the header of a PO is usually the BUYER issuing the order, NOT the vendor
- CRITICAL: PO Number rules:
  1. PO Number is found in a LABELED field, never inferred from other text.
  2. Look for these exact labels: "Doc No.", "PO No.", "P.O. No.", "Purchase Order No.", "Order No.", "PO Number", "Reference No."
  3. The value must be adjacent to (right of or below) the label.
  4. Do NOT extract any substring from company names, product descriptions, or address fields.
  5. Common Malaysian SME PO number formats: PONNNNNNN (e.g. PO3005713), PO-YYYY-NNNN, or numeric-only.
  6. If no clearly labeled PO number field exists, return null. Do NOT guess or extract from unrelated text.
  7. Length is typically 6-12 characters. Reject candidates shorter than 5.""" + DOCUMENT_NUMBER_NOTE + """
- ITEM DESCRIPTION and QUANTITY: from the FIRST line-item row of the goods table. If multiple rows exist, use the first row.
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
  If the document has both "Total Excl. Tax" and "Total Payable Incl. Tax", ALWAYS return "Total Payable Incl. Tax".""" + CURRENCY_NOTE + """
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD

""" + AUTHENTICITY_SIGNALS_BLOCK + """{
  "po_number": "string or null",
  "vendor_name": "string or null (the SUPPLIER the PO is issued to)",
  "po_date": "YYYY-MM-DD or null",
  "total_amount": number or null (the ORIGINAL-currency total, never an exchange-rate-converted value),
  "currency": "string or null (the ORIGINAL currency of total_amount, e.g. RM, MYR, USD)",
  "item_description": "string or null (first line-item row)",
  "quantity": number or null (first line-item row),
""" + AUTHENTICITY_JSON_TAIL

GR_FULL_PROMPT = """You are an expert at extracting structured data from Malaysian SME
business documents AND detecting authenticity signals on them. You are looking
directly at the GOODS RECEIPT (GR) IMAGE (not OCR text), so read the actual layout.

=== PART 1: FIELD EXTRACTION ===
""" + DOCUMENT_QUALITY_NOTE + """

IMPORTANT RULES:
- The VENDOR is who DELIVERED the goods (look for "Received From", "Delivered by", "Supplier")
- The company shown in the header is usually the RECEIVING company (the buyer's warehouse), NOT the vendor
- GR Number labels (priority order):
  1. "Doc No." (most common on Malaysian GRN, e.g. "Doc No.  PD6011823")
  2. "GRN No.", "GR No.", "Goods Receipt No."
  3. "Receipt No.", "Ref No."
  Common formats: PDNNNNNNN (e.g. PD6011652), GRN-YYYY-NNNN, or numeric-only.
  Do NOT confuse with:
  - PO Number (usually labeled "From Doc No." or "PO Ref") — put this in po_reference, not gr_number
  - Supplier Ref No. (that's the supplier's invoice reference)
  - Item Code / Part Number""" + DOCUMENT_NUMBER_NOTE + """
- PO REFERENCE: the PO number this GR was received against (often labeled "PO Ref", "From Doc No.") — this is
  NOT the GR's own number.
- ITEM DESCRIPTION and QUANTITY: from the FIRST line-item row of the goods table. If multiple rows exist, use the first row.
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD

""" + AUTHENTICITY_SIGNALS_BLOCK + """{
  "gr_number": "string or null",
  "vendor_name": "string or null (who DELIVERED the goods)",
  "receipt_date": "YYYY-MM-DD or null",
  "po_reference": "string or null (the PO number this GR was received against)",
  "item_description": "string or null (first line-item row)",
  "quantity": number or null (first line-item row),
""" + AUTHENTICITY_JSON_TAIL


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


def call_gemini_sdk(text_prompt, image=None, context='', timeout_ms=GEMINI_TIMEOUT_MS):
    """
    Shared low-level call through the official google-genai SDK — the
    single choke point every Gemini generateContent call in the codebase
    goes through (text extraction, merged invoice extraction+
    authenticity, authenticity-only, anomaly explanation).

    image: optional (mime_type, raw_bytes) tuple, e.g. from
      prepare_gemini_image_payload(), for a vision call.
    Returns response.text (str) on success, or None on any failure
    (missing key, network, timeout, bad model) — callers are responsible
    for JSON-parsing/stripping markdown fences from the returned text.
    """
    if not Config.GEMINI_API_KEY:
        print(f"DEBUG Gemini ({context}): GEMINI_API_KEY not set, skipping")
        return None

    parts = []
    if image is not None:
        mime_type, data = image
        parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    parts.append(types.Part.from_text(text=text_prompt))

    print(f"DEBUG Gemini request ({context}): model={Config.GEMINI_MODEL!r} "
          f"key=...{gemini_key_suffix()} via google-genai SDK")
    try:
        response = _get_client().models.generate_content(
            model=Config.GEMINI_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type='application/json',
                http_options=types.HttpOptions(timeout=timeout_ms),
            ),
        )
        return response.text
    except errors.APIError as e:
        print(f"DEBUG Gemini call error ({context}): {e.code} {e.status}: {e.message}")
        if e.code == 404:
            log_available_gemini_models()
        return None
    except Exception as e:
        print(f"DEBUG Gemini call error ({context}): {type(e).__name__}: {e}")
        return None


def prepare_gemini_image_payload(file_bytes, file_name):
    """
    Returns (mime_type, raw_bytes) ready for google.genai.types.Part.from_bytes,
    given the raw bytes of an already-read file (from DB or disk) and its
    original filename (used only to detect the extension) — takes bytes
    rather than a file path so it has no dependency on the local
    filesystem, which is ephemeral on Render's free tier.

    PDFs are rendered to their FIRST PAGE as a PNG image (PyMuPDF/fitz,
    opened directly from the in-memory bytes — no temp file needed)
    rather than sent as raw PDF bytes: sending a PDF's raw bytes doesn't
    reliably produce visual-signal detection or [ymin,xmin,ymax,xmax]
    bounding boxes for chop/logo/signature, since that's a rasterized-
    page-image task, not a document-text task. Image files are passed
    through unchanged.
    """
    ext = file_name.lower().rsplit('.', 1)[-1]
    if ext == 'pdf':
        doc = fitz.open(stream=file_bytes, filetype='pdf')
        try:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(PDF_RENDER_ZOOM, PDF_RENDER_ZOOM))
            png_bytes = pix.tobytes('png')
        finally:
            doc.close()
        return 'image/png', png_bytes

    mime = 'image/png' if ext == 'png' else 'image/jpeg'
    return mime, file_bytes


def gemini_extract_invoice_full(file_bytes, file_name):
    """
    Single merged Gemini vision call for an invoice: extracted fields AND
    authenticity signals in one request/response, so an invoice upload only
    ever spends one Gemini call (avoids the free-tier per-minute limit that
    two separate calls — field extraction + authenticity — used to hit).
    Returns the parsed dict, or None if the call fails for any reason
    (429, timeout, network, bad JSON, PDF render failure) or GEMINI_API_KEY
    is unset.
    """
    try:
        image = prepare_gemini_image_payload(file_bytes, file_name)
        text = call_gemini_sdk(
            INVOICE_FULL_PROMPT, image=image,
            context='merged invoice extraction+authenticity',
            timeout_ms=GEMINI_VISION_TIMEOUT_MS,
        )
        if text is None:
            return None
        result = json.loads(_strip_markdown_fences(text))
        print(f"DEBUG Gemini merged invoice+authenticity result: {result}")
        return result
    except Exception as e:
        print(f"DEBUG Gemini merged invoice call error: {type(e).__name__}: {e}")
        return None


def gemini_extract_po_full(file_bytes, file_name):
    """
    Single merged Gemini vision call for a PO: extracted fields AND
    authenticity signals in one request/response, mirroring
    gemini_extract_invoice_full() so a PO upload only ever spends one
    Gemini call. Returns the parsed dict, or None if the call fails for
    any reason (429, timeout, network, bad JSON, PDF render failure) or
    GEMINI_API_KEY is unset.
    """
    try:
        image = prepare_gemini_image_payload(file_bytes, file_name)
        text = call_gemini_sdk(
            PO_FULL_PROMPT, image=image,
            context='merged PO extraction+authenticity',
            timeout_ms=GEMINI_VISION_TIMEOUT_MS,
        )
        if text is None:
            return None
        result = json.loads(_strip_markdown_fences(text))
        print(f"DEBUG Gemini merged PO+authenticity result: {result}")
        return result
    except Exception as e:
        print(f"DEBUG Gemini merged PO call error: {type(e).__name__}: {e}")
        return None


def gemini_extract_gr_full(file_bytes, file_name):
    """
    Single merged Gemini vision call for a GR: extracted fields AND
    authenticity signals in one request/response, mirroring
    gemini_extract_invoice_full() so a GR upload only ever spends one
    Gemini call. Returns the parsed dict, or None if the call fails for
    any reason (429, timeout, network, bad JSON, PDF render failure) or
    GEMINI_API_KEY is unset.
    """
    try:
        image = prepare_gemini_image_payload(file_bytes, file_name)
        text = call_gemini_sdk(
            GR_FULL_PROMPT, image=image,
            context='merged GR extraction+authenticity',
            timeout_ms=GEMINI_VISION_TIMEOUT_MS,
        )
        if text is None:
            return None
        result = json.loads(_strip_markdown_fences(text))
        print(f"DEBUG Gemini merged GR+authenticity result: {result}")
        return result
    except Exception as e:
        print(f"DEBUG Gemini merged GR call error: {type(e).__name__}: {e}")
        return None
