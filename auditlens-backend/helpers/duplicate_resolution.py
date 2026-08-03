"""Finance-side resolution of a 'possible duplicate invoice' send-back —
the counterpart to the auditor's existing structured send-back (helpers/
send_back.py's 'possible_duplicate_invoice' reason_category /
'confirm_duplicate_submission' required_action). Kept separate from
send_back.py since it's specifically about the duplicate-suspicion
lifecycle (reading the anomaly engine's own finding, retiring a
confirmed duplicate), not the general send-back/resubmit payload shape.

No AI calls. No new detection logic - reuses the SAME duplicate match
already computed and cached by helpers/anomaly_detector.py's
detect_duplicate_suspicion() at the time the anomaly was first raised.
"""

# A withdrawn duplicate is a TERMINAL status: the case is closed, the
# document is confirmed a duplicate of another real transaction, and it
# must disappear from every active queue/dashboard/anomaly count from
# this point on - but the row itself, and every related record
# (extracted_fields, review_records, send_back_cycles, anomalies,
# audit_logs), is never deleted. Every query that lists "active"
# documents across the app excludes this one literal value.
WITHDRAWN_DUPLICATE_STATUS = 'withdrawn_duplicate'


def get_suspected_original(cursor, document_id):
    """Returns the suspected-original invoice info for THIS document's
    most recent 'duplicate' anomaly finding, or None if this document
    has no such finding (e.g. the auditor chose 'possible_duplicate_
    invoice' manually without an anomaly ever having fired - the
    'possible duplicate' send-back reason and the anomaly engine's own
    duplicate detector are independent; this only surfaces a result
    when they agree). Reads detected_pattern.matched_document_id -
    added alongside matched_invoice_no/matched_date so this lookup
    never has to re-run duplicate detection, only read what's already
    stored on the cached anomaly row.

    `cursor` must be a RealDictCursor (matches every other read in this
    codebase's route handlers)."""
    cursor.execute(
        '''SELECT detected_pattern FROM anomalies
           WHERE invoice_document_id = %s AND anomaly_type = 'duplicate'
           ORDER BY created_at DESC LIMIT 1''',
        (document_id,)
    )
    row = cursor.fetchone()
    if not row or not row.get('detected_pattern'):
        return None

    pattern = row['detected_pattern']
    matched_document_id = pattern.get('matched_document_id')
    if not matched_document_id:
        # Anomaly rows raised before this field was added (see
        # anomaly_detector.py) have no matched_document_id to look up -
        # degrade gracefully to "no suspected original available"
        # rather than erroring.
        return None

    cursor.execute(
        '''SELECT d.document_id, d.status, ef.invoice_number, ef.vendor_name,
                  ef.invoice_date, ef.total_amount, ef.currency
           FROM documents d
           LEFT JOIN extracted_fields ef ON ef.document_id = d.document_id
           WHERE d.document_id = %s''',
        (matched_document_id,)
    )
    original = cursor.fetchone()
    if not original:
        return None

    return {
        'document_id':     original['document_id'],
        'status':          original['status'],
        'invoice_number':  original['invoice_number'],
        'vendor_name':     original['vendor_name'],
        'invoice_date':    original['invoice_date'].isoformat() if original['invoice_date'] else None,
        'total_amount':    float(original['total_amount']) if original['total_amount'] is not None else None,
        'currency':        original['currency'],
        'days_apart':      pattern.get('days_apart'),
        'amount_diff_pct': pattern.get('amount_diff_pct'),
    }
