"""Candidate-based extraction engine — shared scoring/selection/logging
used by helpers/ocr_helper.py's invoice/PO/GR regex extraction.

Replaces "first regex match wins" / "largest number wins" with "collect
every candidate a document actually offers -> score each by label
priority (with a reject/negative-keyword list per field type) -> select
the highest-confidence one". The regex DETECTION (finding a value near a
label on a line) still lives in ocr_helper.py, per document type — this
module only holds the generic candidate shape, the priority-label
scoring tables, and the selection/logging logic that used to be
duplicated ad hoc (and inconsistently) across extract_fields()/
extract_po_fields()/extract_gr_fields().

No layout/bounding-box geometry — "position" is the OCR line index
(document order), not x/y coordinates; Google Vision OCR responses
aren't currently parsed for geometry anywhere in this app.
"""

import re

NEEDS_REVIEW_THRESHOLD = 60

# A totals mini-table ("SUB-TOTAL: / GST (0%) / 8,020.00 / 0.00 /
# 8,020.00 / TOTAL (US$)") is sometimes read by Google Vision with the
# VALUE column emitted before the LABEL row, not after — a bare "TOTAL"-
# style label line with no value on that line, and no value on the next
# line either, because the value already came out several lines above.
_BARE_NUMBER_RE = re.compile(r'^[\d,]+\.?\d*\s*(?:usd|us\$|rm|myr)?$', re.IGNORECASE)


def find_reverse_proximity_amount(lines, label_index, window=5):
    """Looks at up to `window` lines immediately ABOVE `label_index` for
    the nearest bare-number line (optionally with a trailing currency
    tag, e.g. "8,020.00 USD") and returns (raw_text, source_index), or
    None if none is found. Only meant to be tried as a fallback, after a
    same-line/forward-next-line match has already failed — a normal
    forward-labeled document never reaches this.
    """
    for offset in range(1, window + 1):
        idx = label_index - offset
        if idx < 0:
            break
        line = lines[idx].strip()
        if _BARE_NUMBER_RE.match(line):
            return line, idx
    return None


def make_candidate(value, context, position, score, reason, **extra):
    """Builds one candidate dict in the shape every extractor below emits.
    `extra` carries field-specific data (e.g. currency= for amounts) that
    selection/logging can use without every caller needing the same
    fixed shape.
    """
    candidate = {
        'value':            value,
        'context':          context,
        'position':         position,
        'confidence_score': score,
        'reason':           reason,
    }
    candidate.update(extra)
    return candidate


def select_best(candidates):
    """Generic selection: highest confidence_score wins; ties broken by
    earliest document position. Candidates scored at/below the "likely
    rejected" floor (5 — see score_amount_context/score_docnumber_context/
    score_date_context) are ignored UNLESS every candidate found is that
    bad, in which case the best of a bad set still beats returning
    nothing (same graceful-degradation behavior the old first-match-wins
    code had via its own bare/last-resort fallback patterns).
    """
    if not candidates:
        return None
    usable = [c for c in candidates if c['confidence_score'] > 5]
    pool = usable or candidates
    return max(pool, key=lambda c: (c['confidence_score'], -c['position']))


def select_best_amount(candidates, preferred_currency=None):
    """Same as select_best(), plus: if `preferred_currency` (the
    document's ORIGINAL currency, e.g. 'USD' on a document whose local/
    converted total is RM) has at least one usable candidate, that
    currency group always wins over an equal-or-lower-scored local-
    currency one — a converted total is never the real transaction
    amount. Within the winning group, ties broken by largest value
    (Total = Subtotal + tax, so the true total is never the smaller of
    two same-priority candidates) then earliest position.
    """
    if not candidates:
        return None
    usable = [c for c in candidates if c['confidence_score'] > 5]
    pool = usable or candidates
    if preferred_currency:
        preferred = [c for c in pool if c.get('currency') == preferred_currency]
        if preferred:
            pool = preferred
    return max(pool, key=lambda c: (c['confidence_score'], c['value'], -c['position']))


def log_extraction_result(document_type, field, candidates, selected):
    """Structured production log (permanent — not a temporary debug
    print) in the format:

    EXTRACTION RESULT:
    Document: Invoice
    Field: total_amount
    Candidates:
    [
     {value: 8020.0, context: "TOTAL (US$)", score: 95},
     {value: 98.0, context: "GST", score: 5}
    ]
    Selected: 8020.0
    Reason: highest confidence candidate
    """
    candidates_str = ',\n'.join(
        f' {{value: {c["value"]!r}, context: {c["context"]!r}, score: {c["confidence_score"]}}}'
        for c in candidates
    ) or ' (none found)'
    selected_value = selected['value'] if selected else None
    reason = selected['reason'] if selected else 'no candidate found'
    print(
        f"EXTRACTION RESULT:\n"
        f"Document: {document_type}\n"
        f"Field: {field}\n"
        f"Candidates:\n[\n{candidates_str}\n]\n"
        f"Selected: {selected_value}\n"
        f"Reason: {reason}"
    )
    return {
        'confidence': selected['confidence_score'] if selected else 0,
        'source':     selected['context'] if selected else None,
        'needs_review': (selected is None) or (selected['confidence_score'] < NEEDS_REVIEW_THRESHOLD),
    }


# ============================================================
# AMOUNT scoring (invoice total_amount / PO total_amount / GR total_amount)
# ============================================================
# AP-aware amount candidate scoring. The requested point scale (+50/+40/
# +30/... , -40/-30/-20) is reported verbatim in the `reason` text for
# every candidate (visible in every "TOTAL CANDIDATES"/EXTRACTION RESULT
# debug log) but the actual returned confidence_score stays on this
# engine's existing 0-100 scale — used by select_best_amount()'s ">5 is
# usable" floor and log_extraction_result()'s NEEDS_REVIEW_THRESHOLD
# (=60), both shared across amount/document-number/date scoring — so the
# relative-priority POINT VALUES the spec asks for are preserved exactly
# (Grand Total/Total Payable > Amount Due/Invoice Total > Total Amount >
# medium > negative) without needing a field-type-specific threshold.
#
# Checked in this order — more specific/longer phrases first, since a
# phrase like "Total Payable Incl. Tax" or "Invoice Total" must win on
# its OWN tier before the negative "tax" keyword (or the generic bare
# "total") ever gets a chance to match a substring of it.
_AMOUNT_SUBTOTAL_LABELS = ('sub total', 'subtotal')                      # AP score -30
_AMOUNT_SCORE_50_LABELS = ('grand total', 'total payable')               # AP score +50
_AMOUNT_SCORE_40_LABELS = (                                              # AP score +40
    'amount due', 'invoice total', 'purchase order total',
    'balance due', 'net payable',
)
_AMOUNT_SCORE_30_LABELS = (                                              # AP score +30
    'total amount', 'order total', 'purchase total', 'total value', 'total',
)
_AMOUNT_MEDIUM_PRIORITY_LABELS = ('net amount', 'amount')                # AP score +15
_AMOUNT_NEGATIVE_40_KEYWORDS = ('gst', 'tax', 'vat', 'sst')               # AP score -40
_AMOUNT_NEGATIVE_20_KEYWORDS = (                                         # AP score -20
    'unit price', 'price each', 'qty', 'quantity', 'discount',
)


def score_amount_context(context_text):
    text = (context_text or '').lower()
    for kw in _AMOUNT_SUBTOTAL_LABELS:
        if kw in text:
            return 10, f'AP score -30: subtotal keyword "{kw}" (not the final total)'
    for kw in _AMOUNT_SCORE_50_LABELS:
        if kw in text:
            return 95, f'AP score +50: high priority total keyword "{kw}"'
    for kw in _AMOUNT_SCORE_40_LABELS:
        if kw in text:
            return 85, f'AP score +40: high priority total keyword "{kw}"'
    for kw in _AMOUNT_SCORE_30_LABELS:
        if kw in text:
            return 70, f'AP score +30: total keyword "{kw}"'
    for kw in _AMOUNT_MEDIUM_PRIORITY_LABELS:
        if kw in text:
            return 50, f'AP score +15: medium priority keyword "{kw}"'
    for kw in _AMOUNT_NEGATIVE_40_KEYWORDS:
        if kw in text:
            return 5, f'AP score -40: negative keyword "{kw}" — likely not the document total'
    for kw in _AMOUNT_NEGATIVE_20_KEYWORDS:
        if kw in text:
            return 15, f'AP score -20: negative keyword "{kw}" — likely not the document total'
    return 30, 'AP score +10: no recognized total keyword (bare amount)'


# ============================================================
# DOCUMENT NUMBER scoring (invoice_number / po_number / gr_number)
# ============================================================
_DOCNUMBER_REJECT_KEYWORDS = (
    'supplier ref', 'customer ref', 'account no', 'account number', 'ref',
)
DOCNUMBER_REJECT_VALUES = {'ref', 'no', 'number', 'ument', 'account'}

INVOICE_NUMBER_LABELS = ('invoice no', 'invoice number', 'inv no')
PO_NUMBER_LABELS = ('po no', 'po number', 'purchase order no', 'document no', 'doc no')
GR_NUMBER_LABELS = ('gr no', 'goods receipt no', 'document no', 'doc no')


def score_docnumber_context(context_text, high_priority_labels):
    text = (context_text or '').lower()
    for kw in _DOCNUMBER_REJECT_KEYWORDS:
        if kw in text:
            return 5, f'reject-list keyword "{kw}" present — likely a different reference field'
    for kw in high_priority_labels:
        if kw in text:
            return 90, f'high priority label "{kw}"'
    return 35, 'no recognized document-number label'


def is_rejected_docnumber_value(value):
    return (not value) or (len(value) <= 2) or (value.strip().lower() in DOCNUMBER_REJECT_VALUES)


# ============================================================
# PO REFERENCE scoring (the PO an invoice/GR was raised against — NOT
# that document's own number) — distinct from PO_NUMBER_LABELS above,
# which scores a PO's OWN document-number extraction.
# ============================================================
# Priority order per spec: PO Number label > Purchase Order label >
# P/O label > Document No. Checked in this order, first match wins.
_PO_REFERENCE_PRIORITY = (
    ('po number',       95, 'PO Number label (highest priority)'),
    ('po no',           85, 'PO No label'),
    ('purchase order',  80, 'Purchase Order label'),
    ('p/o',             70, 'P/O label'),
    ('document no',     60, 'Document No label'),
    ('doc no',          60, 'Doc No label'),
)
_PO_REFERENCE_REJECT_KEYWORDS = (
    'invoice no', 'delivery no', 'gr no', 'goods receipt no', 'receipt no',
)


def score_po_reference_context(context_text):
    text = (context_text or '').lower()
    for kw in _PO_REFERENCE_REJECT_KEYWORDS:
        if kw in text:
            return 5, f'reject-list keyword "{kw}" — likely a different document\'s number, not the PO reference'
    for kw, score, label in _PO_REFERENCE_PRIORITY:
        if kw in text:
            return score, label
    return 35, 'no recognized PO-reference label'


# ============================================================
# VENDOR NAME scoring (v5) — AP vendor intelligence: the vendor is the
# SUPPLIER/SELLER/invoice issuer, never the buyer/customer/receiving
# company. Checked in this order — the negative (buyer-side) labels
# first, since a "Bill To"/"Ship To" section can still contain a company
# name shaped exactly like a real vendor name.
#
# IMPORTANT: a real invoice very often prints NO explicit "Vendor:"/
# "Supplier:" label at all — the vendor is identifiable only by its
# header/letterhead position. Absence of a label must NEVER be treated
# as low confidence; only an explicit buyer-side label is. See
# score_vendor_context()'s default tier below — previously 20 ("no
# recognized vendor label"), which penalized the overwhelmingly common
# unlabeled-header-vendor case; now 60, the same floor as every other
# non-negative signal.
# ============================================================
VENDOR_LABEL_SCORES = (
    ('bill to',           -100, 'Bill To (the customer/buyer, not the vendor)'),
    ('ship to',           -100, 'Ship To (the customer/buyer, not the vendor)'),
    ('invoice to',        -100, 'Invoice To (the customer/buyer, not the vendor)'),
    ('customer',          -100, 'Customer (the buyer, not the vendor)'),
    ('buyer',             -100, 'Buyer (the buyer, not the vendor)'),
    ('purchaser',         -100, 'Purchaser (the buyer, not the vendor)'),
    ('receiver',          -100, 'Receiver (the buyer, not the vendor)'),
    ('client',            -100, 'Client (the buyer, not the vendor)'),
    ('delivery address',   -80, 'Delivery address (the buyer\'s, not the vendor\'s)'),
    ('deliver to',         -80, 'Deliver To (the buyer\'s address, not the vendor\'s)'),
    ('supplier',             90, 'Supplier label'),
    ('vendor',               90, 'Vendor label'),
    ('seller',               85, 'Seller label'),
    ('invoice issuer',       85, 'Invoice issuer label'),
    ('issued by',            85, 'Issued by label'),
)


def score_vendor_context(context_text, is_top_of_document=False, is_repeated=False):
    """context_text: the line (or heading) the candidate company name was
    found on/near.
    is_top_of_document: True if this candidate is among the first few
      lines of the WHOLE document — the letterhead/header position,
      computed from ABSOLUTE line position. Deliberately NOT a relative
      "before the Bill To section" heuristic: a document whose Bill To
      section appears early, or whose OCR line order isn't strictly
      top-to-bottom, must not make a genuine header vendor fall through
      to a low score just because that heuristic misfired.
    is_repeated: True if the SAME normalized company name (see helpers/
      entity_normalizer.py) also appears elsewhere in the document — a
      real vendor is often printed more than once (letterhead AND e.g. a
      footer/signature block); a customer/buyer name usually is not.
    No label is required for a high score.
    """
    text = (context_text or '').lower()
    for kw, score, label in VENDOR_LABEL_SCORES:
        if kw in text:
            return score, label
    if is_top_of_document:
        return 100, 'Company name in top invoice header/letterhead area (no label required)'
    if is_repeated:
        return 60, 'Repeated company entity across the document'
    return 60, 'No explicit label or header position — still a plausible vendor candidate, not penalized for a missing label'


# ============================================================
# DATE scoring (invoice_date / po_date / receipt_date)
# ============================================================
# Each entry: (label substring, score, human-readable label for logging).
# Checked in order, first match wins per candidate — longer/more-specific
# phrases must precede shorter ones they contain (e.g. "from doc date"
# before bare "date") for the same reason label lists are ordered
# elsewhere in this file: substring matching stops at the first hit, not
# the longest.
INVOICE_DATE_LABEL_SCORES = (
    ('e-invoice date', 95, 'E-Invoice Date'),
    ('invoice date',   95, 'Invoice Date'),
    ('receipt date',   90, 'Receipt Date'),
    ('bill date',      90, 'Bill Date'),
    ('due date',       40, 'Due Date (payment due date, not the invoice date)'),
    ('date',           60, 'bare Date label'),
)
PO_DATE_LABEL_SCORES = (
    ('po date',        95, 'PO Date'),
    ('order date',     90, 'Order Date'),
    ('document date',  90, 'Document Date'),
    ('date',           60, 'bare Date label'),
)
# Highest priority (+100): this IS the GR's own receipt date — a
# structurally-paired header-table date (see _find_gr_header_pair() in
# ocr_helper.py) scores even higher, +120, applied directly at that call
# site rather than through this table. Negative (-100, checked before the
# medium/low bare-"date" catch-all): a date that belongs to a DIFFERENT
# referenced document (the PO, the supplier's own doc) — never selected
# while any valid receipt/document/header-pair date candidate exists, per
# select_best()'s "usable > 5" floor, but still returned as an absolute
# last resort so a document with ONLY a From Doc Date still gets a value
# instead of None. Medium (+70): a generic document date, plausible but
# less certain than an explicit receipt-date label.
GR_DATE_LABEL_SCORES = (
    ('goods receipt date', 100, 'Goods Receipt Date'),
    ('receipt date',       100, 'Receipt Date'),
    ('gr date',            100, 'GR Date'),
    ('received date',      100, 'Received Date'),
    ('posting date',       100, 'Posting Date'),
    ('from doc date',     -100, 'From Doc Date (the referenced PO\'s date, not this GR\'s own date)'),
    ('po date',           -100, 'PO Date (the referenced PO\'s date, not this GR\'s own date)'),
    ('supplier date',     -100, 'Supplier Date (not this GR\'s own date)'),
    ('supplier',          -100, 'Supplier document date (not this GR\'s own date)'),
    ('document dt',         70, 'Document Dt'),
    ('document date',       70, 'Document Date'),
    ('date',                70, 'bare Date label'),
)


def score_date_context(context_text, label_scores):
    text = (context_text or '').lower()
    for kw, score, label in label_scores:
        if kw in text:
            return score, label
    return 30, 'no recognized date label'


# ============================================================
# CURRENCY scoring — reusable across invoice/PO/GR total_amount
# ============================================================
# Currency must be DERIVED from the OCR context around the selected
# amount, never defaulted — a document with no visible currency marker
# anywhere near its total returns None, not a guessed "MYR". Patterns
# are word-boundary-aware so short tokens like "RM" don't false-positive
# inside unrelated words (e.g. "term", "confirm").
_CURRENCY_PATTERNS = (
    ('USD', (r'u\.s\.\$', r'\bus\$', r'\busd\b', r'\bdollar')),
    ('SGD', (r's\$', r'\bsgd\b')),
    ('EUR', (r'\beur\b', r'€')),
    # JPY and CNY historically share the "¥" glyph — only JPY claims the
    # bare symbol (the far more common case in these documents); CNY is
    # only recognized via its unambiguous code/abbreviation.
    ('JPY', (r'\bjpy\b', r'¥')),
    ('CNY', (r'\bcny\b', r'\brmb\b')),
    ('MYR', (r'\brm\b', r'\bmyr\b', r'\bringgit')),
)


def detect_currency_candidates(text):
    """Every currency keyword found in `text` becomes a (currency,
    context, score) candidate — context is always the full input text
    (not just the matched keyword), since that's what's useful in a
    debug log. Score is nudged by matched-keyword length so a more
    specific match (e.g. "US$", 3 chars) outranks a shorter one that
    happens to be a substring of it (e.g. SGD's "S$", 2 chars, which
    "US$" also contains). Returns [] if nothing found — callers must
    NOT substitute a default currency when this is empty.
    """
    text_lower = (text or '').lower()
    candidates = []
    for currency, patterns in _CURRENCY_PATTERNS:
        for pat in patterns:
            m = re.search(pat, text_lower)
            if m:
                candidates.append((currency, text, 90 + len(m.group(0))))
                break
    return candidates


def select_currency(candidates):
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[2])[0]


def log_currency_result(document_type, candidates, selected_currency):
    """Structured production log:

    CURRENCY CANDIDATES:
    [
    {
    "currency": "USD",
    "context": "TOTAL (US$)",
    "score": 93
    }
    ]
    Selected:
    USD
    """
    candidates_str = ',\n'.join(
        f'{{\n"currency": {cur!r},\n"context": {ctx!r},\n"score": {score}\n}}'
        for cur, ctx, score in candidates
    ) or '(none found)'
    print(
        f"CURRENCY CANDIDATES ({document_type}):\n\n[\n{candidates_str}\n]\n\n"
        f"Selected:\n{selected_currency}"
    )
