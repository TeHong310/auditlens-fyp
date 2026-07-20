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
# Checked in this order — SUB TOTAL/SUBTOTAL must be tested before the
# bare "total" keyword below it (it's a substring of "sub total"), and
# any HIGH_PRIORITY label must be tested before NEGATIVE keywords so a
# legitimate label like "Total Payable Incl. Tax" scores as a real total
# rather than getting penalized just for containing the word "tax".
_AMOUNT_SUBTOTAL_LABELS = ('sub total', 'subtotal')
_AMOUNT_HIGH_PRIORITY_LABELS = (
    'grand total', 'total amount', 'amount due', 'balance due',
    'net payable', 'order total', 'purchase total', 'total payable',
    'total value', 'total',
)
_AMOUNT_MEDIUM_PRIORITY_LABELS = ('net amount', 'amount')
_AMOUNT_NEGATIVE_KEYWORDS = (
    'gst', 'tax', 'vat', 'sst', 'unit price', 'price each',
    'qty', 'quantity', 'discount',
)


def score_amount_context(context_text):
    text = (context_text or '').lower()
    for kw in _AMOUNT_SUBTOTAL_LABELS:
        if kw in text:
            return 55, f'medium priority keyword "{kw}"'
    for kw in _AMOUNT_HIGH_PRIORITY_LABELS:
        if kw in text:
            return 95, f'high priority total keyword "{kw}"'
    for kw in _AMOUNT_MEDIUM_PRIORITY_LABELS:
        if kw in text:
            return 55, f'medium priority keyword "{kw}"'
    for kw in _AMOUNT_NEGATIVE_KEYWORDS:
        if kw in text:
            return 5, f'negative keyword "{kw}" present — likely not the document total'
    return 30, 'no recognized total keyword (bare amount)'


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
GR_DATE_LABEL_SCORES = (
    ('from doc date',  20, 'From Doc Date (the referenced PO\'s date, not this GR\'s own date)'),
    ('po date',        20, 'PO Date (the referenced PO\'s date, not this GR\'s own date)'),
    ('supplier',       20, 'Supplier document date (not this GR\'s own date)'),
    ('receipt date',   95, 'Receipt Date'),
    ('delivery date',  95, 'Delivery Date'),
    ('date received',  95, 'Date Received'),
    ('document date',  90, 'Document Date'),
    ('date',           90, 'bare Date label'),
)


def score_date_context(context_text, label_scores):
    text = (context_text or '').lower()
    for kw, score, label in label_scores:
        if kw in text:
            return score, label
    return 30, 'no recognized date label'
