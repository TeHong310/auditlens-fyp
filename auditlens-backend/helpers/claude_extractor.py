"""Claude Vision extraction.

Used both in production (routed via helpers/ai_extractor_router.py +
routes/documents.py, per AI_EXTRACTION_PROVIDER) and by the manual test
script (scripts/test_claude_extraction.py). extract_with_claude_test is
kept as an alias of extract_with_claude for backward compatibility with
that script — there is no functional difference between "test" and
"production" extraction, only whether the caller wires the result into
the DB.

Image rendering (PDF -> PNG, or pass-through for jpg/png) is NOT
duplicated here — prepare_gemini_image_payload() from gemini_extractor.py
is reused as-is, so this module has no PDF-handling logic of its own to
keep in sync with the real one.

Schema note: one unified prompt/schema is used for all three document
types (invoice/PO/GR), unlike gemini_extractor.py's three separate
prompts — the `document_type` argument tells Claude which document it's
looking at, so it knows which of invoice_number/po_number/gr_number and
invoice_date/po_date/receipt_date are actually relevant; the rest stay
null. The schema is a superset covering every field routes/documents.py's
three merge-key sets read (see each endpoint's `_merge_keys` tuple) —
including item_description/quantity (the first line item, kept only for
backward-compat with the older single-value fields the regex fallback
still populates) alongside the full line_items array.
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

DOCUMENT-TYPE-SPECIFIC FIELDS: you will be told which of invoice/PO/GR
this document is. Only fill the ID/date field(s) that actually apply to
THAT type — leave the others null:
- invoice: fill invoice_number, invoice_date. Leave po_number, po_date,
  gr_number, receipt_date null.
- PO (purchase order): fill po_number, po_date. Leave invoice_number,
  invoice_date, gr_number, receipt_date null. A PO never has po_reference
  (it doesn't reference another PO) — leave that null too.
- GR (goods receipt): fill gr_number, receipt_date. Leave invoice_number,
  invoice_date, po_number, po_date null.
po_reference (the PO this invoice/GR was raised against — NOT that
document's own number) applies to invoices and GRs only.

Return null (not empty string, not "N/A") for any field you cannot
confidently extract — never guess.

Return ONLY valid JSON, no markdown, no code fences, no explanation —
exactly this structure:
{
  "invoice_number": "string or null",
  "po_number": "string or null",
  "gr_number": "string or null",
  "vendor_name": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "po_date": "YYYY-MM-DD or null",
  "receipt_date": "YYYY-MM-DD or null",
  "total_amount": number or null,
  "tax_amount": number or null,
  "currency": "string or null",
  "po_reference": "string or null",
  "item_description": "string or null (the FIRST line item's description, same value as line_items[0].description)",
  "quantity": number or null (the FIRST line item's quantity, same value as line_items[0].quantity),
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


def extract_with_claude(image, document_type):
    """Claude Vision extraction. Makes ONE real Anthropic API call.

    image: (mime_type, raw_bytes) tuple — same shape gemini_extractor.
      py's prepare_gemini_image_payload() returns; reuse that function to
      build this argument rather than re-rendering a PDF here.
    document_type: 'invoice' | 'po' | 'gr'.

    Returns the parsed dict on success, or None if the call fails for
    any reason (no API key, network, timeout, bad JSON) — same
    fail-soft contract as gemini_extract_*_full(). Callers are
    responsible for deciding what "success" means for their purposes
    (see helpers/ai_extractor_router.py's completeness check) and for
    caching (see helpers/claude_cache.py for production, or this
    module's own file-based cache below for the manual test script).
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
        # No `temperature` — some current models reject it outright
        # ("`temperature` is deprecated for this model", seen in
        # production). max_tokens/model/messages/system are all still
        # valid, current Messages API parameters; `timeout` is an
        # SDK/HTTP-layer request timeout, not a generation-config
        # parameter, so it stays.
        response = client.messages.create(
            model=Config.CLAUDE_MODEL,
            max_tokens=4096,
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

    line_items = result.get('line_items') or []
    print(f"DEBUG CLAUDE SUCCESS | vendor={result.get('vendor_name')} | "
          f"amount={result.get('total_amount')} | line_items_count={len(line_items)}")

    return result


# Backward-compat alias — scripts/test_claude_extraction.py was written
# against this name before Claude was wired into production; no
# functional difference from extract_with_claude() above.
extract_with_claude_test = extract_with_claude


# ============================================================
# TEXT-ONLY COMPLETION — used by helpers/ai_assistant.py (AI Audit
# Assistant). No image, no extraction schema — just a system prompt +
# user text in, plain response text out. Reuses the SAME client/call/
# error-handling shape as extract_with_claude/analyze_document_
# authenticity above rather than opening a second way to talk to the
# Anthropic Messages API.
# ============================================================

def ask_claude_text(system_prompt, user_text, max_tokens=1024):
    """Text-only Claude call. Returns the raw response text (str), or
    None on any failure (no API key, network, timeout) — same fail-soft
    contract as every other function in this module; callers decide
    what to do next (helpers/ai_assistant.py falls back to Gemini)."""
    client = _get_claude_client()
    if client is None:
        print("DEBUG CLAUDE TEXT | skipped: ANTHROPIC_API_KEY not set")
        return None

    print(f"DEBUG CLAUDE TEXT REQUEST | model={Config.CLAUDE_MODEL!r} | prompt_len={len(user_text)}")

    try:
        response = client.messages.create(
            model=Config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
            timeout=CLAUDE_TIMEOUT_S,
        )
    except Exception as e:
        print(f"DEBUG CLAUDE TEXT REQUEST error: {type(e).__name__}: {e}")
        return None

    text = "".join(block.text for block in response.content if getattr(block, 'type', None) == 'text')
    _raw_preview = text if len(text) <= 1500 else text[:1500] + '...<truncated>'
    print(f"DEBUG CLAUDE TEXT RESPONSE | text={_raw_preview!r}")
    return text.strip()


# ============================================================
# AUTHENTICATION / VISUAL VERIFICATION
#
# Separate prompt+schema from CLAUDE_SYSTEM_PROMPT above — this is a
# visual authenticity inspection (supplier identity, tampering,
# stamps/signatures, bounding boxes), not field extraction. Reuses the
# same client/call/parsing plumbing (_get_claude_client, messages.create
# shape, _strip_markdown_fences) so there is only one place that knows
# how to talk to the Anthropic Messages API.
# ============================================================

# Bumped whenever this prompt/schema changes meaningfully — part of the
# authenticity cache key (helpers/authenticity_cache.py) so a stale
# cached result shaped for an older prompt version is never served.
CLAUDE_AUTHENTICITY_PROMPT_VERSION = 'v5'

CLAUDE_AUTHENTICITY_PROMPT = """You are an enterprise AP (Accounts Payable) audit AI performing VISUAL
document authenticity verification. You are NOT doing OCR/field
extraction — you are inspecting the document image itself: who issued
it, whether the expected visual marks (logo, stamp, signature) are
present and where EXACTLY, and whether anything looks visually
tampered with.

SUPPLIER IDENTITY — the single most common mistake to avoid is
confusing the BUYER for the SUPPLIER. Which company counts as the
supplier depends on the document type — see DOCUMENT TYPE GUIDANCE
below for the exact priority order to use for THIS document:
- NEVER treat the Bill To / Ship To / Buyer / Customer / Purchaser /
  Receiver company's name, logo, or address as supplier evidence, even
  if it is printed larger, higher on the page, or more prominently than
  the actual supplier's information.
  Example: a Purchase Order's letterhead reads "EMITS TECHNOLOGY SDN
  BHD" because EMITS is the BUYER issuing the PO — on THIS document
  EMITS is the buyer's own header, not supplier evidence, even though
  it is the biggest, most prominent logo on the page. The supplier is
  whichever company appears in the PO's own Supplier/Vendor name or
  address field (e.g. "Coilcraft Singapore Pte Ltd"). Conversely, on
  Coilcraft's own Invoice, Coilcraft's letterhead IS the supplier, and
  a "Ship To: EMITS TECHNOLOGY SDN BHD" block further down is only the
  receiving party — EMITS must NOT become the detected supplier_name/
  logo/address on that document either.
- Correct obvious OCR/scan spelling noise in the supplier name using
  context — e.g. "COLCRAFT", "COILCRAF", "COILCRAFTT" should all resolve
  to the one real, most plausible full name (e.g. "COILCRAFT SINGAPORE
  PTE LTD"), not be returned verbatim.
- status: "verified" if a supplier could be confidently identified,
  "not_found" if there's no discernible supplier identity at all on the
  document, "uncertain" if something is present but ambiguous (e.g.
  multiple plausible company names, or the identity block is illegible).
{document_type_block}{vendor_hint_block}

VISUAL EVIDENCE — for EACH of company_logo, company_name,
supplier_address, stamp, signature, report a status ("detected" or
"not_detected"), your confidence (0-100), a short `label` describing
specifically what you found (e.g. "Coilcraft red logo mark", "COILCRAFT
SINGAPORE PTE LTD header line"), and a `reason` that explains WHY this
specifically belongs to the SUPPLIER (not just what it looks like) —
e.g. "explicit Supplier Address label, matches extraction hint",
"letterhead logo — this Invoice's own header, issued by the supplier",
or, when something was deliberately rejected because it belongs to the
buyer instead, say so explicitly, e.g. "this is the buyer's own header
logo on this PO, not supplier evidence". Also include a bounding box IF
you can locate it (even when a signal is not_detected but there's a
plausible location for it, e.g. a blank signature line).
- company_logo / supplier_address: MUST belong to the SUPPLIER
  identified above — a logo or address block that belongs to the Ship
  To / Buyer / Customer company must be reported as not_detected here
  (it is not supplier evidence), never substituted in just because it's
  the most visible logo/address on the page.
- supplier_address additionally reports `source_section` — exactly one
  of: "supplier_address_label" (an address block explicitly labeled
  "Supplier Address"/"Vendor Address"), "near_supplier_name" (an address
  immediately next to/below the already-identified supplier name, with
  no explicit label), or "" if not_detected. If genuinely ambiguous
  between two candidate addresses, prefer reporting not_detected over
  guessing.

STRICT BOUNDING BOX RULES — every box must be TIGHT to only that one
element, never a region that also happens to contain other things:

1. company_logo — ONLY the graphical logo/brand symbol itself, nothing
   else — not the company name text, not the address, not a
   registration number, not surrounding whitespace, even if they sit
   right next to the logo. This applies even when the SUPPLIER's logo
   is small and the BUYER's own header logo is much larger/more
   prominent elsewhere on the page — box only the supplier's mark, or
   report not_detected if the supplier has no distinct logo at all;
   never substitute the buyer's logo in.
   Wrong:  a box spanning [logo + "COILCRAFT SINGAPORE PTE LTD" + address]
   Correct: a box tightly around just the red Coilcraft mark/icon.

2. company_name — ONLY the single legal supplier name line, at most one
   or two text lines total (e.g. just "COILCRAFT SINGAPORE PTE LTD", or
   that plus an immediately-adjacent line if the legal name itself
   genuinely wraps onto a second line).
   DO NOT include, even on an adjacent line: "ATTN", any address line,
   phone, email, or ANY buyer/customer/ship-to/bill-to information.
   Wrong:  a box spanning ["COILCRAFT SINGAPORE PTE LTD" + "ATTN:" +
           "EMITS TECHNOLOGY" + address line] — this bundles the
           buyer's name into supplier evidence and is never correct.
   Correct: a box around only the "COILCRAFT SINGAPORE PTE LTD" line.

3. supplier_address — ONLY the supplier's own registered address block,
   found per the priority order described above (an explicitly-labeled
   Supplier Address section first, otherwise an address block sitting
   directly next to the already-identified supplier name).
   DO NOT box the Bill To, Ship To, Delivery Address, or Customer
   address — those belong to the buyer, not the supplier, and must
   never be reported as supplier_address evidence at all, regardless of
   how prominent or well-formatted they are.

4. stamp — ONLY the actual ink/stamp area itself: a physical chop, a
   received stamp, or an official seal (this includes QC/inspection and
   approval-style stamps — see the `type` classification below).
   DO NOT box: printed text, item/part codes in a table, the printed
   abbreviation "CHP" as text, any other printed word, or nearby
   handwritten AP reviewer notes — none of these are a stamp even if
   they sit close to one; the box must tightly surround only the actual
   ink/stamp mark.
   Classify the stamp's `type` as exactly one of: "company_chop"
   (a general round/square company chop), "received_stamp" (a
   "RECEIVED" stamp confirming receipt), "qc_stamp" (a quality-control/
   inspection-passed stamp), "approval_stamp" (an approval/authorized
   stamp) — or "" if not_detected. If a stamp doesn't clearly fit one of
   the four types, pick the closest one rather than leaving it empty.

5. signature — same tightness principle: bound only the handwritten ink
   mark itself, not surrounding labels or whitespace.

- Bounding boxes: normalized to a 0-1000 scale relative to the full
  image, top-left = [0,0], bottom-right = [1000,1000], format
  [ymin, xmin, ymax, xmax] (same convention used elsewhere in this
  system). Omit the box only if no plausible location exists at all.
- signature: NOT every AP document needs one; a missing signature on an
  Invoice or PO is normal (many are computer-generated), not a sign of
  fraud — just report status "not_detected", do not treat it as
  suspicious on its own.

INTEGRITY / TAMPERING — assess four independent risk axes, each
"low", "medium", or "high":
- copy_paste_risk: does any region look like a pasted-in block from a
  different source (mismatched resolution/compression, a rectangle that
  doesn't align with the surrounding layout)? Includes suspicious white
  boxes covering original content and altered totals/dates that look
  pasted over.
- font_consistency: do all text blocks that should share one font
  (e.g. all amounts, all header text) actually look visually
  consistent, or does something stand out as a different font/weight/
  size than its surroundings?
- alignment_consistency: is spacing/alignment consistent with a normal
  printed or scanned document (rows aligned, consistent margins,
  consistent line spacing), or is there abnormal spacing/misalignment
  suggesting a field was re-typed or inserted afterward (e.g. a total or
  date that sits slightly off-baseline from the rest of the row)?
- alteration_risk: any sign of an overwritten number, an altered total
  or date, or re-typed text that doesn't match the surrounding print
  quality?
- reason: one short sentence explaining the overall integrity
  assessment (what you looked at, why it's low/medium/high).
- Do NOT declare a document "fake" or "forged" — only report the four
  risk axes and the reason. Most documents are legitimate, including
  normal scanned/photographed copies with ordinary scan noise, slight
  skew, or compression artifacts — do NOT mark a normal scanned document
  as suspicious for that alone. Only flag medium/high when something is
  visually concrete (a real pasted block, a real overwritten value), not
  merely low scan quality.

Return null/false/0/empty-array defaults for anything you cannot
confidently determine — never guess.

Return ONLY valid JSON, no markdown, no code fences, no explanation —
exactly this structure:
{{
  "supplier_identity": {{
    "status": "verified" or "not_found" or "uncertain",
    "supplier_name": "string or null",
    "logo_detected": true or false,
    "address_detected": true or false,
    "contact_block_detected": true or false
  }},
  "document_visual_evidence": {{
    "company_logo":     {{"status": "detected" or "not_detected", "label": "string", "reason": "string", "confidence": 0-100, "boxes": [ymin, xmin, ymax, xmax] or null}},
    "company_name":      {{"status": "detected" or "not_detected", "label": "string", "reason": "string", "confidence": 0-100, "boxes": [ymin, xmin, ymax, xmax] or null}},
    "supplier_address":  {{"status": "detected" or "not_detected", "source_section": "supplier_address_label" or "near_supplier_name" or "", "label": "string", "reason": "string", "confidence": 0-100, "boxes": [ymin, xmin, ymax, xmax] or null}},
    "stamp":             {{"status": "detected" or "not_detected", "type": "company_chop" or "received_stamp" or "qc_stamp" or "approval_stamp" or "", "label": "string", "reason": "string", "confidence": 0-100, "boxes": [ymin, xmin, ymax, xmax] or null}},
    "signature":         {{"status": "detected" or "not_detected", "label": "string", "reason": "string", "confidence": 0-100, "boxes": [ymin, xmin, ymax, xmax] or null}}
  }},
  "integrity_check": {{
    "copy_paste_risk": "low" or "medium" or "high",
    "font_consistency": "low" or "medium" or "high",
    "alignment_consistency": "low" or "medium" or "high",
    "alteration_risk": "low" or "medium" or "high",
    "reason": "string"
  }},
  "overall_result": {{
    "status": "PASS" or "REVIEW" or "FAIL",
    "risk_level": "LOW" or "MEDIUM" or "HIGH",
    "reasons": ["string", ...]
  }}
}}"""


# v5: document-type-specific supplier-identification priority order,
# injected into {document_type_block} above. An Invoice is ISSUED BY the
# supplier (letterhead = supplier), but a PO is issued BY the buyer and a
# GR is normally issued/stamped by the buyer's own receiving department
# (letterhead = buyer/receiver on both) — treating "the letterhead is the
# supplier" as a universal rule (the pre-v5 prompt's wording) is exactly
# what caused a PO/GR's buyer letterhead (e.g. "EMITS TECHNOLOGY SDN
# BHD") to occasionally get misread as the supplier. PO and GR share the
# same priority order since both need the same buyer-letterhead-is-not-
# supplier correction.
_PO_GR_SUPPLIER_PRIORITY = """
To find the actual supplier on THIS document, use this priority order
(do NOT default to the letterhead/logo at the top of the page — that is
virtually always the BUYER on this document type):
1. An explicit "Supplier Address" / "Vendor Address" section.
2. An explicit "Supplier:" / "Vendor:" name field.
3. The extraction hint below (if provided), cross-checked against any
   supplier name/address field actually visible on the document.
4. A supplier logo — ONLY if a logo distinct from the buyer's own
   top-of-page header logo is visible near the supplier's name/address
   block; never the buyer's own header logo, even if it is the only
   logo on the page.
"""

_DOCUMENT_TYPE_GUIDANCE = {
    'invoice': (
        '\nDOCUMENT TYPE: INVOICE. The supplier ISSUES this document (it is a bill '
        'FROM the supplier TO the buyer) — the letterhead/logo at the very top of '
        'THIS document is normally the supplier. A "Ship To:"/"Bill To:"/"Customer:" '
        'block further down the page is the buyer and must never be used as '
        'supplier evidence.\n'
    ),
    'po': (
        '\nDOCUMENT TYPE: PURCHASE ORDER. The BUYER issues a PO, so the letterhead/'
        'logo at the top of THIS document is virtually always the BUYER, not the '
        'supplier.\n' + _PO_GR_SUPPLIER_PRIORITY
    ),
    'gr': (
        "\nDOCUMENT TYPE: GOODS RECEIPT. Like a PO, a GR is normally issued/stamped "
        "by the BUYER's own receiving department confirming goods arrived — the "
        "letterhead at the top of THIS document is virtually always the BUYER/"
        "receiver, not the supplier.\n" + _PO_GR_SUPPLIER_PRIORITY
    ),
}


def analyze_document_authenticity(image, document_type, extracted_vendor_name=None):
    """Claude Vision visual authenticity check. Makes ONE real Anthropic
    API call. Same fail-soft contract as extract_with_claude(): returns
    the parsed dict on success, or None on any failure (no API key,
    network, timeout, bad JSON) — callers (helpers/authenticity_check.py)
    are responsible for falling back to Gemini when this returns None.

    image: (mime_type, raw_bytes) tuple from prepare_gemini_image_payload().
    document_type: 'invoice' | 'po' | 'gr' — selects the document-type-
      specific supplier-identification priority order injected into the
      prompt (see _DOCUMENT_TYPE_GUIDANCE): an Invoice's own letterhead
      is normally the supplier, but a PO/GR's own letterhead is normally
      the BUYER, so those two need a different priority order to find
      the actual supplier rather than defaulting to the page header.
    extracted_vendor_name: the vendor_name the (separate, already-run)
      extraction pipeline identified for this document, if any — passed
      through as a hint so Claude can cross-check its own visual
      finding against it rather than working blind; Claude is
      explicitly told to verify, not blindly trust, this hint (the
      extraction pipeline can itself be wrong, e.g. from an OCR-noisy
      scan).
    """
    client = _get_claude_client()
    if client is None:
        print("DEBUG AUTHENTICATION AI | skipped: ANTHROPIC_API_KEY not set")
        return None

    if extracted_vendor_name:
        vendor_hint_block = (
            f'\nEXTRACTION HINT: a separate field-extraction pass already read the vendor '
            f'name on this document as "{extracted_vendor_name}". Use this as a hint to help '
            f'resolve ambiguity, but VERIFY it visually rather than blindly trusting it — '
            f'confirm or correct it based on what the letterhead/logo actually show.\n'
        )
    else:
        vendor_hint_block = ''
    document_type_block = _DOCUMENT_TYPE_GUIDANCE.get(document_type, '')
    system_prompt = CLAUDE_AUTHENTICITY_PROMPT.format(
        document_type_block=document_type_block, vendor_hint_block=vendor_hint_block)

    mime_type, image_bytes = image
    user_text = "Analyze this document image for authenticity per the schema in your instructions."

    print(f"DEBUG AUTHENTICATION AI | request | model={Config.CLAUDE_MODEL!r} | "
          f"document_type={document_type!r} | mime={mime_type} | "
          f"image_size_kb={len(image_bytes) / 1024:.1f} | extracted_vendor_name={extracted_vendor_name!r}")

    try:
        response = client.messages.create(
            model=Config.CLAUDE_MODEL,
            max_tokens=2048,
            system=system_prompt,
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
        print(f"DEBUG AUTHENTICATION AI request error: {type(e).__name__}: {e}")
        return None

    text = "".join(block.text for block in response.content if getattr(block, 'type', None) == 'text')
    _raw_preview = text if len(text) <= 3000 else text[:3000] + '...<truncated>'
    print(f"DEBUG AUTHENTICATION AI response | document_type={document_type} | text={_raw_preview!r}")

    try:
        result = json.loads(_strip_markdown_fences(text))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"DEBUG AUTHENTICATION AI response parse error: {type(e).__name__}: {e}")
        return None

    supplier = result.get('supplier_identity') or {}
    evidence = result.get('document_visual_evidence') or {}
    overall = result.get('overall_result') or {}

    def _is_detected(entry):
        # Accepts either the current schema's status string ("detected"/
        # "not_detected") or a legacy boolean, defensively — whichever
        # shape a given response actually used.
        entry = entry or {}
        if 'status' in entry:
            return entry.get('status') == 'detected'
        return bool(entry.get('detected'))

    print("DEBUG AUTH AI RESULT\n"
          f"vendor={supplier.get('supplier_name')}\n"
          f"logo={_is_detected(evidence.get('company_logo'))}\n"
          f"stamp={_is_detected(evidence.get('stamp'))}\n"
          f"signature={_is_detected(evidence.get('signature'))}\n"
          f"tampering={overall.get('risk_level')}")

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
