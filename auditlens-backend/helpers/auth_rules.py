"""
Document-type-aware authentication scoring engine.

Replaces the old binary "3-signals-treated-equally" pass/fail gate
(previously helpers/authenticity_check.py::_compute_authenticity_status)
with a weighted rule engine: different document types care about
different signals to different degrees (e.g. a signature missing on an
Invoice is normal; a receiving chop missing on a Goods Receipt is not).

This module is pure computation — no Gemini calls, no DB access, no
dependency on the extraction pipeline. It only takes already-detected
booleans (from the existing Gemini authenticity signals: company_name,
company_logo, company_chop, signature — see AUTHENTICITY_PROMPT in
authenticity_check.py, which this file does NOT modify) plus whether the
document's own reference number was extracted (invoice_number/po_number/
gr_number — already available wherever authenticity_checks is joined
against extracted_fields/purchase_orders/goods_receipts), and turns them
into a score.

Callers are expected to invoke compute_authentication() at READ time
(e.g. in routes/authenticity.py, using data already fetched from the DB)
rather than persist its output — no new database column is needed, and
none is added here.
"""

# ── Rule configuration ──────────────────────────────────────────
# Each document type's signals, grouped by how much an auditor should
# care if that signal is missing:
#   required  — should always be present; missing it is a real concern
#   important — normally present; missing it is worth a second look
#   optional  — commonly absent for legitimate reasons on this doc type
#     (e.g. a digitally-generated PO has no wet-ink signature/chop by
#     design; a Goods Receipt's chop matters far more than a signature)
#
# 'doc_number' represents the document's OWN reference number
# (invoice_number / po_number / gr_number) — not a Gemini authenticity
# signal, but an existing extracted field already available to callers,
# scored here as a required identity check per document type.
#
# Deliberately NOT included: supplier_address, tax_number, received_stamp
# — these have no detector in AUTHENTICITY_PROMPT today (Task 6 keeps
# Gemini vision detection unchanged: company_name/company_logo/
# company_chop/signature only). Including undetectable signals would
# silently cap every document's achievable score below 100. They can be
# added here once (if ever) a corresponding signal is actually detected.
# Note: AUTHENTICITY_PROMPT's has_company_chop already treats a
# "RECEIVED"-style stamp as a chop signal, so a Goods Receipt's chop
# detection already substantively covers the "receiving stamp" concept.
AUTH_RULES = {
    'invoice': {
        'required':  ['company_name', 'doc_number'],
        'important': ['company_logo', 'company_chop'],
        'optional':  ['signature'],
    },
    'po': {
        'required':  ['company_name', 'doc_number'],
        'important': ['company_logo'],
        'optional':  ['company_chop', 'signature'],
    },
    'gr': {
        'required':  ['company_name', 'doc_number'],
        'important': ['company_chop'],
        'optional':  ['signature', 'company_logo'],
    },
}

# Soft default for any document_type not in AUTH_RULES (mirrors the old
# _compute_authenticity_status's "unknown doc type -> defensive default"
# behavior): only company identity is required, everything else is a
# bonus rather than an expectation.
_DEFAULT_RULES = {
    'required':  ['company_name'],
    'important': [],
    'optional':  ['company_logo', 'company_chop', 'signature', 'doc_number'],
}

# 'grn' accepted as an alias for 'gr', matching the old
# _compute_authenticity_status's tolerance for that spelling.
_DOC_TYPE_ALIASES = {'grn': 'gr'}

WEIGHTS = {
    'required':  30,
    'important': 15,
    'optional':  5,
}

_SIGNAL_LABELS = {
    'company_name': 'Company Name',
    'company_logo': 'Company Logo',
    'company_chop': 'Company Chop',
    'signature':    'Signature',
}
_DOC_NUMBER_LABELS = {
    'invoice': 'Invoice Number',
    'po':      'PO Number',
    'gr':      'GR Number',
}

_DOC_LABELS = {
    'invoice': 'Invoice',
    'po':      'Purchase Order',
    'gr':      'Goods Receipt',
}

# Task 3: base 0-100 bands, with a document-specific PASS cutoff that can
# raise the bar above the base 80 (e.g. Goods Receipt) or lower it (e.g.
# Invoice/PO both pass at 70, reflecting that a signature/chop being
# optional on those types is normal, not a sign of a weaker document).
# FAIL is always the same floor (score < 50) regardless of document
# type — REVIEW is deliberately the "wide middle" band between "clearly
# has problems" and "clearly fine for this document type".
_FAIL_THRESHOLD = 50
_DEFAULT_PASS_THRESHOLD = 80
_PASS_THRESHOLDS = {
    'invoice': 70,
    'po':      70,
    'gr':      80,
}


def _normalize_doc_type(document_type):
    doc_type = (document_type or '').lower()
    return _DOC_TYPE_ALIASES.get(doc_type, doc_type)


def _rules_for(doc_type):
    return AUTH_RULES.get(doc_type, _DEFAULT_RULES)


def _signal_label(signal, doc_type):
    if signal == 'doc_number':
        return _DOC_NUMBER_LABELS.get(doc_type, 'Document Number')
    return _SIGNAL_LABELS.get(signal, signal.replace('_', ' ').title())


def _signal_message(category, detected, label, doc_label):
    """Task 4: replace misleading flat "X not detected" phrasing with a
    message that reflects whether this signal actually matters for this
    document type. Only returned for signals that are NOT detected —
    a detected signal needs no explanatory message (mirrors the Task 5
    example, where the present "Company Name" entry has no message key
    but the missing "Signature" entry does)."""
    if detected:
        return None
    if category == 'required':
        return f'{label} is required for {doc_label} authentication and was not detected.'
    if category == 'important':
        return f'{label} is recommended for {doc_label} authentication but was not detected.'
    return f'{label} is not required for {doc_label} authentication.'


def _status_for_score(score, doc_type):
    if score < _FAIL_THRESHOLD:
        return 'FAIL'
    pass_threshold = _PASS_THRESHOLDS.get(doc_type, _DEFAULT_PASS_THRESHOLD)
    if score >= pass_threshold:
        return 'PASS'
    return 'REVIEW'


def _build_summary(doc_type, status, score, signal_details):
    doc_label = _DOC_LABELS.get(doc_type, 'Document')
    detected_count = sum(1 for d in signal_details if d['detected'])
    total_count = len(signal_details)
    missing_required = [d['name'] for d in signal_details if d['category'] == 'required' and not d['detected']]

    summary = f'{doc_label} authentication: {detected_count}/{total_count} signals detected ({score}/100) — {status}.'
    if missing_required:
        summary += f' Missing required: {", ".join(missing_required)}.'
    return summary


def compute_authentication(document_type, detected_signals):
    """
    document_type: 'invoice' | 'po' | 'gr' (any other value falls back
      to _DEFAULT_RULES, a soft "company identity only" rule set).
    detected_signals: dict of booleans for whichever of 'company_name',
      'company_logo', 'company_chop', 'signature', 'doc_number' apply —
      a missing key is treated as not detected (False), so callers only
      need to pass the keys they actually have data for.

    Returns:
      {
        'authentication_score': int (0-100, normalized — see below),
        'authentication_status': 'PASS' | 'REVIEW' | 'FAIL',
        'authentication_summary': str,
        'signal_details': [
          {'name': str, 'category': 'required'|'important'|'optional',
           'detected': bool, 'score': int, 'message': str (only when not detected)},
          ...
        ],
      }

    Scoring: each configured signal contributes its tier's weight
    (required=30, important=15, optional=5) IF detected, 0 if not. The
    raw point total is then normalized to a 0-100 scale by dividing by
    the maximum possible total for that document type's configured
    signals (different document types have different numbers of
    required/important/optional signals, so their raw maximums differ —
    normalizing keeps the PASS/REVIEW/FAIL thresholds meaning the same
    relative thing for every document type). Each individual signal's
    un-normalized point contribution is still reported in signal_details
    ('score': 30/15/5/0) for transparency about what earned/lost points.
    """
    doc_type = _normalize_doc_type(document_type)
    rules = _rules_for(doc_type)
    doc_label = _DOC_LABELS.get(doc_type, 'Document')

    signal_details = []
    raw_points = 0
    max_points = 0

    for category in ('required', 'important', 'optional'):
        weight = WEIGHTS[category]
        for signal in rules.get(category, []):
            detected = bool(detected_signals.get(signal, False))
            label = _signal_label(signal, doc_type)
            points = weight if detected else 0
            raw_points += points
            max_points += weight

            entry = {
                'name':     label,
                'category': category,
                'detected': detected,
                'score':    points,
            }
            message = _signal_message(category, detected, label, doc_label)
            if message is not None:
                entry['message'] = message
            signal_details.append(entry)

    score = round((raw_points / max_points) * 100) if max_points > 0 else 0
    status = _status_for_score(score, doc_type)
    summary = _build_summary(doc_type, status, score, signal_details)

    return {
        'authentication_score':   score,
        'authentication_status':  status,
        'authentication_summary': summary,
        'signal_details':         signal_details,
    }
