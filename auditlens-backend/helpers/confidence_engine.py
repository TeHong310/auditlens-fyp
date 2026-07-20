"""Post-merge field confidence — compares Gemini's returned value against
the OCR/regex engine's independently-computed value for the SAME field.
Two independent extraction methods agreeing is the strongest confidence
signal available; disagreement is exactly what should route a field to
manual review. In-memory/logged only (see helpers/extraction_engine.py's
`_confidence` for the OCR engine's own internal per-field confidence,
which this builds on) — not persisted to the DB or added as a new API
response field.
"""

ACCEPT_THRESHOLD = 85
REVIEW_THRESHOLD = 60


def _values_agree(a, b):
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 0.01
    return str(a).strip().lower() == str(b).strip().lower()


def compute_field_confidence(gemini_value, ocr_value, ocr_confidence_entry):
    """ocr_confidence_entry: the OCR engine's own {confidence, source,
    needs_review} dict for this field (extraction_engine.py's
    log_extraction_result() return value), or None."""
    ocr_conf = (ocr_confidence_entry or {}).get('confidence', 0)

    if gemini_value is not None and ocr_value is not None and _values_agree(gemini_value, ocr_value):
        confidence, source = 95, 'Gemini + OCR agreement'
    elif gemini_value is not None and ocr_value is not None:
        # Two independent extractors disagree — trust Gemini's value (the
        # primary extractor) but this is exactly the case that should be
        # flagged for a human to double-check, not silently accepted.
        confidence, source = 65, 'Gemini value (disagrees with OCR)'
    elif gemini_value is not None:
        confidence, source = 80, 'Gemini only (no independent OCR value to cross-check)'
    elif ocr_value is not None:
        confidence, source = min(ocr_conf, 75), (ocr_confidence_entry or {}).get('source') or 'OCR only'
    else:
        confidence, source = 0, None

    if confidence >= ACCEPT_THRESHOLD:
        status = 'accepted'
    elif confidence >= REVIEW_THRESHOLD:
        status = 'review'
    else:
        status = 'needs_review'

    value = gemini_value if gemini_value is not None else ocr_value
    return {'value': value, 'confidence': confidence, 'source': source, 'status': status}


def compute_line_items_confidence(line_items, gemini_result_had_items, ocr_result_had_items):
    """Confidence for the line_items array as a whole (not per-row):
    what fraction of the extracted rows have every field a matching
    engine actually needs (description + quantity), plus a source/
    agreement signal — Gemini and OCR independently producing a non-
    empty table is the same "two independent methods agree" signal
    compute_field_confidence() uses for scalar fields."""
    if not line_items:
        return {'value': [], 'confidence': 0, 'source': None, 'status': 'needs_review'}

    complete = sum(1 for it in line_items if it.get('description') and it.get('quantity') is not None)
    completeness = complete / len(line_items)

    if gemini_result_had_items and ocr_result_had_items:
        source = 'Gemini + OCR agreement'
        base = 95
    elif gemini_result_had_items:
        source = 'Gemini'
        base = 85
    else:
        source = 'OCR'
        base = 70

    confidence = round(base * completeness)
    if confidence >= ACCEPT_THRESHOLD:
        status = 'accepted'
    elif confidence >= REVIEW_THRESHOLD:
        status = 'review'
    else:
        status = 'needs_review'
    return {'value': line_items, 'confidence': confidence, 'source': source, 'status': status}


def log_field_confidence(document_type, field_confidence):
    for field, entry in field_confidence.items():
        value = entry['value']
        value_repr = f'{len(value)} item(s)' if isinstance(value, list) else repr(value)
        print(
            f"FIELD CONFIDENCE ({document_type}) | {field} | "
            f"value={value_repr} | confidence={entry['confidence']} | "
            f"source={entry['source']!r} | status={entry['status']}"
        )
