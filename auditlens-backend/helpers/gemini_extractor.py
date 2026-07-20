import json
import os
import re
import time
import psutil
import requests
import fitz  # PyMuPDF
from google import genai
from google.genai import types, errors
from config import Config

# TEMP-DEBUG: lightweight RSS checkpoint logging for investigating the
# Render 512MB OOM during a single invoice upload. Logs only the
# process's resident memory in MB at a named lifecycle point — never
# document content, image bytes, or PDF content. Safe to delete this
# function and its call site (tagged "# TEMP-DEBUG") once no longer
# needed.
def _debug_log_memory(checkpoint):
    rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    print(f"DEBUG MEMORY CHECKPOINT | {checkpoint} | rss_mb={rss_mb:.1f}")

# ListModels is still a plain REST call — confirmed working even with
# this account's "AQ."-prefixed key (only generateContent rejects it over
# raw HTTP), so there's no need to route it through the SDK too.
GEMINI_MODELS_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_TIMEOUT_MS = 15_000
GEMINI_VISION_TIMEOUT_MS = 20_000

# PDF_RENDER_ZOOM controls the resolution (DPI) a PDF's first page is
# rasterized at before being sent to Gemini as an image — see
# prepare_gemini_image_payload() below. PyMuPDF's zoom factor maps to DPI
# as zoom * 72 (its base unit is 72 DPI at zoom 1.0), so 3.0 -> ~216 DPI.
#
# Why higher resolution matters here: Gemini reads the page as a raw
# image, not as PDF text — every character is whatever pixels the render
# produced, with no underlying text layer to fall back on. Small printed
# fields (a PO number in a corner box, a "Doc No." value in a compact
# header line, digits inside a total-amount cell) can be only a handful
# of pixels tall at low DPI. At the previous 2.0 zoom (~144 DPI), a 6pt
# label on a standard A4/Letter page renders at well under 20px tall —
# thin strokes and tight digit spacing (e.g. distinguishing "PO3006000"
# from "PO3OO6OOO") become genuinely ambiguous at that pixel density, the
# same way a low-resolution scan is harder for a person to read even
# when the layout is otherwise clear. Rendering at a higher DPI gives
# those same small fields more pixels to be represented by, which is the
# single biggest lever for improving small-text legibility short of
# cropping/zooming into a sub-region (out of scope here — this stays a
# single whole-page render, one image, one Gemini call per document).
#
# 3.0 (~216 DPI) was chosen as a moderate step up from 2.0, not the more
# aggressive 4.0 (~288 DPI) also suggested: PyMuPDF's rendered pixmap
# scales with the SQUARE of the zoom factor (width and height both grow
# linearly with zoom), so going 2.0 -> 3.0 is already a 2.25x increase in
# raw pixel count (and therefore in-memory pixmap size) per page, while
# 2.0 -> 4.0 would be a 4x increase. On Render's free-tier 512MB RAM
# limit (see /admin/debug/memory), a single extra in-flight PDF render
# at 4x the pixel count is a less predictable memory spike than the more
# moderate 2.25x step, for a resolution jump (144 -> 216 DPI) that
# already comfortably clears the "small printed label" legibility
# threshold. The output is still encoded losslessly as PNG
# (pix.tobytes('png') below), so this resolution increase is not
# undermined by any lossy compression afterward.
PDF_RENDER_ZOOM = 3.0  # ~216 DPI — was 2.0 (~144 DPI); see rationale above

# Uploading several documents in quick succession can hit the free-tier
# per-minute rate limit on a later call even though each upload only makes
# ONE Gemini call — the limit is per-minute across all calls, not per
# upload. These are the backoff delays (seconds) between retries of a
# 429 specifically; a burst that trips the limit has usually cleared by
# the time the second retry's wait elapses.
GEMINI_RATE_LIMIT_RETRY_DELAYS = (20, 10)

_client = None


class GeminiRateLimitError(Exception):
    """Raised by call_gemini_sdk only when the caller opts in via
    on_rate_limit='raise' AND all retries for a 429 were exhausted — lets
    a caller (currently only run_authenticity_check's fallback) tell a
    temporary rate limit apart from a permanent failure, without changing
    the str-or-None return contract every other caller relies on."""
    pass


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

LABEL_NOT_VALUE_NOTE = """
- NEVER return a field's own label/placeholder text as its value. If the
  text you're about to return for a field is itself one of the generic
  words "Ref", "No", "Number", "Date", "Amount" (with or without a colon,
  in any capitalization) — that is the LABEL, not the value beside/below
  it. Keep reading past the label to find the actual printed value; if no
  actual value is visible next to that label, return null instead of the
  label word.
- If uncertain which of two nearby pieces of text is the label and which
  is the value, return null rather than guessing.
- Ignore handwritten annotations, margin notes, and pen marks when
  extracting field values — only use them if a field is unmistakably
  ONLY present as a handwritten official entry (e.g. a hand-filled date
  field on an otherwise printed form). Do not let a handwritten note,
  scribble, or unrelated jotted number substitute for a document's
  official printed amount, tax, or reference number."""

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
  together with "EXCHANGE RATE=1.2670 ... SUB TOTAL= 10,161.34 ... TOTAL=
  10,161.34 (RM)", or a PO showing "Total Payable Incl. Tax (RM) 32,946.16"
  alongside "USD 8,020".
  1. The real transaction amount is always the ORIGINAL-currency total —
     the one tagged with a foreign-currency symbol/code (US$, USD, etc.),
     NOT the one computed from an exchange rate.
  2. Do NOT return the exchange-rate-converted value as total_amount, even
     if it is labeled "Total"/"Sub Total", even if it is the larger number,
     and even if it is positioned BELOW the real total (an exchange-rate
     conversion block, once it starts, may repeat the word "Total" one or
     more times for its own converted subtotal/total — none of those
     belong in total_amount).
  3. The "(US$)"/"USD" currency tag may be visually separated from its
     number (e.g. the tag on one line/position, the number just below or
     beside it) — still treat that number as the USD total, not whatever
     number is physically closest to an unrelated "(RM)" tag elsewhere.
  4. CRITICAL: "currency" MUST be the currency of the total_amount value
     you actually return — never a different currency symbol that merely
     appears somewhere else on the document. If total_amount is 8020
     because you found it next to "US$", currency MUST be "USD", even if
     "(RM)" also appears elsewhere on the page (e.g. in a separate
     converted-total block, or another field like a tax-inclusive local
     total). Getting the amount right but the currency wrong (e.g.
     returning 8020 with currency "RM") is a mistake — verify the two
     values are describing the SAME amount before returning them.
  5. If only one currency appears on the document, use that currency and
     ignore this rule."""

LINE_ITEMS_NOTE = """
- LINE ITEMS: extract EVERY row of the goods/services table (not just the first) as a "line_items"
  array, in the SAME order as printed. Each entry:
  {"item_code": string or null, "description": string, "quantity": number or null,
   "unit_price": number or null, "amount": number or null}
  1. item_code is the SKU/part-code (e.g. "SLT-MOS-N60R", "MTC-IND-4R7M") if the description cell
     has one — whether it's printed in a SEPARATE code column, OR as the leading token of a single
     combined description cell (e.g. the cell reads "SLT-MOS-N60R MOSFET N-Ch 600V TO-220"). EITHER
     WAY, split it out into item_code and put ONLY the remaining text ("MOSFET N-Ch 600V TO-220")
     in description — never leave the code duplicated inside description once it's been captured
     in item_code. This must be done CONSISTENTLY for every row, on every document type (invoice,
     PO, GR) — the SAME product on different documents must end up with the SAME item_code and
     description, since these are matched against each other across documents.
  2. If a row's description has no code-shaped prefix at all, item_code is null and description is
     the full cell text unchanged.
  3. quantity/unit_price/amount: null for any cell you cannot confidently read, never a guess.
  4. If there are more than 50 rows, return only the first 50.
  5. If no line-item table can be found at all, return an empty array []."""

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
""" + DOCUMENT_QUALITY_NOTE + LABEL_NOT_VALUE_NOTE + """

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
  "Description"/"Qty"). If multiple rows exist, use the first row.""" + LINE_ITEMS_NOTE + """
- INVOICE DATE: the date THIS invoice was issued.
  Label priority (use the FIRST of these that appears on the document):
  1. "Invoice Date"
  2. "Date of Invoice"
  3. "Invoice Issue Date"
  Do NOT use, even if no better candidate is found:
  - a "PO Date" / "Order Date" (that belongs to the referenced Purchase Order, a different document)
  - a "Delivery Date" (when goods are/were delivered, not when the invoice was issued)
  - a "Due Date" / "Payment Due Date" (when payment is owed, not when the invoice was issued)
  If none of the three invoice-date labels above can be found, return null rather than substituting a
  PO/delivery/due date.
- TOTAL AMOUNT: invoices list several amount lines — Subtotal, SST/GST/Tax, the final Total, and often
  UNRELATED numbers nearby (bank/account numbers, handwritten notes, reference codes). Read the whole
  document layout and pick the correct FINAL payable amount; do not just grab the nearest number to the
  word "Total". Use this LAYERED priority — try Priority 1 first, then Priority 2, then Priority 3 only
  if neither matched:
  Priority 1 (highest — check these labels first):
  1. "TOTAL"
  2. "GRAND TOTAL"
  3. "TOTAL PAYABLE"
  4. "AMOUNT DUE"
  Priority 2 (check these if none of Priority 1 is present):
  5. "TOTAL AMOUNT"
  6. "NET AMOUNT"
  7. "AMOUNT"
  Priority 3 (only if NO exact label from Priority 1 or 2 exists anywhere on the document): select the
  final payable monetary value from the document's summary/totals area (typically the bottom-most and/or
  largest clearly-labeled monetary figure there) rather than returning null — a real invoice's payable
  amount is usually identifiable from its position and formatting even when the exact wording doesn't
  match a listed label. Only return null if the summary area itself cannot be confidently identified.
  This is the FINAL amount the customer must pay, after tax.
  Regardless of which priority tier you matched on, do NOT select:
  - a tax amount (SST/GST/VAT line) — that is tax_amount, a separate field, never the total.
  - a subtotal ("Subtotal"/"Sub Total") — that is the pre-tax amount, not the total.
  - an account number, bank account number, or invoice/reference number — these are identifiers, not
    monetary amounts, even if they happen to be numeric and positioned near the amount area.
  - a handwritten number or annotation — never treat handwriting as the official total.""" + CURRENCY_NOTE + """
- TAX AMOUNT: the OFFICIAL SST/GST/VAT tax amount, printed on a line explicitly labeled with one of those
  terms (e.g. "SST 6%", "GST", "VAT") — not the percentage itself, and never a number extrapolated from
  elsewhere. Do NOT extract:
  - an account number or any part of one
  - a handwritten number or annotation
  - a number that merely looks tax-sized but has no SST/GST/VAT label attached
  If no line is explicitly labeled as tax/SST/GST/VAT, return null. A tax amount only means something
  relative to the invoice's total_amount — if you cannot also identify a total_amount anywhere on this
  document (per the TOTAL AMOUNT rule above, including its Priority 3 fallback), treat any tax-labeled
  value you found with extra caution and double-check it is genuinely SST/GST/VAT-labeled before
  returning it, since an unverifiable tax figure is a common source of extraction errors.
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
  "line_items": [{"item_code": null, "description": "string", "quantity": null, "unit_price": null, "amount": null}],
""" + AUTHENTICITY_JSON_TAIL

PO_FULL_PROMPT = """You are an expert at extracting structured data from Malaysian SME
business documents AND detecting authenticity signals on them. You are looking
directly at the PURCHASE ORDER (PO) IMAGE (not OCR text), so read the actual layout.

=== PART 1: FIELD EXTRACTION ===
""" + DOCUMENT_QUALITY_NOTE + LABEL_NOT_VALUE_NOTE + """

IMPORTANT RULES:
- CRITICAL — VENDOR NAME is a COMMON MISTAKE, read carefully: the company
  name printed in the LETTERHEAD at the top of a PO (the largest/most
  prominent company name on the page) is the BUYER — the company ISSUING
  the order — and is NEVER the vendor, no matter how prominent it looks.
  The VENDOR is the SUPPLIER the PO is addressed TO, named under a
  "Supplier"/"Supplier Address"/"Vendor"/"Bill To Vendor"/"To" heading
  further down the page (NOT "Ship To"/"Deliver To", which is a different
  address). Example: if the letterhead says "ORIONTECH ELECTRONICS SDN.
  BHD." and, further down, a "SUPPLIER" heading is followed by "MEGATECH
  COMPONENTS (M) SDN. BHD.", the vendor_name is "MEGATECH COMPONENTS (M)
  SDN. BHD." — the letterhead company, ORIONTECH, must NEVER be returned
  as vendor_name on a PO. If no supplier heading/section can be found,
  return null rather than defaulting to the letterhead.
- CRITICAL: PO Number rules:
  1. PO Number identification priority — different suppliers label this field differently. Search for
     these labels, in this priority order, and use the value adjacent to (right of or below) the FIRST
     one you find on the document:
     1. "Purchase Order Number"
     2. "PO Number"
     3. "PO No" / "PO No."
     4. "Order Number" / "Order No."
     5. "Document Number"
     6. "Document No." / "Doc No."
     7. "PO Ref No" / "PO Ref. No."
     Accept close punctuation/spacing variants of these labels (e.g. "P.O. No.", "PONo:") — but do not
     treat an unrelated label as a match just because it also happens to contain the word "No".
  2. Read PAST the label to the actual printed value next to it. NEVER return any of the following as
     the po_number value — these are field labels or placeholder text, not real identifiers, even if
     one of them is literally the only text printed next to the label:
     - "Ref", "No", "Number" (with or without ":", any case)
     - any other bare field-label word
     - empty placeholder text (blank underscores, dashes, "____", "...", or similar)
     If a label from the priority list above is present but no real value follows it, return null —
     do not fall back to returning the label itself.
  3. Example:
       Document reads:  "Doc No: PO3006000"
       Correct output:  {"po_number": "PO3006000"}
       Wrong output:    {"po_number": "Doc No"}  or  {"po_number": "No"}
  4. If more than one candidate identifier appears on the document, choose the value that:
     - identifies the PURCHASE ORDER document itself, not a different document referenced on it
     - appears near the PO's header/title area (top of the document, alongside a "Purchase Order"/"PO"
       heading), rather than buried in a body/table section
     - matches a plausible alphanumeric PO format (see format note in rule 6 below)
     Do NOT use any of the following as po_number, even if no better candidate is found:
     - a supplier account number / customer account code
     - an invoice number (that belongs to a different document)
     - an item code / part number from the goods table
     - a delivery reference / shipping reference number
  5. Do NOT extract any substring from company names, product descriptions, or address fields.
  6. Common Malaysian SME PO number formats: PONNNNNNN (e.g. PO3005713), PO-YYYY-NNNN, or numeric-only.
  7. If no clearly labeled PO number field exists, return null. Do NOT guess or extract from unrelated text.
  8. Length is typically 6-12 characters. Reject candidates shorter than 5 — a short generic word like
     "Ref" or "No" must never be returned even if it's the only text near the label.""" + DOCUMENT_NUMBER_NOTE + """
- ITEM DESCRIPTION and QUANTITY: from the FIRST line-item row of the goods table. If multiple rows exist, use the first row.""" + LINE_ITEMS_NOTE + """
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
  "line_items": [{"item_code": null, "description": "string", "quantity": null, "unit_price": null, "amount": null}],
""" + AUTHENTICITY_JSON_TAIL

GR_FULL_PROMPT = """You are an expert at extracting structured data from Malaysian SME
business documents AND detecting authenticity signals on them. You are looking
directly at the GOODS RECEIPT (GR) IMAGE (not OCR text), so read the actual layout.

=== PART 1: FIELD EXTRACTION ===
""" + DOCUMENT_QUALITY_NOTE + LABEL_NOT_VALUE_NOTE + """

IMPORTANT RULES:
- CRITICAL — VENDOR NAME is a COMMON MISTAKE, read carefully: the company
  name printed in the LETTERHEAD at the top of a GR (the largest/most
  prominent company name on the page) is the RECEIVING company (the
  buyer's own warehouse) — and is NEVER the vendor, no matter how
  prominent it looks. The VENDOR is who DELIVERED the goods, named under
  a "Received From"/"Delivered By"/"Supplier" heading further down the
  page. If no such heading/section can be found, return null rather than
  defaulting to the letterhead.
- GR Number labels (priority order):
  1. "Doc No." (most common on Malaysian GRN, e.g. "Doc No.  PD6011823")
  2. "GRN No.", "GR No.", "Goods Receipt No."
  3. "Receipt No.", "Ref No."
  Common formats: PDNNNNNNN (e.g. PD6011652), GRN-YYYY-NNNN, or numeric-only.
  Do NOT confuse with:
  - PO Number (usually labeled "From Doc No." or "PO Ref", e.g. "From Doc
    No.: PO3006000") — put this in po_reference, not gr_number. A label
    that STARTS WITH "From" (e.g. "From Doc No.") is ALWAYS the referenced
    PO, never the GR's own document number, even though it also contains
    the words "Doc No."
  - Supplier Ref No. (that's the supplier's invoice reference)
  - Item Code / Part Number""" + DOCUMENT_NUMBER_NOTE + """
- PO REFERENCE: the PO number this GR was received against (often labeled "PO Ref", "From Doc No.") — this is
  NOT the GR's own number.
- RECEIPT DATE: Goods Receipt documents commonly show MULTIPLE dates (the PO's own date, the date goods
  were actually received, a general document/print date). receipt_date must be the date OF THIS GR
  DOCUMENT, not a date belonging to the referenced PO. Priority order:
  1. The Goods Receipt's own document date — a date printed next to the SAME label/section as the GR's
     own document number (gr_number above), e.g. next to "Doc No." on the GR itself, or a "GR Date"/
     "Receipt Date" label.
  2. A receipt/transaction date — labeled "Received Date", "Date Received", "Receipt Date", or similar,
     describing when the goods physically arrived.
  3. A general delivery date — labeled "Delivery Date" or "Date Delivered".
  Do NOT use a date that is explicitly attached to the referenced Purchase Order (e.g. appearing next to
  or under a "PO Date", "Order Date", or the same "From Doc No." block that gave you po_reference above)
  — that date belongs to a different document and must never be returned as receipt_date, UNLESS none of
  the three GR-specific date types above exist anywhere on the document, in which case return null rather
  than substituting the PO's date.
- ITEM DESCRIPTION and QUANTITY: from the FIRST line-item row of the goods table. If multiple rows exist, use the first row.""" + LINE_ITEMS_NOTE + """
- TOTAL AMOUNT / CURRENCY: most GRNs carry no monetary total (they record quantity received, not money) — leave
  total_amount and currency null in that case. If the GR DOES show a monetary value, apply the same original-
  currency-vs-converted-value rule as below.""" + CURRENCY_NOTE + """
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
  "total_amount": number or null (usually null — most GRNs carry no monetary total),
  "currency": "string or null (only if total_amount is present)",
  "line_items": [{"item_code": null, "description": "string", "quantity": null, "unit_price": null, "amount": null}],
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


def _is_rate_limit_error(e):
    code = getattr(e, 'code', None)
    status = str(getattr(e, 'status', '') or '').upper()
    return code == 429 or 'RESOURCE_EXHAUSTED' in status


def call_gemini_sdk(text_prompt, image=None, context='', timeout_ms=GEMINI_TIMEOUT_MS,
                     on_rate_limit='return_none'):
    """
    Shared low-level call through the official google-genai SDK — the
    single choke point every Gemini generateContent call in the codebase
    goes through (text extraction, merged invoice extraction+
    authenticity, authenticity-only, anomaly explanation).

    image: optional (mime_type, raw_bytes) tuple, e.g. from
      prepare_gemini_image_payload(), for a vision call.
    on_rate_limit: 'return_none' (default) — after retries are exhausted
      on a 429, behave like any other failure (return None). 'raise' —
      instead raise GeminiRateLimitError, so a caller that wants to tell
      "temporarily rate-limited" apart from "permanently unavailable"
      (currently only run_authenticity_check's fallback) can do so.
    Returns response.text (str) on success, or None on any failure
    (missing key, network, timeout, bad model) — callers are responsible
    for JSON-parsing/stripping markdown fences from the returned text.

    A 429 (free-tier per-minute rate limit — plausible when several
    documents are uploaded in quick succession, since the limit is
    per-minute across ALL calls, not per upload) is retried automatically
    with backoff (GEMINI_RATE_LIMIT_RETRY_DELAYS) before giving up, so a
    short burst of uploads doesn't permanently degrade one of them to the
    OCR-text-only fallback when Gemini would have succeeded moments later.
    """
    if not Config.GEMINI_API_KEY:
        print(f"DEBUG Gemini ({context}): GEMINI_API_KEY not set, skipping")
        return None

    parts = []
    if image is not None:
        mime_type, data = image
        parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    parts.append(types.Part.from_text(text=text_prompt))

    # TEMP-DEBUG: confirm Gemini actually receives the rendered image —
    # logged once per call (not per retry attempt, since the image part
    # doesn't change across retries). Safe to delete this block once no
    # longer needed.
    print(f"DEBUG GEMINI PAYLOAD | parts={1 if image is not None else 0} | "
          f"mime={image[0] if image is not None else None} | "
          f"size_kb={(len(image[1]) / 1024) if image is not None else 0.0:.1f}")

    attempt = 0
    while True:
        print(f"DEBUG Gemini request ({context}): model={Config.GEMINI_MODEL!r} "
              f"key=...{gemini_key_suffix()} via google-genai SDK"
              + (f" (retry {attempt}/{len(GEMINI_RATE_LIMIT_RETRY_DELAYS)})" if attempt else ""))
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
            if _is_rate_limit_error(e) and attempt < len(GEMINI_RATE_LIMIT_RETRY_DELAYS):
                delay = GEMINI_RATE_LIMIT_RETRY_DELAYS[attempt]
                attempt += 1
                print(f"DEBUG Gemini ({context}): rate-limited (429), retrying in {delay}s")
                time.sleep(delay)
                continue
            if e.code == 404:
                log_available_gemini_models()
            if _is_rate_limit_error(e) and on_rate_limit == 'raise':
                raise GeminiRateLimitError(str(e)) from e
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
    page-image task, not a document-text task. The render resolution is
    controlled by PDF_RENDER_ZOOM (see its comment above) — the higher
    that value, the more pixels small printed fields (PO number, doc
    number, total-amount cell) get to be represented by, which is what
    actually determines whether Gemini can read them.

    No resizing or compression is applied anywhere in this function:
    the PDF render is encoded via pix.tobytes('png') — PNG is lossless,
    so nothing is thrown away after rendering at PDF_RENDER_ZOOM. Image
    files (jpg/png) are passed through completely unchanged — neither
    downscaled nor re-compressed — specifically so a directly-uploaded
    photo/scan's original detail on small fields is never degraded
    before Gemini sees it.
    """
    ext = file_name.lower().rsplit('.', 1)[-1]
    if ext == 'pdf':
        doc = fitz.open(stream=file_bytes, filetype='pdf')
        try:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(PDF_RENDER_ZOOM, PDF_RENDER_ZOOM))
            png_bytes = pix.tobytes('png')
            # TEMP-DEBUG: rendered-image characteristics, to compare against
            # the previous working version. Reads pix.width/pix.height
            # before `del pix` below. Safe to delete this one line once no
            # longer needed.
            print(f"DEBUG GEMINI IMAGE | width={pix.width} | height={pix.height} | "
                  f"size_kb={len(png_bytes) / 1024:.1f} | zoom={PDF_RENDER_ZOOM}")
            _debug_log_memory('3_after_pdf_render_for_gemini')  # TEMP-DEBUG (logged before del pix, to catch the true peak)
            # TEMP-DEBUG memory fix: release the raw (uncompressed) pixmap
            # buffer as soon as the much-smaller compressed PNG has been
            # extracted from it, rather than waiting for this function to
            # return. At PDF_RENDER_ZOOM=3.0 (~216 DPI) this buffer is the
            # single largest transient allocation in this function — roughly
            # width*height*3 bytes, ~13.5MB for an A4 page, vs. ~6MB at the
            # old zoom=2.0 — so freeing it immediately (rather than relying
            # on end-of-function refcounting) reduces peak memory.
            del pix
        finally:
            doc.close()
            # TEMP-DEBUG memory fix: MuPDF keeps its own internal cache
            # ("store") of parsed/rendered objects that can persist and grow
            # across renders within the same long-lived worker process, even
            # after the Document itself is closed. Shrinking it to 0 here
            # returns that cache memory — relevant because Render's free-tier
            # 512MB limit is a per-PROCESS ceiling, not a per-request one, so
            # memory this doesn't release stays counted against every
            # subsequent upload handled by the same worker.
            fitz.TOOLS.store_shrink(100)
        return 'image/png', png_bytes

    mime = 'image/png' if ext == 'png' else 'image/jpeg'
    return mime, file_bytes


# ============================================================
# TEMP DEBUG LOGGING — Gemini field-extraction trace. Safe to delete
# this whole function plus its 3 call sites (tagged "# TEMP-DEBUG")
# below once no longer needed.
#
# Purpose: log only the specific identifying fields, right where Gemini's
# JSON is first parsed — before anything in routes/documents.py merges,
# validates, or stores it — to tell apart "Gemini itself never returned
# this value" from "something downstream (validator/merge/DB) removed
# it". `result` here is always the already-parsed JSON dict of Gemini's
# TEXT response; image bytes / PDF content are never part of it and are
# never logged by this function.
# ============================================================
def _debug_log_gemini_fields(document_type, result):
    print(
        f"DEBUG GEMINI EXTRACTION FIELDS | doc_type={document_type} | "
        f"invoice_number={result.get('invoice_number')} | "
        f"invoice_date={result.get('invoice_date')} | "
        f"total_amount={result.get('total_amount')} | "
        f"po_number={result.get('po_number')} | "
        f"gr_number={result.get('gr_number')}"
    )
# ============================================================
# END TEMP DEBUG LOGGING helper
# ============================================================


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
        # TEMP-DEBUG: the complete set of invoice scalar fields Gemini
        # itself returned, logged right after parsing — before anything
        # in routes/documents.py merges/validates/stores it — to tell
        # apart Gemini failing to extract vs. downstream code removing
        # values. Safe to delete this line once no longer needed.
        print(f"DEBUG GEMINI RESULT | type=invoice | invoice_number={result.get('invoice_number')} | "
              f"invoice_date={result.get('invoice_date')} | total_amount={result.get('total_amount')} | "
              f"tax_amount={result.get('tax_amount')} | vendor={result.get('vendor_name')}")
        print(f"DEBUG Gemini merged invoice+authenticity result: {result}")
        _debug_log_gemini_fields('invoice', result)  # TEMP-DEBUG
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
        # TEMP-DEBUG: see the matching comment in gemini_extract_invoice_full.
        print(f"DEBUG GEMINI RESULT | type=po | po_number={result.get('po_number')} | "
              f"po_date={result.get('po_date')} | total_amount={result.get('total_amount')} | "
              f"vendor={result.get('vendor_name')}")
        print(f"DEBUG Gemini merged PO+authenticity result: {result}")
        _debug_log_gemini_fields('po', result)  # TEMP-DEBUG
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
        # TEMP-DEBUG: see the matching comment in gemini_extract_invoice_full.
        print(f"DEBUG GEMINI RESULT | type=gr | gr_number={result.get('gr_number')} | "
              f"receipt_date={result.get('receipt_date')} | vendor={result.get('vendor_name')}")
        print(f"DEBUG Gemini merged GR+authenticity result: {result}")
        _debug_log_gemini_fields('gr', result)  # TEMP-DEBUG
        return result
    except Exception as e:
        print(f"DEBUG Gemini merged GR call error: {type(e).__name__}: {e}")
        return None
