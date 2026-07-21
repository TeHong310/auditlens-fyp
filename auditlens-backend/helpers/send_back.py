"""Pure validation + presentation helpers for the auditor <-> Finance
send-back/correction workflow (routes/reviews.py). Kept separate from the
route functions, same pattern as helpers/authenticity_check.py, so the
validation rules are unit-testable without a Flask request context or a
real DB connection.

No AI calls, no DB access — every function here takes plain dicts/values
and returns plain dicts/values.
"""
from datetime import date, datetime

REASON_CATEGORIES = (
    'missing_document',
    'incorrect_extracted_information',
    'invoice_po_gr_mismatch',
    'possible_duplicate_invoice',
    'authenticity_evidence_requires_clarification',
    'incorrect_supplier_information',
    'amount_or_quantity_requires_verification',
    'other',
)

REQUIRED_ACTIONS = (
    'upload_missing_document',
    'correct_extracted_information',
    'provide_written_explanation',
    'confirm_duplicate_submission',
    'replace_incorrect_document',
    'verify_amount_or_quantity',
    'confirm_supplier_information',
    'other',
)

PRIORITIES = ('normal', 'medium', 'high')


def _parse_date(value):
    """Returns a date object, or None if value is missing/unparseable."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except ValueError:
        return None


def validate_send_back_payload(data, today=None):
    """Validates an auditor's structured send-back request (Feature 1).
    `today` is injectable for deterministic testing; defaults to the real
    current date. Returns (errors: list[str], cleaned: dict|None) — cleaned
    is None whenever errors is non-empty."""
    today = today or date.today()
    errors = []

    reason_category = (data.get('reason_category') or '').strip()
    if not reason_category:
        errors.append('reason_category is required')
    elif reason_category not in REASON_CATEGORIES:
        errors.append(f'reason_category must be one of {REASON_CATEGORIES}')

    reason_other_note = (data.get('reason_other_note') or '').strip()
    if reason_category == 'other' and not reason_other_note:
        errors.append('reason_other_note is required when reason_category is "other"')

    instruction = (data.get('instruction') or '').strip()
    if not instruction:
        errors.append('instruction is required')

    required_actions = data.get('required_actions')
    if not isinstance(required_actions, list) or len(required_actions) == 0:
        errors.append('required_actions must be a non-empty list')
        required_actions = []
    else:
        invalid = [a for a in required_actions if a not in REQUIRED_ACTIONS]
        if invalid:
            errors.append(f'required_actions contains invalid values: {invalid}')

    required_action_other_note = (data.get('required_action_other_note') or '').strip()
    if 'other' in (required_actions or []) and not required_action_other_note:
        errors.append('required_action_other_note is required when required_actions includes "other"')

    priority = (data.get('priority') or 'normal').strip().lower()
    if priority not in PRIORITIES:
        errors.append(f'priority must be one of {PRIORITIES}')

    due_date_raw = data.get('due_date')
    due_date = _parse_date(due_date_raw)
    if due_date_raw and due_date is None:
        errors.append('due_date must be a valid date (YYYY-MM-DD)')
    elif due_date and due_date < today:
        errors.append('due_date cannot be earlier than today')
    elif priority == 'high' and not due_date:
        errors.append('due_date is required for high-priority send-back requests')

    if errors:
        return errors, None

    return [], {
        'reason_category':             reason_category,
        'reason_other_note':           reason_other_note or None,
        'instruction':                 instruction,
        'required_actions':            required_actions,
        'required_action_other_note':  required_action_other_note or None,
        'priority':                    priority,
        'due_date':                    due_date,
    }


def validate_finance_response_payload(data):
    """Validates Finance's written response, required before resubmission
    (Feature 3). Returns (errors: list[str], response: str|None)."""
    response = (data.get('response') or '').strip()
    if not response:
        return ['response is required before resubmitting'], None
    return [], response


def compute_activity_summary(cycle, invoice_edited_at, po_uploaded_at, gr_uploaded_at):
    """A reliable, timestamp-based activity summary for 'Changes Since
    Send Back' (Feature 4) — never a fabricated field-level diff. Only
    reports an activity when the underlying record's own real timestamp
    is AFTER this cycle's sent_back_at, which is the only signal this
    schema can support without a field-history/versioning table."""
    sent_back_at = cycle.get('sent_back_at')
    if not sent_back_at:
        return []

    summary = []
    if invoice_edited_at and invoice_edited_at > sent_back_at:
        summary.append('Invoice fields were corrected')
    if po_uploaded_at and po_uploaded_at > sent_back_at:
        summary.append('Purchase Order was uploaded or replaced')
    if gr_uploaded_at and gr_uploaded_at > sent_back_at:
        summary.append('Goods Receipt was uploaded or replaced')
    if cycle.get('finance_response'):
        summary.append('Finance response added')
    return summary


def is_overdue(cycle, today=None):
    """A cycle is overdue only while it's still awaiting Finance (not yet
    resubmitted/resolved) and its due date has passed."""
    today = today or date.today()
    if cycle.get('cycle_status') in ('resubmitted', 'resolved'):
        return False
    due_date = cycle.get('response_due_date')
    if not due_date:
        return False
    if isinstance(due_date, str):
        due_date = _parse_date(due_date)
    return bool(due_date and due_date < today)
