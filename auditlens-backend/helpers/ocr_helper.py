import re
import os
import base64
import tempfile
import requests
from datetime import datetime
from pdf2image import convert_from_path, pdfinfo_from_path
from config import Config

INVOICE_BLACKLIST = ['account no', 'account number', 'deposit', 'page', 'sub total', 'total']

# Companies that are typically BUYERS (to exclude from vendor detection)
BUYER_KEYWORDS = [
    'northern point', 'invoice to', 'bill to', 'sold to', 'deliver to',
    'ship to', 'attn', 'attention'
]

# On a PO, the letterhead at the top is the BUYER (the company issuing the
# order) — the actual vendor/supplier is named under one of these headings
# instead. Unlike invoice's is_buyer_line() (skip a known-buyer line, take
# the first company match otherwise), PO vendor extraction must not trust
# ANY company match until one of these headings has been seen — see
# extract_po_fields(). Matches "Supplier"/"Supplier Address"/"Vendor"/
# "Bill To Vendor" anywhere in the line, or a bare "To"/"To:" label at the
# very start of the line (but NOT "Bill To"/"Ship To"/"Deliver To", which
# are different recipient fields, not the supplier).
PO_SUPPLIER_HEADING_RE = re.compile(r'\bsupplier\b|\bvendor\b|^to\s*[:\-]?(?:\s|$)', re.IGNORECASE)

# Same idea for a GR: the letterhead is the RECEIVING company, not the
# vendor — the actual supplier is named under one of these headings.
GR_SUPPLIER_HEADING_RE = re.compile(r'\breceived\s*from\b|\bsupplier\b|\bdelivered\s*by\b', re.IGNORECASE)

# Table-header words a line-item description/quantity fallback must never
# grab as if it were the actual value — e.g. "Description" label
# immediately followed by the next column header "Qty" on the next line
# (not real data) rather than an actual product description.
HEADER_WORD_RE = re.compile(
    r'^(?:no\.?|item\s*no\.?|description|particulars|item\s*description|item|'
    r'qty\.?|quantity|unit\s*price\s*(?:\(rm\)|\(myr\))?|price\s*(?:\(rm\)|\(myr\))?|'
    r'amount\s*(?:\(rm\)|\(myr\))?|total\s*(?:\(rm\)|\(myr\))?)\s*$',
    re.IGNORECASE
)

# Line-item table ROW, all on one line: "1  Aluminium Bracket A100  120
# 8.00  960.00" -> row no. (discarded), description, qty (whole number),
# unit price, amount. Kept as a fallback for a document that happens to
# emit one line per row, but confirmed against REAL Google Vision output
# that this is NOT how this app's actual invoices/POs/GRs come back —
# see _extract_first_line_item() below, which is the primary method now.
LINE_ITEM_ROW_RE = re.compile(
    r'^\s*\d+\s+([A-Za-z][\w\s\-\.\/&]*?)\s+(\d+)\s+[\d,]+\.\d+\s+[\d,]+\.\d+\s*$'
)

# Google Vision OCR emits this app's line-item table with EACH CELL on
# its OWN line, confirmed against real OCR output — not one
# space-separated row per item:
#   No / Description / Qty / Unit Price (RM) / Amount (RM) / 1 /
#   Aluminium Bracket A 100 / 120 / 8.00 / 960.00 / 2 / Steel Fastener S200 / ...
_TABLE_HEADER_LINE_RES = [
    re.compile(r'^description$', re.IGNORECASE),
    re.compile(r'^qty\.?$', re.IGNORECASE),
    re.compile(r'^unit\s*price\s*(?:\(rm\)|\(myr\))?$', re.IGNORECASE),
    re.compile(r'^amount\s*(?:\(rm\)|\(myr\))?$', re.IGNORECASE),
]
_INTEGER_LINE_RE = re.compile(r'^\d+$')
_DECIMAL_LINE_RE = re.compile(r'^[\d,]+\.\d+$')


def _extract_first_line_item(lines):
    """
    Primary line-item extraction for this app's actual documents: finds
    the 4 consecutive standalone header lines "Description"/"Qty"/
    "Unit Price (RM)"/"Amount (RM)", then parses the FIRST data row that
    follows — a row-number line, a description line, an integer
    quantity line, then two decimal price/amount lines, each on its own
    line, in that order. Returns (description, quantity), or (None,
    None) if no such table is found or the row doesn't parse cleanly
    (never guesses).
    """
    stripped = [l.strip() for l in lines]
    n = len(stripped)

    header_start = None
    for i in range(n - 3):
        if all(pat.match(stripped[i + j]) for j, pat in enumerate(_TABLE_HEADER_LINE_RES)):
            header_start = i
            break
    if header_start is None:
        return None, None

    idx = header_start + 4
    # Skip a leading row-number line (e.g. "1").
    if idx < n and _INTEGER_LINE_RE.match(stripped[idx]):
        idx += 1

    # Next non-blank, non-header, non-numeric line is the description.
    description = None
    while idx < n:
        line = stripped[idx]
        idx += 1
        if not line or HEADER_WORD_RE.match(line):
            continue
        if _INTEGER_LINE_RE.match(line) or _DECIMAL_LINE_RE.match(line):
            # Hit a number where a description was expected — malformed
            # table for our purposes, bail out rather than guessing.
            return None, None
        if re.search(r'\d+\.\d+\s+[\d,]+\.\d+\s*$', line):
            # ENDS with two decimal-shaped numbers (unit price + amount,
            # e.g. "...  8.00 960.00") — this document isn't actually
            # split one-cell-per-line after all (row number + description
            # + numbers all landed on one line instead). Bail so the
            # per-line LINE_ITEM_ROW_RE / label-based fallbacks can try
            # instead, rather than swallowing the whole row as a
            # "description". Deliberately narrower than "contains any
            # decimal number" — a real component description can
            # legitimately include one (e.g. "Power Inductor 4.7uH 20%
            # SMD"), which must NOT be mistaken for an unsplit row.
            return None, None
        description = line
        break
    if description is None:
        return None, None

    # From here, the first bare integer line (no decimal point) is the
    # quantity — decimal lines (unit price, amount) are skipped over.
    quantity = None
    while idx < n:
        line = stripped[idx]
        if _INTEGER_LINE_RE.match(line):
            quantity = float(line)
            break
        if _DECIMAL_LINE_RE.match(line) or not line:
            idx += 1
            continue
        break

    return description, quantity


# Defensive cap on how many line items a single document can contribute —
# a malformed/garbled document must not be able to produce an unbounded
# result (Render free tier is 512MB).
MAX_LINE_ITEMS = 50

# A line matching any of these ends the line-item table — the totals/
# footer section that follows the last real row.
_LINE_ITEMS_END_RE = re.compile(
    r'\b(?:sub[\s\-]?total|grand\s*total|total|amount\s*due)\b', re.IGNORECASE
)


def _extract_all_line_items(lines, max_items=MAX_LINE_ITEMS):
    """
    Extracts EVERY line-item row from this app's actual document table
    layout (see _TABLE_HEADER_LINE_RES above for the confirmed real
    Google Vision OCR structure — one cell per line), not just the
    first. After the header block, rows repeat: a row-number line,
    description, integer quantity, decimal unit price, decimal amount —
    until a totals/footer line (Subtotal/Total/Grand Total/Amount Due)
    or the end of the document.

    Returns a list of dicts (possibly empty), in table order:
      {'line_no', 'item_code', 'description', 'quantity', 'unit_price', 'amount'}
    item_code is always None here — this app's OCR table has no separate
    item-code column (only row number + description); Gemini's vision
    call is the only source for item_code, since it can read a SKU
    column if the document actually has one.

    The FIRST item's parsing rules mirror _extract_first_line_item()'s
    bail-out behavior (never guess): if the very first row doesn't parse
    cleanly, returns []. A malformed row AFTER at least one item has
    already been found just ends scanning — the items found so far are
    kept rather than discarded.
    """
    stripped = [l.strip() for l in lines]
    n = len(stripped)

    header_start = None
    for i in range(n - 3):
        if all(pat.match(stripped[i + j]) for j, pat in enumerate(_TABLE_HEADER_LINE_RES)):
            header_start = i
            break
    if header_start is None:
        return []

    idx = header_start + 4
    items = []

    while idx < n and len(items) < max_items:
        while idx < n and not stripped[idx]:
            idx += 1
        if idx >= n:
            break

        line = stripped[idx]

        if _LINE_ITEMS_END_RE.search(line):
            break

        # A bare integer here is the row number (1, 2, 3, ...) — skip it,
        # same as _extract_first_line_item()'s "skip a leading row-number
        # line". If a row's number is missing entirely (OCR drop), this
        # simply doesn't fire and the line below is tried as a description
        # instead — no special-casing needed.
        if _INTEGER_LINE_RE.match(line):
            idx += 1
            continue

        if HEADER_WORD_RE.match(line):
            idx += 1
            continue

        # This should be the description — same two bail checks as
        # _extract_first_line_item() (never guess a description).
        if _DECIMAL_LINE_RE.match(line) or re.search(r'\d+\.\d+\s+[\d,]+\.\d+\s*$', line):
            break

        description = line
        idx += 1

        # Quantity: first bare integer after the description.
        quantity = None
        while idx < n:
            l2 = stripped[idx]
            if _INTEGER_LINE_RE.match(l2):
                quantity = float(l2)
                idx += 1
                break
            if _DECIMAL_LINE_RE.match(l2) or not l2:
                idx += 1
                continue
            break
        if quantity is None:
            break

        # Unit price then amount: the next two decimal lines, in order.
        unit_price = None
        amount = None
        while idx < n and amount is None:
            l3 = stripped[idx]
            if _DECIMAL_LINE_RE.match(l3):
                if unit_price is None:
                    unit_price = extract_amount(l3)
                else:
                    amount = extract_amount(l3)
                idx += 1
                continue
            if not l3:
                idx += 1
                continue
            break

        items.append({
            'line_no':     len(items) + 1,
            'item_code':   None,
            'description': description[:200],
            'quantity':    quantity,
            'unit_price':  unit_price,
            'amount':      amount,
        })

    return items


def clean_vendor_name(vendor):
    if not vendor:
        return vendor
    # Remove registration numbers like 242481-M, (605195-A)
    vendor = re.sub(r'\s*[\-]?\s*\d{6,}[\-\w]*', '', vendor)
    vendor = re.sub(r'\s*\(\d{6,}[\-\w]*\)', '', vendor)
    vendor = re.sub(r'\s+', ' ', vendor).strip()
    vendor = vendor.rstrip('.-,')
    return vendor.strip()

def normalize_date_string(date_str):
    if not date_str:
        return date_str
    normalized = date_str.strip()
    month_fixes = {
        r'\b0ct\b': 'Oct', r'\b0an\b': 'Jan', r'\bFe6\b': 'Feb',
        r'\bJan\b': 'Jan', r'\bFeb\b': 'Feb', r'\bMar\b': 'Mar',
        r'\bApr\b': 'Apr', r'\bApri1\b': 'April', r'\bMay\b': 'May',
        r'\bJun\b': 'Jun', r'\bJu1\b': 'Jul', r'\bJul\b': 'Jul',
        r'\bAu9\b': 'Aug', r'\bAug\b': 'Aug', r'\b5ep\b': 'Sep',
        r'\bSep\b': 'Sep', r'\b0ec\b': 'Dec', r'\bDec\b': 'Dec',
        r'\bN0v\b': 'Nov', r'\bNov\b': 'Nov', r'\bOct\b': 'Oct',
    }
    for pattern, replacement in month_fixes.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized

def parse_date(date_str):
    if not date_str:
        return None
    normalized = normalize_date_string(date_str)
    formats = [
        '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y-%m-%d',
        '%d/%b/%Y', '%d %b %Y', '%d %B %Y', '%B %d, %Y',
        '%B %d %Y', '%b %d, %Y', '%b %d %Y', '%d-%b-%Y',
        '%d/%m/%y', '%d-%m-%y', '%d-%b-%y',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt).date()
        except:
            continue
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except:
            continue
    return None

def is_valid_invoice_number(val):
    if not val:
        return False
    val_lower = val.lower()
    invalid_words = ['date', 'invoice', 'receipt', 'total', 'amount', 'page',
                     'account', 'deposit', 'sub', 'no', 'to', 'from', 'by']
    if val_lower in invalid_words:
        return False
    for b in INVOICE_BLACKLIST:
        if b in val_lower:
            return False
    if not re.search(r'[A-Za-z0-9]', val):
        return False
    if len(val) < 3:
        return False
    return True

def extract_amount(text):
    match = re.search(r'([\d,]+\.?\d*)', str(text))
    if match:
        try:
            val = float(match.group(1).replace(',', ''))
            if val > 0 and val < 10000000:
                return val
        except:
            pass
    return None


def split_item_code_prefix(text):
    """
    Splits a leading item-code-shaped token off a line-item description,
    e.g. "SLT-MOS-N60R MOSFET N-Ch 600V TO-220" -> ("SLT-MOS-N60R",
    "MOSFET N-Ch 600V TO-220"). This app's document tables put the item
    code as the first token of the Description cell when a code exists
    at all — but WHICH extraction path produced a given line item
    (Gemini vision, which sometimes splits the code into its own field
    and sometimes leaves it inline; the regex fallback, which never
    splits) is inconsistent, and the SAME product extracted via two
    different paths (e.g. invoice via Gemini, PO via regex fallback)
    would otherwise compare as two different items during 3-way
    matching. Called uniformly on every line item regardless of source
    so item_code/description end up in the same shape either way.

    A token counts as code-shaped only if it contains a hyphen AND a
    digit (e.g. "SLT-MOS-N60R", "MTC-IND-4R7M") — this rules out an
    ordinary hyphenated word (no digit) accidentally being split off a
    plain description. Returns (item_code, remaining_description); if
    the text doesn't start with a code-shaped token, returns
    (None, text) unchanged.
    """
    if not text:
        return None, text
    parts = text.split(None, 1)
    if len(parts) != 2:
        return None, text
    token, rest = parts
    if '-' not in token or not re.match(r'^[A-Za-z0-9-]+$', token):
        return None, text
    if not re.search(r'\d', token):
        return None, text
    if not re.match(r'^[A-Za-z]', rest):
        return None, text
    return token.upper(), rest.strip()


def normalize_line_item_code(item):
    """
    Ensures a single line-item dict's item_code/description end up in a
    consistent shape via split_item_code_prefix() — called uniformly on
    every extracted item (Gemini AND regex fallback, invoice AND PO AND
    GR) right before persistence, which is what actually guarantees the
    same product compares equal across documents regardless of which
    extraction path produced each side. Mutates and returns `item`.

    If item_code is already set (e.g. Gemini split it out itself) and
    description STILL starts with that same code (Gemini put it in
    both places), the duplicate is stripped from description too. If
    item_code is unset, a code-shaped prefix is split out of
    description into item_code.
    """
    desc = (item.get('description') or '').strip()
    code = (item.get('item_code') or '').strip() or None
    prefix_code, remainder = split_item_code_prefix(desc)

    if code:
        norm_code = re.sub(r'[^A-Za-z0-9]', '', code).upper()
        norm_prefix = re.sub(r'[^A-Za-z0-9]', '', prefix_code).upper() if prefix_code else None
        if norm_prefix and norm_prefix == norm_code:
            item['description'] = remainder
    elif prefix_code:
        item['item_code'] = prefix_code
        item['description'] = remainder

    return item


def is_buyer_line(line):
    line_lower = line.lower()
    for keyword in BUYER_KEYWORDS:
        if keyword in line_lower:
            return True
    return False

def run_google_vision_ocr(image_path):
    with open(image_path, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode('utf-8')

    url = f"https://vision.googleapis.com/v1/images:annotate?key={Config.GOOGLE_VISION_API_KEY}"
    payload = {
        "requests": [{
            "image": {"content": image_data},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]
        }]
    }

    response = requests.post(url, json=payload)
    result   = response.json()

    if 'responses' not in result or not result['responses']:
        return '', 0.0

    response_data = result['responses'][0]

    if 'fullTextAnnotation' not in response_data:
        return '', 0.0

    full_text  = response_data['fullTextAnnotation'].get('text', '')
    confidence = 0.0

    try:
        pages = response_data['fullTextAnnotation'].get('pages', [])
        if pages:
            confidences = []
            for page in pages:
                for block in page.get('blocks', []):
                    conf = block.get('confidence', 0)
                    if conf > 0:
                        confidences.append(conf)
            if confidences:
                confidence = round(sum(confidences) / len(confidences) * 100, 2)
    except:
        confidence = 85.0

    print(f"DEBUG OCR TEXT: {full_text[:500]}".encode('ascii', errors='replace').decode('ascii'))
    return full_text, confidence


def run_ocr(file_path, file_ext):
    all_results = []
    total_text  = ''
    confidence  = 0.0

    try:
        if file_ext == 'pdf':
            # TEMP-DEBUG memory fix: convert ONE page at a time instead of
            # calling convert_from_path() once for the whole document.
            # Without first_page/last_page, convert_from_path() has poppler
            # rasterize and return EVERY page as a full-resolution PIL Image
            # in one list, all resident simultaneously (at dpi=300 — higher
            # than the Gemini-side render) — for a multi-page document this
            # upfront peak (~26MB/A4 page x page count) happens before the
            # per-page loop below even starts, regardless of how quickly
            # each image is closed once inside the loop. pdfinfo_from_path()
            # only reads PDF metadata (page count) — it does not rasterize
            # anything, so it adds negligible memory/time cost.
            page_count = pdfinfo_from_path(file_path, poppler_path=Config.POPPLER_PATH).get('Pages', 1)
            all_confidences = []

            for page_num in range(1, page_count + 1):
                # first_page=last_page=page_num -> poppler renders and
                # returns only THIS page; at most one page's bitmap is ever
                # resident, instead of all `page_count` of them at once.
                page_images = convert_from_path(
                    file_path, poppler_path=Config.POPPLER_PATH, dpi=300,
                    first_page=page_num, last_page=page_num,
                )
                img = page_images[0]

                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    tmp_path = tmp.name
                img.save(tmp_path, 'PNG')
                # Once this page's pixels are saved to its temp file above,
                # the in-memory PIL object is never read again
                # (run_google_vision_ocr() reads back from tmp_path, not
                # from `img`) — release it and the single-page result list
                # immediately rather than waiting for the next loop
                # iteration to reassign `page_images`.
                img.close()
                del page_images

                page_text, page_conf = run_google_vision_ocr(tmp_path)
                total_text += page_text + '\n'
                if page_conf > 0:
                    all_confidences.append(page_conf)

                try:
                    os.unlink(tmp_path)
                except:
                    pass

            if all_confidences:
                confidence = round(sum(all_confidences) / len(all_confidences), 2)

        else:
            total_text, confidence = run_google_vision_ocr(file_path)

        if total_text.strip():
            all_results = [([[0,0],[1,0],[1,1],[0,1]], total_text, confidence/100)]

    except Exception as e:
        print(f"Google Vision OCR error: {e}")

    return all_results, total_text, confidence


def calculate_confidence(ocr_results):
    if not ocr_results:
        return 0.0
    confidences = [r[2] for r in ocr_results if len(r) >= 3 and r[2] > 0]
    if confidences:
        return round(sum(confidences) / len(confidences) * 100, 2)
    return 0.0


def extract_fields(ocr_text):
    fields = {
        'invoice_number':   None,
        'vendor_name':      None,
        'invoice_date':     None,
        'total_amount':     None,
        'tax_amount':       None,
        # 3-way audit comparison fields (Field Comparison table: PO Ref,
        # Item/Description, Quantity) — regex-only, best-effort, no
        # Gemini call. po_reference is the PO number THIS invoice bills
        # against (not this invoice's own number).
        'po_reference':     None,
        'item_description': None,
        'quantity':         None,
        'currency':         None,
        # EVERY line item (not just the first) — see _extract_all_line_
        # items() — for line-item-level 3-way audit matching. item_
        # description/quantity above stay the FIRST item only, kept for
        # backward compatibility with anything still reading them.
        'line_items':       [],
    }

    lines = ocr_text.split('\n')
    full_text_lower = ocr_text.lower()

    fields['line_items'] = _extract_all_line_items(lines)

    # Primary line-item extraction — Google Vision emits this app's
    # tables with each cell on its own line (confirmed against real OCR
    # output), so this must run over the whole line list, not per-line
    # inside the main loop below (which still has a same-line fallback
    # for simpler non-tabular invoices).
    item_desc, item_qty = _extract_first_line_item(lines)
    if item_desc:
        fields['item_description'] = item_desc
    if item_qty is not None:
        fields['quantity'] = item_qty

    # ── Track if we passed "Invoice To" section ──────────
    passed_invoice_to = False
    invoice_to_index  = -1
    for i, line in enumerate(lines):
        if re.search(r'invoice\s*to|bill\s*to|sold\s*to', line, re.IGNORECASE):
            invoice_to_index = i
            break

    total_candidates = []
    usd_total_candidates = []
    # Once an "EXCHANGE RATE" marker is seen anywhere in the document, every
    # subsequent generic "Total"/"Sub Total" line is almost certainly the
    # exchange-rate-converted value, not the real total — Google Vision OCR
    # commonly splits "TOTAL (US$) 8,020.00" onto separate lines from the
    # exchange-rate/converted-total block that follows it, so a same-line-
    # only check isn't enough (see the currency-tagged TOTAL AMOUNT block
    # below). This document-level flag suppresses RM/generic total
    # candidates found AFTER that point, regardless of line-splitting.
    seen_exchange_rate = False

    for i, line in enumerate(lines):
        line_clean = line.strip()
        next_line  = lines[i + 1].strip() if i + 1 < len(lines) else ''

        # ── VENDOR NAME ───────────────────────────────────
        # Strategy: Find company name that is NOT the buyer
        if fields['vendor_name'] is None:
            # Look for company names (Sdn Bhd, Ltd, etc.)
            company_match = re.search(
                r'^([\w\s&\.\-\(\)\/]+(?:sdn\.?\s*bhd\.?|berhad|corporation|corp|ltd\.?|enterprise|trading|network|logistics|broadband|applications)[\w\s&\.\-\(\)\/\-]*)',
                line_clean, re.IGNORECASE
            )
            if company_match:
                vendor = company_match.group(1).strip()
                # Skip if this line is part of buyer section
                if (len(vendor) > 5 and
                    not is_buyer_line(line_clean) and
                    not is_buyer_line(lines[i-1] if i > 0 else '') and
                    not re.search(r'\b(customer|invoice\s*to|bill\s*to|deliver\s*to|ship\s*to)\b',
                                  ' '.join(lines[max(0,i-2):i]), re.IGNORECASE)):
                    fields['vendor_name'] = clean_vendor_name(vendor)

        # ── PO REFERENCE ──────────────────────────────────
        # The PO number this invoice is billing against (not this
        # invoice's own number) — the anchor field for the 3-way audit
        # comparison. Requires an explicit ":"/"-" after the label so a
        # bare "PO" mention elsewhere on the page can't false-positive.
        if fields['po_reference'] is None:
            match = re.search(
                r'\b(?:p\.?o\.?|purchase\s*order)\s*(?:no\.?|number|ref\.?)?\s*[:\-]\s*([A-Za-z0-9\-\/]+)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = match.group(1).strip()
                if len(val) > 2:
                    fields['po_reference'] = val

        # ── LINE ITEM: description + qty from the SAME table row ──
        # Tabular layout: "1  Aluminium Bracket A100  120  8.00  960.00"
        # -> row no. (discarded), description, qty, unit price, amount.
        # Tried before the label-based fallbacks below — pulling both
        # fields from the same row keeps them internally consistent
        # (never description from one row and qty from a different one),
        # and this is the primary layout our invoices/POs/GRs actually
        # use, not a "Description:"/"Qty:" label form.
        if fields['item_description'] is None or fields['quantity'] is None:
            row_match = LINE_ITEM_ROW_RE.match(line_clean)
            if row_match:
                if fields['item_description'] is None:
                    desc_val = row_match.group(1).strip()
                    if len(desc_val) > 2:
                        fields['item_description'] = desc_val[:200]
                if fields['quantity'] is None:
                    try:
                        qty_val = float(row_match.group(2))
                        if qty_val > 0:
                            fields['quantity'] = qty_val
                    except ValueError:
                        pass

        # ── ITEM / DESCRIPTION (label-based fallback, e.g. a
        # "Description: Widget XYZ" line on a non-tabular document).
        # HEADER_WORD_RE guards against grabbing the NEXT column header
        # (e.g. "Qty") when "Description" is itself just a column header
        # with no inline value, not a real label:value line. ──
        if fields['item_description'] is None:
            label_match = re.search(
                r'^(?:description|particulars|item\s*description|item)\s*[:\-]?\s*(.*)$',
                line_clean, re.IGNORECASE
            )
            if label_match:
                inline_val = label_match.group(1).strip()
                if len(inline_val) > 2 and not HEADER_WORD_RE.match(inline_val):
                    fields['item_description'] = inline_val[:200]
                elif next_line and len(next_line) > 2 and not HEADER_WORD_RE.match(next_line):
                    fields['item_description'] = next_line[:200]

        # ── QUANTITY (label-based fallback, e.g. "Qty: 100") ──────
        # THE key 3-way audit field: PO ordered vs GR received vs
        # Invoice billed.
        if fields['quantity'] is None:
            match = re.search(r'\b(?:qty|quantity)\.?\s*[:\-]?\s*(\d+(?:\.\d+)?)\b', line_clean, re.IGNORECASE)
            if match:
                try:
                    qty_val = float(match.group(1))
                    if qty_val > 0:
                        fields['quantity'] = qty_val
                except ValueError:
                    pass

        # ── INVOICE NUMBER ────────────────────────────────
        if fields['invoice_number'] is None:
            # Skip lines that are clearly not invoice number labels
            if re.search(r'\b(?:po|p\.o|gr|goods\s*receipt|reg\.?|account|tel|fax|bank|acct)\s*(?:no\.?|number)\s*[:\-]', line_clean, re.IGNORECASE):
                pass  # skip invoice number extraction on this line
            else:
                inv_patterns = [
                    # E-invoice
                    r'e-invoice\s*no\.?\s*[:\-]?\s*(SIN\d+)',
                    r'\b(SIN\d{6,})\b',
                    # Standard No: XXXXX pattern (generic - works for any invoice)
                    r'^No\s*[:\-]\s*([A-Z]{2,}[A-Z0-9\-\/]+)',
                    r'No\s*:\s*([A-Z]{2,}[A-Z0-9\-\/]+)',
                    # Invoice No patterns
                    r'invoice\s*no\.?\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    r'invoice\s*number\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    r'receipt\s*(?:no\.?|number)\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    r'booking\s*id\s*[:\-]?\s*([A-Z0-9\-]+)',
                    r'bill\s*(?:no\.?|number)\s*[:\-]?\s*(\S+)',
                    r'invoice\s*(?:no\.?|#)\s*[:\-]?\s*(\S+)',
                    # Bare "INVOICE:" label with no "No"/"Number"/"#" suffix
                    # (real-world layout, e.g. "INVOICE:  IX107587") — the
                    # \s* between "invoice" and ":" requires the colon to
                    # come right after the word, so "Invoice No:"/"Invoice
                    # Date:" never match this (they have a word in between).
                    r'\binvoice\s*:\s*([A-Za-z0-9][A-Za-z0-9\-\/]*)',
                ]
                for p in inv_patterns:
                    match = re.search(p, line_clean, re.IGNORECASE)
                    if match:
                        val = match.group(1).strip().rstrip('-').strip()
                        # Only strip a trailing "- N" as a page-suffix artifact
                        # (e.g. "INV12345 - 3") when it's whitespace-separated -
                        # a hyphen with no surrounding spaces is part of the
                        # invoice number itself (e.g. "INV-SE-2025-1001").
                        val = re.sub(r'\s+-\s+\d+$', '', val).strip()
                        if is_valid_invoice_number(val):
                            fields['invoice_number'] = val
                            break

                # Label only, value on next line
                if fields['invoice_number'] is None:
                    if re.search(r'^(?:invoice\s*(?:no\.?|number|#)|inv\s*(?:no\.?|#)|receipt\s*(?:no\.?|number))\s*[:\-]\s*$',
                                 line_clean, re.IGNORECASE):
                        if next_line and is_valid_invoice_number(next_line.strip()):
                            fields['invoice_number'] = next_line.strip()

        # ── INVOICE DATE ──────────────────────────────────
        if fields['invoice_date'] is None:
            date_patterns = [
                # E-invoice
                r'e-invoice\s*date.*?[:\-]\s*(\d{2}-\d{2}-\d{4})',
                # Standard date patterns
                r'(?:invoice|receipt|bill)\s*date\s*[:\-]?\s*(\d{1,2}[-\/]\w+[-\/]\d{2,4})',
                r'(?:invoice|receipt|bill)\s*date\s*[:\-]?\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})',
                r'(?:invoice|receipt|bill)\s*date\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{4})',
                r'invoice\s*due\s*date\s*[:\-]?\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})',
                # Generic date label
                r'^date\s*[:\-]\s*(\d{1,2}[-\/]\w+[-\/]\d{2,4})',
                r'^date\s*[:\-]\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})',
                r'^date\s*[:\-]\s*(\d{1,2}\s+\w+\s+\d{4})',
                r'^date\s*[:\-]\s*(\w+\s+\d{1,2},?\s+\d{4})',
                # Date/Time format
                r'date\/time\s*[:\-]?\s*(\d{1,2}[-\/]\w+[-\/]\d{4})',
                r'date\/time\s*[:\-]?\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{4})',
            ]
            for p in date_patterns:
                match = re.search(p, line_clean, re.IGNORECASE)
                if match:
                    raw_date = match.group(1).strip()
                    if len(raw_date) > 4:
                        fields['invoice_date'] = normalize_date_string(raw_date)
                        break

        # ── TOTAL AMOUNT (currency-tagged) ─────────────────
        # Real invoices sometimes show BOTH an original-currency total
        # (e.g. "TOTAL (US$) 8,020.00") and a converted local-currency
        # total via an exchange-rate line (e.g. "EXCHANGE RATE=1.2670
        # ... TOTAL= 10,161.34 (RM)"). The real transaction amount is
        # always the ORIGINAL-currency one, never the converted value —
        # so a USD-tagged total is collected separately and takes
        # unconditional priority below, and a line describing the
        # conversion itself is never treated as a candidate total.
        is_exchange_rate_line = re.search(r'exchange\s*rate', line_clean, re.IGNORECASE)
        if is_exchange_rate_line:
            seen_exchange_rate = True
        if not is_exchange_rate_line:
            # Same-line form: "TOTAL (US$) 8,020.00".
            currency_total_match = re.search(
                r'\btotal\s*\(\s*(us\$|usd|rm|myr)\s*\)\s*[:\-]?\s*([\d,]+\.?\d*)',
                line_clean, re.IGNORECASE
            )
            if currency_total_match:
                tag = currency_total_match.group(1).upper().replace('$', '')
                val = extract_amount(currency_total_match.group(2))
                if val and val > 1:
                    if tag in ('US', 'USD'):
                        usd_total_candidates.append(val)
                    else:
                        total_candidates.append(val)

            # Two-line form: Google Vision OCR commonly puts the label and
            # its value on SEPARATE lines (confirmed elsewhere in this app
            # for table headers) — a bare "TOTAL (US$)"/"TOTAL (RM)" label
            # line, value on the next line. Checked regardless of
            # seen_exchange_rate: the original-currency total legitimately
            # appears anywhere, including before the exchange-rate block.
            currency_label_only = re.search(r'^total\s*\(\s*(us\$|usd|rm|myr)\s*\)\s*$', line_clean, re.IGNORECASE)
            if currency_label_only:
                tag = currency_label_only.group(1).upper().replace('$', '')
                val = extract_amount(next_line)
                if val and val > 1:
                    if tag in ('US', 'USD'):
                        usd_total_candidates.append(val)
                    else:
                        total_candidates.append(val)

            # Bare-word form (no parentheses): "Total amount: USD 8,020.00",
            # "Grand Total USD 8,020.00" — as common on real invoices as the
            # parenthesized "TOTAL (US$)" form above; previously unmatched
            # by any pattern, so a document using this layout silently
            # produced no total_amount at all despite the value being
            # clearly printed and labeled.
            bare_usd_total_match = re.search(
                r'\b(?:total\s*(?:amount)?|grand\s*total|amount\s*due)\s*[:\-]?\s*(us\$|usd)\s*([\d,]+\.?\d*)',
                line_clean, re.IGNORECASE
            )
            if bare_usd_total_match:
                val = extract_amount(bare_usd_total_match.group(2))
                if val and val > 1:
                    usd_total_candidates.append(val)

            # Amount BEFORE the currency tag, e.g. "8,020.00 USD" — no
            # "total"/"amount due" label required on the line itself (some
            # invoices print just the tagged amount). Mirrors the same
            # currency-order fix already applied to extract_po_fields().
            amount_then_usd_match = re.search(
                r'([\d,]+\.?\d*)\s*(us\$|usd)\b', line_clean, re.IGNORECASE
            )
            if amount_then_usd_match:
                val = extract_amount(amount_then_usd_match.group(1))
                if val and val > 1:
                    usd_total_candidates.append(val)

        # ── TOTAL AMOUNT ──────────────────────────────────
        # Collect every "total"-labeled amount on the invoice (skipping
        # Subtotal/Sub Total lines) instead of stopping at the first match —
        # OCR line order doesn't guarantee Subtotal comes before Total, and
        # "Sub Total (RM)" (with a space) would otherwise match the same
        # \btotal patterns as the real "Total (RM)" line. The true total is
        # always the largest of these (Total = Subtotal + tax), so the max
        # is taken once the whole line loop finishes. Suppressed entirely
        # once an exchange-rate marker has been seen anywhere earlier in
        # the document — a generic "Total"/"Sub Total" line found after
        # that point is the converted value, not the real total (the real,
        # original-currency total is captured separately above).
        if (not re.search(r'\bsub[\s\-]?total\b', line_clean, re.IGNORECASE)
                and not is_exchange_rate_line and not seen_exchange_rate):
            amount_patterns = [
                r'\btotal\s*\(\s*(?:rm|myr)\s*\)\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*net\s*amount\s*(?:\(rm\))?\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*payable\s*amount\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*including\s*(?:tax|sst|gst)\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*\(?\s*incl\.?(?:uding)?\s*(?:tax|sst|gst)?\s*\)?\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*sales\s*\(inclusive\s*of\s*sst\)\s*([\d,]+\.?\d*)',
                r'total\s*paid\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'total\s*amount\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'total\s*current\s*charges\s*(?:myr|rm)?\s*([\d,]+\.?\d*)',
                r'grand\s*total\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'amount\s*due\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'^total\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)$',
                r'^amount\s*:\s*([\d,]+\.?\d*)$',
            ]
            for p in amount_patterns:
                match = re.search(p, line_clean, re.IGNORECASE)
                if match:
                    val = extract_amount(match.group(1))
                    if val and val > 1:
                        total_candidates.append(val)
                    break

            if re.search(r'^total\s*$', line_clean, re.IGNORECASE):
                val = extract_amount(next_line)
                if val and val > 1:
                    total_candidates.append(val)

        # ── TAX AMOUNT ────────────────────────────────────
        if fields['tax_amount'] is None:
            tax_patterns = [
                r'service\s*tax\s*\(\d+%.*?\)\s*([\d,]+\.?\d*)',
                r'service\s*tax\s*@\s*\d+%.*?([\d,]+\.?\d*)',
                # "SST 8% (RM): 145.60" — the %-then-currency gap may be
                # wrapped in parens ("(RM)"), which the currency group must
                # tolerate or it backtracks onto the bare percentage digit.
                r'(?:service\s*tax|sst|gst)\s*[:\-@]?\s*\d*%?\s*\(?\s*(?:rm|myr)?\s*\)?\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*tax\s*amount\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'tax\s*amount\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                # Bare "Tax:" label (anchored so it doesn't fire mid-line,
                # e.g. inside "Tax Invoice No.")
                r'^tax\s*[:\-]\s*(?:rm|myr)?\s*([\d,]+\.?\d*)$',
            ]
            for p in tax_patterns:
                match = re.search(p, line_clean, re.IGNORECASE)
                if match:
                    val = extract_amount(match.group(1))
                    if val is not None:
                        fields['tax_amount'] = val
                        break

    if usd_total_candidates:
        fields['total_amount'] = max(usd_total_candidates)
        fields['currency'] = 'USD'
        _total_reason = 'largest USD-tagged candidate (priority over any MYR/RM candidate)'
    elif total_candidates:
        fields['total_amount'] = max(total_candidates)
        fields['currency'] = 'MYR'
        _total_reason = 'largest "total"-labeled candidate found (no USD-tagged candidate present)'
    else:
        _total_reason = 'no candidate found'
    print(f"DEBUG Invoice total_amount candidates: usd={usd_total_candidates} rm/other={total_candidates}\n"
          f"Selected: {fields['total_amount']} ({_total_reason})")  # TEMP-DEBUG

    # ══════════════════════════════════════════════════════
    # FALLBACKS — Generic, works for any invoice
    # ══════════════════════════════════════════════════════

    # Invoice Number fallback — generic No: XXXXX
    if fields['invoice_number'] is None:
        for pattern in [
            r'\b(SIN\d{6,})\b',
            r'No\s*[:\-]\s*([A-Z]{2,}[A-Z0-9\-\/]+)',
            r'(?:invoice|receipt|bill)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
            r'receipt\s*no\.?\s*[:\.]?\s*(\d+)',
            r'booking\s*id\s*[:\-]?\s*([A-Za-z0-9\-]+)',
        ]:
            match = re.search(pattern, ocr_text, re.IGNORECASE)
            if match:
                val = match.group(1).strip()
                if is_valid_invoice_number(val):
                    fields['invoice_number'] = val
                    break

    # Vendor fallback — find first company name not in buyer section
    if fields['vendor_name'] is None:
        all_companies = re.finditer(
            r'([\w\s&\.\-\(\)\/]+(?:sdn\.?\s*bhd\.?|berhad|enterprise|corporation|corp|ltd\.?|trading|network|logistics|broadband)[\w\s&\.\-\(\)\/\-]*)',
            ocr_text, re.IGNORECASE
        )
        for m in all_companies:
            vendor = m.group(1).strip()
            # Check surrounding context — skip if buyer
            start = max(0, m.start() - 50)
            context = ocr_text[start:m.start()].lower()
            if not any(k in context for k in ['invoice to', 'bill to', 'deliver to', 'ship to', 'sold to']):
                if len(vendor) > 5:
                    fields['vendor_name'] = clean_vendor_name(vendor)
                    break

    # Total Amount fallback
    if fields['total_amount'] is None:
        for pattern in [
            r'\btotal\s*\(\s*(?:rm|myr)\s*\)\s*[:\-]?\s*([\d,]+\.?\d*)',
            r'total\s*net\s*amount\s*(?:\(rm\))?\s*[:\-]?\s*([\d,]+\.?\d*)',
            r'\btotal\s*(?:paid|amount|charges?|due|payable|net)\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
            r'grand\s*total\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
            r'(?:rm|myr)\s*:?\s*([\d,]+\.?\d*)\s*$',
            r'cash\s*amt\s*[:\-]?\s*([\d,]+\.?\d*)',
        ]:
            match = re.search(pattern, ocr_text, re.IGNORECASE | re.MULTILINE)
            if match:
                val = extract_amount(match.group(1))
                if val and val > 1:
                    fields['total_amount'] = val
                    break

    # Invoice Date fallback
    if fields['invoice_date'] is None:
        # Try month name format: 28-May-2025, 28 May 2025
        month_abbr = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'
        month_full = r'(?:january|february|march|april|may|june|july|august|september|october|november|december)'
        for pattern in [
            rf'(\d{{1,2}}[-\s]{month_abbr}[-\s]\d{{4}})',
            rf'(\d{{1,2}}[-\s]{month_full}[-\s]\d{{4}})',
            rf'(\d{{1,2}}\s+{month_full}\s+\d{{4}})',
            r'(\d{1,2}\/\d{1,2}\/\d{4})',
            r'(\d{1,2}-\d{1,2}-\d{4})',
            r'(\d{4}-\d{2}-\d{2})',
        ]:
            match = re.search(pattern, ocr_text, re.IGNORECASE)
            if match:
                fields['invoice_date'] = normalize_date_string(match.group(1).strip())
                break
                # Clean up wrong invoice number
    if fields['invoice_number'] and any(x in str(fields['invoice_number']) for x in ['Invoice To', 'N0016', 'invoice to', 'Bill To', 'Deliver To']):
        fields['invoice_number'] = None

    # Generic No: fallback - handle value separated by other lines
    if fields['invoice_number'] is None:
        lines_list = ocr_text.split('\n')
        for i, line in enumerate(lines_list):
            if re.search(r'^No\s*:\s*$', line.strip()):
                # Check next 3 lines for invoice number
                for j in range(1, 4):
                    if i + j < len(lines_list):
                        next_val = lines_list[i + j].strip()
                        if (next_val and
                            is_valid_invoice_number(next_val) and
                            not re.search(r'^(date|deliver|invoice|northern|unit|gurney|penang|tel|fax|attn)$',
                                         next_val, re.IGNORECASE) and
                            re.search(r'[A-Z]{2,}', next_val)):
                            fields['invoice_number'] = next_val
                            break

    # Note: no Gemini call here — extract_fields() is regex-only. The
    # single merged Gemini vision call (fields + authenticity in one
    # request) happens in routes/documents.py's upload_document() and
    # overrides these regex values when it succeeds, so an invoice
    # upload never makes more than one Gemini call.
    print(f"DEBUG extracted fields: {fields}")
    return fields


def extract_po_fields(ocr_text):
    fields = {
        'po_number':        None,
        'vendor_name':      None,
        'po_date':          None,
        'total_amount':     None,
        'currency':         'MYR',
        # 3-way audit comparison fields — po_number above IS the PO's
        # own anchor reference, so no separate po_reference field here.
        'item_description': None,
        'quantity':         None,
        # EVERY line item (not just the first) — see extract_fields().
        'line_items':       [],
    }

    lines = ocr_text.split('\n')
    total_candidates = []
    usd_total_candidates = []
    # (value, source_label, accepted) — every po_number attempt across all
    # four extraction passes below, for debug visibility into what was
    # tried vs. what actually got selected (and why a candidate was
    # rejected, e.g. a label word echoed back as the value).
    po_number_candidates = []
    # On a PO the letterhead at the top is the BUYER (the company issuing
    # the order), never the vendor — the actual supplier is named under a
    # "Supplier"/"Vendor"/"To" heading further down. Vendor extraction
    # below is gated on this flag so it never grabs the letterhead: it
    # only starts trying once a supplier heading has actually been seen.
    seen_supplier_heading = False

    fields['line_items'] = _extract_all_line_items(lines)

    # Primary line-item extraction — see extract_fields() for why this
    # runs over the whole line list up front rather than per-line below.
    item_desc, item_qty = _extract_first_line_item(lines)
    if item_desc:
        fields['item_description'] = item_desc
    if item_qty is not None:
        fields['quantity'] = item_qty

    # ── PO NUMBER: "Document No."/"Doc No." gets DOCUMENT-WIDE priority
    # over the generic "PO ..." pattern in the per-line loop below,
    # checked in its own pass over every line (not gated to whichever
    # line the main loop happens to reach first). Real POs often also
    # carry a DIFFERENT field like "PO Ref No: 400-C008" (a buyer-side
    # reference/cost-center code, not the PO's own document number) —
    # if that line appears earlier in the document than "Document No:
    # PO3006000", the per-line loop would already have set po_number
    # from the wrong field by the time it reached the right one. Doing
    # this as a separate, first, whole-document pass means the correct
    # label wins regardless of line order.
    for _i, _line in enumerate(lines):
        _line_clean = _line.strip()
        _doc_no_match = re.search(
            r'\b(?:document|doc)\.?\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
            _line_clean, re.IGNORECASE
        )
        if _doc_no_match:
            _val = _doc_no_match.group(1).strip()
            # Reject a bare label word being echoed back as the value
            # (e.g. matching into an adjacent "...No: Ref..." fragment) —
            # same class of bug the Gemini-side prompt and the validator
            # both already guard against.
            if len(_val) > 2 and _val.lower() not in ('ref', 'no', 'number'):
                po_number_candidates.append((_val, 'Document No. (same line)', True))
                fields['po_number'] = _val
                break
            po_number_candidates.append((_val, 'Document No. (same line, rejected label-echo)', False))
            continue

        # Two-line form: Google Vision OCR commonly puts the label and its
        # value on SEPARATE lines for table/box-style layouts (same
        # pattern already relied on elsewhere in this codebase, e.g. the
        # invoice's "TOTAL (US$)" / value-on-next-line handling) — a bare
        # "Document No." / "Doc No." label line, with "PO3006000" as the
        # very next line and nothing else on the label line to capture.
        if re.search(r'^(?:document|doc)\.?\s*(?:no\.?|number|#)\s*$', _line_clean, re.IGNORECASE):
            _next = lines[_i + 1].strip() if _i + 1 < len(lines) else ''
            if len(_next) > 2 and re.match(r'^[A-Za-z0-9\-\/]+$', _next) and _next.lower() not in ('ref', 'no', 'number'):
                po_number_candidates.append((_next, 'Document No. (two-line, value on next line)', True))
                fields['po_number'] = _next
                break
            elif _next:
                po_number_candidates.append((_next, 'Document No. (two-line, rejected)', False))

    for i, line in enumerate(lines):
        line_clean = line.strip()
        next_line  = lines[i + 1].strip() if i + 1 < len(lines) else ''

        # ── LINE ITEM: description + qty from the SAME table row ──
        # Tabular layout: "1  Aluminium Bracket A100  120  8.00  960.00"
        # -> row no. (discarded), description, qty, unit price, amount.
        # Tried before the label-based fallbacks below — pulling both
        # fields from the same row keeps them internally consistent.
        if fields['item_description'] is None or fields['quantity'] is None:
            row_match = LINE_ITEM_ROW_RE.match(line_clean)
            if row_match:
                if fields['item_description'] is None:
                    desc_val = row_match.group(1).strip()
                    if len(desc_val) > 2:
                        fields['item_description'] = desc_val[:200]
                if fields['quantity'] is None:
                    try:
                        qty_val = float(row_match.group(2))
                        if qty_val > 0:
                            fields['quantity'] = qty_val
                    except ValueError:
                        pass

        # ── ITEM / DESCRIPTION (label-based fallback). HEADER_WORD_RE
        # guards against grabbing the NEXT column header (e.g. "Qty")
        # when "Description" is itself just a column header. ──
        if fields['item_description'] is None:
            label_match = re.search(
                r'^(?:description|particulars|item\s*description|item)\s*[:\-]?\s*(.*)$',
                line_clean, re.IGNORECASE
            )
            if label_match:
                inline_val = label_match.group(1).strip()
                if len(inline_val) > 2 and not HEADER_WORD_RE.match(inline_val):
                    fields['item_description'] = inline_val[:200]
                elif next_line and len(next_line) > 2 and not HEADER_WORD_RE.match(next_line):
                    fields['item_description'] = next_line[:200]

        # ── QUANTITY (label-based fallback, e.g. "Qty: 100") ──────
        if fields['quantity'] is None:
            match = re.search(r'\b(?:qty|quantity)\.?\s*[:\-]?\s*(\d+(?:\.\d+)?)\b', line_clean, re.IGNORECASE)
            if match:
                try:
                    qty_val = float(match.group(1))
                    if qty_val > 0:
                        fields['quantity'] = qty_val
                except ValueError:
                    pass

        # ── USD TOTAL (original-currency, takes priority over any RM
        # total below) — real POs may show "Total Payable Incl. Tax
        # (RM) 32,946.16" alongside a reference "USD 8,020" original-
        # currency amount; the real transaction amount is the USD one.
        # Checks both currency-then-number ("USD 8,020.00") and
        # number-then-currency ("8,020.00 USD") — real documents use
        # either order, and only the first form was previously matched,
        # silently dropping the total on documents using the second.
        usd_match = (
            re.search(r'\b(?:usd|us\$)\s*[:\-]?\s*([\d,]+\.?\d*)', line_clean, re.IGNORECASE)
            or re.search(r'([\d,]+\.?\d*)\s*(?:usd|us\$)\b', line_clean, re.IGNORECASE)
        )
        if usd_match:
            val = extract_amount(usd_match.group(1))
            if val and val > 1:
                usd_total_candidates.append(val)

        if fields['po_number'] is None:
            # Requires SOME real separation between the "PO" label and the
            # captured value (a "No"/"Number"/"#" word, a colon/dash, or at
            # least a space) — without this, a PO number that itself starts
            # with "PO" (e.g. "PO3005713", a common Malaysian SME format)
            # would self-match: "PO" gets treated as the label and only
            # "3005713" gets captured, silently dropping the "PO" prefix.
            match = re.search(
                r'\bp\.?o\.?(?:\s*(?:no\.?|number|#)\s*[:\-]?\s*|\s*[:\-]\s*|\s+)([A-Za-z0-9\-\/]+)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = match.group(1).strip()
                # Reject a bare label word (e.g. "PO Ref No: 400-C008" —
                # a DIFFERENT field, a buyer-side reference code, not this
                # PO's own number — matches "PO" + whitespace and would
                # otherwise capture "Ref" as if it were the value).
                if len(val) > 2 and val.lower() not in ('ref', 'no', 'number'):
                    po_number_candidates.append((val, 'PO No. (same-line fallback)', True))
                    fields['po_number'] = val
                else:
                    po_number_candidates.append((val, 'PO No. (same-line fallback, rejected label-echo)', False))

            # Real POs sometimes label the document number bare "Doc No."
            # rather than "PO No." — checked only when the PO-specific
            # pattern above didn't match, so a genuine "PO No:" line is
            # never overridden by this looser fallback. (In practice the
            # document-wide "Document No."/"Doc No." pass earlier in this
            # function already catches most of these first; this stays as
            # a same-line safety net for phrasing that pass doesn't.)
            if fields['po_number'] is None:
                # "document\.?" must precede the shorter "doc\.?" — same
                # fix as extract_gr_fields()'s gr_number pattern: without
                # it, "doc\.?" alone matches just "Doc" inside "Document"
                # (no word boundary needed, since "No." is optional here
                # too), then greedily captures the rest of that SAME word
                # ("ument") as the value.
                doc_match = re.search(
                    r'(?:document\.?|doc\.?)\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    line_clean, re.IGNORECASE
                )
                if doc_match:
                    val = doc_match.group(1).strip()
                    if len(val) > 2 and val.lower() not in ('ref', 'no', 'number', 'ument'):
                        po_number_candidates.append((val, 'Doc No. (bare same-line fallback)', True))
                        fields['po_number'] = val
                    else:
                        po_number_candidates.append((val, 'Doc No. (bare same-line fallback, rejected)', False))

        # ── VENDOR NAME (the SUPPLIER, never the letterhead/buyer) ──
        # Unlike invoice's vendor extraction (which takes the FIRST
        # company match, skipping known-buyer lines), a PO's vendor is
        # named under a "Supplier"/"Vendor"/"To" heading further down the
        # page — the letterhead at the top is the BUYER issuing the
        # order. No company match is trusted until that heading has been
        # seen, so the letterhead can never be picked up as the vendor.
        if PO_SUPPLIER_HEADING_RE.search(line_clean):
            seen_supplier_heading = True

        if fields['vendor_name'] is None and seen_supplier_heading:
            # Unanchored (unlike invoice/GR's `^...`) so a same-line label
            # like "Supplier: MEGATECH COMPONENTS (M) SDN. BHD." still
            # matches starting at the company name, not failing because
            # "Supplier:" precedes it.
            match = re.search(
                r'([\w\s&\.\-\(\)]+(?:sdn\.?\s*bhd\.?|berhad|corporation|corp|ltd\.?|enterprise|trading)[\w\s&\.\-\(\)]*)',
                line_clean, re.IGNORECASE
            )
            if match:
                vendor = match.group(1).strip()
                if len(vendor) > 5:
                    fields['vendor_name'] = clean_vendor_name(vendor)

        if fields['po_date'] is None:
            match = re.search(
                r'(?:po\s*date|date|order\s*date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})',
                line_clean, re.IGNORECASE
            )
            if match:
                fields['po_date'] = normalize_date_string(match.group(1).strip())

        # TOTAL AMOUNT — collect every "total"-labeled amount (skipping
        # Subtotal/Sub Total/SST/Tax lines) and take the max once the loop
        # finishes, same fix as extract_fields(): Total = Subtotal + tax,
        # so it's always the largest labeled amount, and this is immune to
        # a Subtotal/SST line matching before the real Total line does.
        #
        # Two-line form: Google Vision OCR commonly puts a "Total Payable
        # Incl. Tax (RM)" / "Total Excl. Tax (RM)" / "Total" label on its
        # own line with nothing else, and the amount as the next line —
        # same pattern as the label-only "Document No." handling above.
        # Priority order (Payable-Incl-Tax > Excl-Tax > bare Total)
        # mirrors the priority the PO_FULL_PROMPT already gives Gemini
        # for this same field.
        _label_only_total_patterns = [
            r'^total\s*payable\s*(?:incl\.?(?:uding)?\s*(?:tax|sst|gst)?)?\s*\(?\s*(?:rm|myr|usd|us\$)?\s*\)?\s*$',
            r'^total\s*(?:excl\.?(?:uding)?\s*(?:tax|sst|gst)?)\s*\(?\s*(?:rm|myr)?\s*\)?\s*$',
            r'^total\s*\(?\s*(?:rm|myr)?\s*\)?\s*$',
        ]
        if any(re.search(p, line_clean, re.IGNORECASE) for p in _label_only_total_patterns):
            val = extract_amount(next_line)
            if val and val > 1:
                total_candidates.append(val)

        if not re.search(r'\bsub[\s\-]?total\b', line_clean, re.IGNORECASE) and \
           not re.search(r'\b(?:sst|gst|tax)\b', line_clean, re.IGNORECASE):
            match = re.search(
                r'\b(?:total|amount|grand\s*total)\s*\(?\s*(?:rm|myr)?\s*\)?\s*[:\-]?\s*([\d,]+\.?\d*)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = extract_amount(match.group(1))
                if val and val > 1:
                    total_candidates.append(val)

    if usd_total_candidates:
        fields['total_amount'] = max(usd_total_candidates)
        fields['currency'] = 'USD'
        _total_reason = f'largest USD-tagged candidate (priority over any MYR/RM candidate)'
    elif total_candidates:
        fields['total_amount'] = max(total_candidates)
        _total_reason = 'largest "total"-labeled candidate found (no USD-tagged candidate present)'
    else:
        _total_reason = 'no candidate found'
    print(f"DEBUG PO total_amount candidates: usd={usd_total_candidates} rm/other={total_candidates}\n"
          f"Selected: {fields['total_amount']} ({_total_reason})")  # TEMP-DEBUG

    _po_num_candidates_str = ',\n'.join(
        f' "{v}" from "{lbl}"' + ('' if acc else ' (rejected)')
        for v, lbl, acc in po_number_candidates
    )
    print(f"PO number candidates:\n[\n{_po_num_candidates_str}\n]\nSelected:\n{fields['po_number']}")  # TEMP-DEBUG
    if fields['po_number'] is None:
        match = re.search(r'PO[-\s]?(\d+)', ocr_text, re.IGNORECASE)
        if match:
            fields['po_number'] = match.group(0).strip()
            print(f"DEBUG PO po_number: found via final bare 'PO<digits>' document-wide fallback: {fields['po_number']!r}")  # TEMP-DEBUG

    if fields['po_date'] is None:
        match = re.search(r'(\d{1,2}\/\d{1,2}\/\d{4})', ocr_text)
        if match:
            fields['po_date'] = match.group(1).strip()

    if fields['total_amount'] is None:
        match = re.search(r'(?:rm|myr)\s*([\d,]+\.?\d*)', ocr_text, re.IGNORECASE)
        if match:
            val = extract_amount(match.group(1))
            if val:
                fields['total_amount'] = val

    # No Gemini call here — extract_po_fields() is regex-only, used as the
    # FALLBACK. The single merged Gemini vision call (fields + authenticity
    # in one request) happens in routes/documents.py's upload_purchase_order()
    # and overrides these regex values when it succeeds, so a PO upload
    # never makes more than one Gemini call.
    print(f"DEBUG PO extracted fields: {fields}")
    return fields


def extract_gr_fields(ocr_text):
    fields = {
        'gr_number':        None,
        'vendor_name':      None,
        'receipt_date':     None,
        'total_amount':     None,
        'currency':         'MYR',
        # 3-way audit comparison fields — po_reference is the PO number
        # THIS GR was received against (GR's own number is gr_number
        # above).
        'po_reference':     None,
        'item_description': None,
        'quantity':         None,
        # EVERY line item (not just the first) — see extract_fields().
        'line_items':       [],
    }

    lines = ocr_text.split('\n')
    total_candidates = []
    usd_total_candidates = []
    # On a GR the letterhead at the top is the RECEIVING company, never
    # the vendor — the actual supplier is named under a "Received From"/
    # "Supplier"/"Delivered By" heading further down. Same gating pattern
    # as extract_po_fields()'s vendor extraction.
    seen_supplier_heading = False

    # ── RECEIPT DATE candidates — (raw_value, source_label, excluded) ──
    # Every date-labeled line found is recorded here (even ones excluded
    # below) purely for debug visibility into what was found vs. selected.
    # Patterns are tried most-specific-label first per line (same
    # alternation-order reasoning as elsewhere in this file: regex
    # alternation/ordered-list matching stops at the first success, not
    # the longest) so a line like "From Doc Date" is labeled that instead
    # of falling through to the bare trailing "date" pattern.
    receipt_date_candidates = []
    _DATE_LABEL_PATTERNS = [
        (r'from\s+doc\s*date',    'From Doc Date',          True),
        (r'po\s*date',            'PO Date',                True),
        (r'supplier[\w\s]*date',  'Supplier document date', True),
        (r'receipt\s*date',       'Receipt Date',           False),
        (r'delivery\s*date',      'Delivery Date',          False),
        (r'date\s*received',      'Date Received',          False),
        (r'date',                 'Date',                   False),
    ]

    fields['line_items'] = _extract_all_line_items(lines)

    # Primary line-item extraction — see extract_fields() for why this
    # runs over the whole line list up front rather than per-line below.
    item_desc, item_qty = _extract_first_line_item(lines)
    if item_desc:
        fields['item_description'] = item_desc
    if item_qty is not None:
        fields['quantity'] = item_qty

    for i, line in enumerate(lines):
        line_clean = line.strip()
        next_line  = lines[i + 1].strip() if i + 1 < len(lines) else ''

        # ── PO REFERENCE ──────────────────────────────────
        # Real EMITS-style GRNs reference the PO via "From Doc No.:
        # PO3006000" rather than "PO No:"/"PO Ref:" — added as an
        # alternative label alongside the existing ones.
        if fields['po_reference'] is None:
            match = re.search(
                r'\b(?:p\.?o\.?|purchase\s*order|from\s*doc)\s*(?:no\.?|number|ref\.?)?\s*[:\-]\s*([A-Za-z0-9\-\/]+)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = match.group(1).strip()
                if len(val) > 2:
                    fields['po_reference'] = val

        # ── LINE ITEM: description + qty from the SAME table row ──
        # Tabular layout: "1  Aluminium Bracket A100  120  8.00  960.00"
        # -> row no. (discarded), description, qty, unit price, amount.
        # Tried before the label-based fallbacks below — pulling both
        # fields from the same row keeps them internally consistent.
        if fields['item_description'] is None or fields['quantity'] is None:
            row_match = LINE_ITEM_ROW_RE.match(line_clean)
            if row_match:
                if fields['item_description'] is None:
                    desc_val = row_match.group(1).strip()
                    if len(desc_val) > 2:
                        fields['item_description'] = desc_val[:200]
                if fields['quantity'] is None:
                    try:
                        qty_val = float(row_match.group(2))
                        if qty_val > 0:
                            fields['quantity'] = qty_val
                    except ValueError:
                        pass

        # ── ITEM / DESCRIPTION (label-based fallback). HEADER_WORD_RE
        # guards against grabbing the NEXT column header (e.g. "Qty")
        # when "Description" is itself just a column header. ──
        if fields['item_description'] is None:
            label_match = re.search(
                r'^(?:description|particulars|item\s*description|item)\s*[:\-]?\s*(.*)$',
                line_clean, re.IGNORECASE
            )
            if label_match:
                inline_val = label_match.group(1).strip()
                if len(inline_val) > 2 and not HEADER_WORD_RE.match(inline_val):
                    fields['item_description'] = inline_val[:200]
                elif next_line and len(next_line) > 2 and not HEADER_WORD_RE.match(next_line):
                    fields['item_description'] = next_line[:200]

        # ── QUANTITY (label-based fallback, e.g. "Qty: 100") ──────
        if fields['quantity'] is None:
            match = re.search(r'\b(?:qty|quantity)\.?\s*[:\-]?\s*(\d+(?:\.\d+)?)\b', line_clean, re.IGNORECASE)
            if match:
                try:
                    qty_val = float(match.group(1))
                    if qty_val > 0:
                        fields['quantity'] = qty_val
                except ValueError:
                    pass

        if fields['gr_number'] is None:
            # "doc" must precede the shorter "gr"/"do" alternatives — real
            # GRNs are often labeled bare "Doc No.  PD6011823" rather than
            # "GR No"/"GRN No", and since regex alternation takes the FIRST
            # alternative that lets the overall match succeed (not the
            # longest), "do" would otherwise partial-match inside "Doc" and
            # capture only the trailing "c" of "Doc No." as the number.
            #
            # A line starting "From Doc No." is the REFERENCED PO (captured
            # into po_reference above), not the GR's own document number —
            # skipped here so it's never mistaken for gr_number just
            # because it also contains the words "Doc No.".
            if re.search(r'\bfrom\s+doc', line_clean, re.IGNORECASE):
                match = None
            else:
                # "document\.?" must precede the shorter "doc\.?" for the
                # same reason "doc\.?" precedes "gr"/"do" per the comment
                # above: alternation tries alternatives left-to-right and
                # stops at the first one that lets the match succeed, not
                # necessarily the longest — without this, "doc\.?" alone
                # would already succeed by matching just "Doc" inside
                # "Document" (no word boundary required, since "Doc No."
                # must also match), then greedily capture the rest of that
                # SAME word ("ument") as the value, e.g. "Document No:
                # PD6011823" wrongly producing gr_number="ument".
                match = re.search(
                    r'(?:goods\s*receipt|delivery\s*order|document\.?|doc\.?|asn|gr|do)\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    line_clean, re.IGNORECASE
                )
            if match:
                val = match.group(1).strip()
                if len(val) > 2 and val.lower() not in ('ument', 'no', 'number'):
                    fields['gr_number'] = val

                if fields['gr_number'] is None:
                    no_match = re.search(r'^No\s*:\s*([A-Z]{2,}[A-Z0-9]+)', line_clean, re.IGNORECASE)
                    if no_match:
                        val = no_match.group(1).strip()
                        if len(val) > 2:
                            fields['gr_number'] = val

        # ── VENDOR NAME (who DELIVERED the goods, never the letterhead/
        # receiving company) — same gating pattern as extract_po_fields():
        # no company match is trusted until a "Received From"/"Supplier"/
        # "Delivered By" heading has actually been seen. ──
        if GR_SUPPLIER_HEADING_RE.search(line_clean):
            seen_supplier_heading = True

        if fields['vendor_name'] is None and seen_supplier_heading:
            # Unanchored, same reason as extract_po_fields() — a same-line
            # label like "Received From: MEGATECH..." must still match
            # starting at the company name.
            match = re.search(
                r'([\w\s&\.\-\(\)]+(?:sdn\.?\s*bhd\.?|berhad|corporation|corp|ltd\.?|enterprise|trading)[\w\s&\.\-\(\)]*)',
                line_clean, re.IGNORECASE
            )
            if match:
                vendor = match.group(1).strip()
                if len(vendor) > 5:
                    fields['vendor_name'] = clean_vendor_name(vendor)

        # ── RECEIPT DATE — collect every date-labeled candidate on this
        # line (see receipt_date_candidates / _DATE_LABEL_PATTERNS above
        # for why "From Doc Date"/"PO Date"/"Supplier ... Date" are
        # recorded but marked excluded rather than silently skipped). ──
        for _label_pat, _label_name, _excluded in _DATE_LABEL_PATTERNS:
            _date_match = re.search(
                _label_pat + r'\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})',
                line_clean, re.IGNORECASE
            )
            if _date_match:
                receipt_date_candidates.append((_date_match.group(1).strip(), _label_name, _excluded))
                break

        # Most GRNs carry no monetary total, but a few reference one — same
        # original-currency preference as extract_fields()/extract_po_fields().
        usd_match = re.search(r'\b(?:usd|us\$)\s*[:\-]?\s*([\d,]+\.?\d*)', line_clean, re.IGNORECASE)
        if usd_match:
            val = extract_amount(usd_match.group(1))
            if val and val > 1:
                usd_total_candidates.append(val)

        # TOTAL AMOUNT — same fix as extract_fields()/extract_po_fields():
        # collect every "total"-labeled amount (skipping Subtotal/SST/Tax
        # lines) and take the max once the loop finishes, instead of
        # keeping whichever "total"-ish line matched first.
        if not re.search(r'\bsub[\s\-]?total\b', line_clean, re.IGNORECASE) and \
           not re.search(r'\b(?:sst|gst|tax)\b', line_clean, re.IGNORECASE):
            match = re.search(
                r'\b(?:total|amount|grand\s*total)\s*\(?\s*(?:rm|myr)?\s*\)?\s*[:\-]?\s*([\d,]+\.?\d*)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = extract_amount(match.group(1))
                if val and val > 1:
                    total_candidates.append(val)

    if usd_total_candidates:
        fields['total_amount'] = max(usd_total_candidates)
        fields['currency'] = 'USD'
    elif total_candidates:
        fields['total_amount'] = max(total_candidates)

    # First non-excluded candidate, in document order, wins — same
    # semantics as the previous first-match-wins loop, now with every
    # candidate (including excluded ones) visible in the debug log below.
    _selected_date = next((c for c in receipt_date_candidates if not c[2]), None)
    if _selected_date:
        fields['receipt_date'] = normalize_date_string(_selected_date[0])
    _date_candidates_str = ',\n'.join(f' "{v}" from "{lbl}"' for v, lbl, _ in receipt_date_candidates)
    print(f"GR date candidates:\n[\n{_date_candidates_str}\n]\nSelected:\n{fields['receipt_date']}")  # TEMP-DEBUG

    # Fallback for GR number — this previously ran INSIDE the block above
    # by accident (bad indentation nested it under `if fields['gr_number']
    # is None`, which also silently broke receipt_date/total_amount
    # extraction, now moved into the main loop above). Kept as its own
    # top-level pass, unrelated to the main loop.
    if fields['gr_number'] is None:
        lines_list = ocr_text.split('\n')
        for i, line in enumerate(lines_list):
            if re.search(r'^No\s*:\s*$', line.strip()):
                for j in range(1, 4):
                    if i + j < len(lines_list):
                        next_val = lines_list[i + j].strip()
                        if (next_val and len(next_val) > 2 and
                            re.search(r'[A-Z]{2,}', next_val) and
                            not re.search(r'^(date|deliver|invoice|northern|unit|gurney|penang|tel|fax|attn)$',
                                         next_val, re.IGNORECASE)):
                            fields['gr_number'] = next_val
                            break

    if fields['receipt_date'] is None:
        match = re.search(r'(\d{1,2}\/\d{1,2}\/\d{4})', ocr_text)
        if match:
            fields['receipt_date'] = match.group(1).strip()

    if fields['total_amount'] is None:
        match = re.search(r'(?:rm|myr)\s*([\d,]+\.?\d*)', ocr_text, re.IGNORECASE)
        if match:
            val = extract_amount(match.group(1))
            if val:
                fields['total_amount'] = val

    # No Gemini call here — extract_gr_fields() is regex-only, used as the
    # FALLBACK. The single merged Gemini vision call (fields + authenticity
    # in one request) happens in routes/documents.py's upload_goods_receipt()
    # and overrides these regex values when it succeeds, so a GR upload
    # never makes more than one Gemini call.
    print(f"DEBUG GR extracted fields: {fields}")
    return fields