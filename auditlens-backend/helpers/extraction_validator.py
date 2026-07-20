"""
Lightweight post-processing validation for Gemini's extracted fields.

This runs AFTER the single Gemini extraction call already made in
routes/documents.py — it never calls Gemini itself, makes no network
calls, and does not touch OCR confidence (helpers/ocr_helper.py's
calculate_confidence()). It only inspects the field dict already
produced by the regex fallback + Gemini merge and:
  1. rejects a small set of known-bad "label echoed as value" strings
     (e.g. a document-number field literally containing "Ref"/"No"),
  2. cross-checks total_amount against the sum of line_items as a
     sanity check (warning only — legitimate totals often include tax/
     shipping/discount that isn't itemized, so a mismatch is a signal
     to review, not proof of an error),
  3. sanity-checks amount/date fields for a document type,
  4. rolls all of the above into a single extraction_confidence score
     (0-100) and a validation_status, kept entirely separate from the
     Vision OCR confidence already computed elsewhere.
"""

from datetime import date

# Words Gemini sometimes echoes back as the "value" when it actually
# found the field's own label instead of the printed value beside it
# (e.g. returning "Ref" for po_number instead of "PO3006000"). Matched
# case-insensitively after stripping surrounding punctuation/whitespace.
_LABEL_ECHO_VALUES = {
    'ref', 'ref.', 'no', 'no.', 'number', 'date', 'amount',
    'n/a', 'na', 'none', '-', '--', 'nil',
}

# Fields where a label-echo is a meaningful, known failure mode (document
# identifier / reference fields) — amount and date fields are checked by
# their own numeric/date-shaped rules below instead.
_LABEL_ECHO_FIELDS = {
    'invoice_number', 'po_number', 'gr_number', 'po_reference',
}

MIN_YEAR = 2000
MAX_YEAR = date.today().year + 1


def _strip_label_punctuation(value):
    return value.strip().strip(':').strip().lower()


def _is_label_echo(value):
    if not isinstance(value, str):
        return False
    return _strip_label_punctuation(value) in _LABEL_ECHO_VALUES


def _is_plausible_date(value):
    """value is already normalized to 'YYYY-MM-DD' (or None) by the time
    it reaches here — parse_date() runs after this validator in
    routes/documents.py, so this checks the raw ISO string Gemini/regex
    produced, not a Python date object."""
    if not isinstance(value, str):
        return False
    parts = value.split('-')
    if len(parts) != 3:
        return False
    try:
        year = int(parts[0])
    except ValueError:
        return False
    return MIN_YEAR <= year <= MAX_YEAR


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _line_items_total(line_items):
    """Sum of line-item amounts, falling back to quantity*unit_price for
    any row missing an explicit 'amount'. Returns None if no row has
    enough data to contribute a number (nothing to cross-check against)."""
    if not line_items:
        return None
    total = 0.0
    counted = 0
    for item in line_items or []:
        if not isinstance(item, dict):
            continue
        amount = _num(item.get('amount'))
        if amount is None:
            qty = _num(item.get('quantity'))
            price = _num(item.get('unit_price'))
            if qty is not None and price is not None:
                amount = qty * price
        if amount is not None:
            total += amount
            counted += 1
    return round(total, 2) if counted else None


def _status_from_confidence(confidence, has_warnings):
    if confidence >= 85 and not has_warnings:
        return 'PASS'
    if confidence < 50:
        return 'FAIL'
    return 'REVIEW'


def validate_extraction(document_type, fields, line_items=None):
    """
    document_type: 'invoice' | 'po' | 'gr'
    fields: the merged (regex fallback + Gemini) field dict for that
      document type, BEFORE parse_date()/DB insert — date fields are
      still raw 'YYYY-MM-DD' strings or None at this point.
    line_items: the same list already resolved into `fields['line_items']`
      by the caller (passed separately here to keep this function's
      signature explicit about what it reads).

    Returns (cleaned_fields, validation_result) where:
      - cleaned_fields is a shallow copy of `fields` with any rejected
        label-echo values replaced by None (nothing else is modified —
        the amount cross-check is a warning, never a mutation, since a
        real total legitimately differing from the summed line items
        is common, e.g. tax/shipping not itemized).
      - validation_result is {'warnings': [str, ...],
        'extraction_confidence': int 0-100, 'validation_status': str}.

    Never calls Gemini. Pure in-process field inspection.
    """
    cleaned = dict(fields)
    warnings = []
    confidence = 100

    # 1) Label-echo rejection for document-identifier fields.
    for field_name in _LABEL_ECHO_FIELDS:
        value = cleaned.get(field_name)
        if _is_label_echo(value):
            warnings.append(
                f'{field_name} looked like a field label ("{value}") rather than an '
                f'actual value and was rejected'
            )
            cleaned[field_name] = None
            confidence -= 25

    # 2) Amount cross-check: total_amount vs. sum of line items.
    #    Warning only per spec — never overwrites total_amount, since a
    #    legitimate total commonly includes tax/shipping/discount that
    #    isn't broken out in the line-item table.
    total_amount = _num(cleaned.get('total_amount'))
    items_total = _line_items_total(line_items if line_items is not None else cleaned.get('line_items'))
    if total_amount is not None and items_total is not None and items_total > 0:
        tolerance = max(1.0, items_total * 0.02)  # 2% relative, 1.00 absolute floor
        if abs(total_amount - items_total) > tolerance:
            warnings.append('Total amount may be incorrect based on line item calculation')
            confidence -= 15

    # 3) Field sanity checks.
    tax_amount = _num(cleaned.get('tax_amount'))
    if total_amount is not None and total_amount < 0:
        warnings.append('total_amount is negative, which is not a valid amount')
        cleaned['total_amount'] = None
        confidence -= 20
    if tax_amount is not None and total_amount is not None and tax_amount > total_amount:
        # Tax can never exceed the final payable total it's part of —
        # this is the exact "handwritten number mistaken for tax" shape
        # described in the bug report (a small unrelated number, e.g.
        # an account-number fragment, ending up larger than it should
        # relative to the real total, or vice versa producing an
        # impossible tax > total).
        warnings.append('tax_amount is larger than total_amount, which is not possible — tax_amount rejected')
        cleaned['tax_amount'] = None
        confidence -= 20
    elif tax_amount is not None and total_amount is None:
        # total_amount missing entirely (the "not detected" half of the
        # reported bug) means tax_amount can't be cross-checked at all —
        # can't prove it's wrong, but a tax value with no total to belong
        # to is exactly the shape of a stray/handwritten/account-number
        # value slipping through, so flag it for human review rather than
        # silently trusting it.
        warnings.append('tax_amount was extracted but total_amount is missing — tax_amount could not be verified')
        confidence -= 10

    date_field = {
        'invoice': 'invoice_date',
        'po':      'po_date',
        'gr':      'receipt_date',
    }.get(document_type)
    if date_field:
        date_value = cleaned.get(date_field)
        if date_value is not None and not _is_plausible_date(date_value):
            warnings.append(f'{date_field} ("{date_value}") is not a plausible date for this document type and was rejected')
            cleaned[date_field] = None
            confidence -= 15

    confidence = max(0, min(100, confidence))
    status = _status_from_confidence(confidence, bool(warnings))

    return cleaned, {
        'warnings': warnings,
        'extraction_confidence': confidence,
        'validation_status': status,
    }
