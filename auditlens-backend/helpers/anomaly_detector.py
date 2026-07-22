import re
import json
import statistics
from datetime import datetime, timedelta, date
import psycopg2.extras
from db import get_db_connection
from config import Config
from helpers.gemini_extractor import call_gemini_sdk
from helpers.document_relationships import get_related_purchase_orders

AMOUNT_HISTORY_SAMPLE_LIMIT = 50
DUPLICATE_DATE_WINDOW_DAYS = 7
DUPLICATE_AMOUNT_TOLERANCE_PCT = 5

PROMPT_TEMPLATE = """You are a financial audit expert reviewing a potential anomaly in an SME invoice.

ANOMALY DETECTED: {anomaly_type}
SEVERITY: {severity}

RAW SIGNALS:
{signals_json}

VENDOR CONTEXT:
{vendor_context}

Provide:
1. A concise 2-3 sentence explanation of WHY this is anomalous, referencing the specific numbers
2. A concise 1-2 sentence recommendation for the auditor

Return ONLY JSON: {{"explanation": "...", "recommendation": "..."}}
No markdown, no code fences."""


def _normalize_vendor(name):
    if not name:
        return ''
    v = name.lower()
    v = re.sub(r'[.,()]', '', v)
    v = re.sub(r'\bsdn\s*bhd\b', '', v)
    v = re.sub(r'\bberhad\b', '', v)
    v = re.sub(r'\s+', ' ', v).strip()
    return v


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def detect_amount_anomaly(invoice_document_id, vendor_name, amount):
    normalized = _normalize_vendor(vendor_name)
    if not normalized or amount is None:
        return None

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(
        '''SELECT ef.vendor_name, ef.total_amount, ef.invoice_date
           FROM extracted_fields ef
           JOIN documents d ON ef.document_id = d.document_id
           WHERE d.status != 'returned' AND ef.document_id != %s
             AND ef.total_amount IS NOT NULL AND ef.invoice_date IS NOT NULL''',
        (invoice_document_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    # Most recent AMOUNT_HISTORY_SAMPLE_LIMIT invoices for THIS vendor,
    # not the N most recent system-wide — the LIMIT has to apply after
    # the vendor filter, or a busy period for other vendors could push
    # this vendor's own history out of the window entirely.
    vendor_rows = [r for r in rows if _normalize_vendor(r['vendor_name']) == normalized]
    vendor_rows.sort(key=lambda r: r['invoice_date'], reverse=True)
    history = [float(r['total_amount']) for r in vendor_rows[:AMOUNT_HISTORY_SAMPLE_LIMIT]]

    print(f"DEBUG Amount detector for vendor={vendor_name}")
    print(f"DEBUG Historical sample size: {len(history)}")

    if len(history) < 3:
        print("DEBUG Threshold check: SKIPPED (fewer than 3 historical invoices)")
        return None

    mean = statistics.mean(history)
    std = statistics.stdev(history)
    current = float(amount)

    if mean <= 0:
        print("DEBUG Threshold check: SKIPPED (baseline mean is zero)")
        return None

    deviation_pct = (current - mean) / mean * 100
    flagged = current > mean + 2 * std or current > 3 * mean

    print(f"DEBUG Baseline mean=RM {mean:.2f}, std=RM {std:.2f}")
    print(f"DEBUG Current amount=RM {current:.2f}, deviation={deviation_pct:.1f}%")
    print(f"DEBUG Threshold check: {'EXCEEDED' if flagged else 'PASSED'}")

    if not flagged:
        return None

    ratio = current / mean
    if ratio > 5:
        severity = 'high'
    elif ratio > 3:
        severity = 'medium'
    else:
        # Covers the mean + 2*std trigger for cases that don't reach the
        # 2x/3x/5x ratio bands the spec defines explicitly.
        severity = 'low'

    return {
        'type': 'amount',
        'severity': severity,
        'pattern': {
            'current': current,
            'mean': round(mean, 2),
            'std': round(std, 2),
            'deviation_pct': round(deviation_pct, 1),
            'sample_size': len(history)
        }
    }


def detect_round_amount(amount):
    if amount is None:
        return None
    amount = float(amount)
    if amount >= 1000 and abs(amount % 500) < 0.01:
        return {
            'type': 'round',
            'severity': 'medium',
            'pattern': {'amount': amount, 'roundness': 'exact_500'}
        }
    return None


def detect_weekend_transaction(invoice_date):
    d = _as_date(invoice_date)
    if not d or d.weekday() not in (5, 6):
        return None
    return {
        'type': 'weekend',
        'severity': 'low',
        'pattern': {'date': d.isoformat(), 'day_of_week': d.strftime('%A')}
    }


def _shares_linked_purchase_order(document_id_a, document_id_b):
    """True if both invoices are linked (via Phase 1's document_
    relationships, directly or through the legacy one-to-one fallback)
    to at least one common purchase order — evidence of legitimate split
    invoicing. Used ONLY to suppress a same-vendor/same-amount/close-
    date duplicate false positive (Enterprise V3 Phase 2, STEP 10). No
    AI call, no schema change."""
    po_ids_a = {po['po_id'] for po in get_related_purchase_orders('invoice', document_id_a)}
    if not po_ids_a:
        return False
    po_ids_b = {po['po_id'] for po in get_related_purchase_orders('invoice', document_id_b)}
    return bool(po_ids_a & po_ids_b)


def detect_duplicate_suspicion(invoice_document_id, vendor_name, amount, invoice_date, invoice_number=None):
    normalized = _normalize_vendor(vendor_name)
    d = _as_date(invoice_date)
    if not normalized or amount is None or not d:
        return None
    amount = float(amount)

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(
        '''SELECT ef.document_id, ef.invoice_number, ef.vendor_name, ef.invoice_date, ef.total_amount
           FROM extracted_fields ef
           WHERE ef.document_id != %s AND ef.vendor_name IS NOT NULL
             AND ef.invoice_date IS NOT NULL AND ef.total_amount IS NOT NULL
             AND ef.invoice_date BETWEEN %s AND %s''',
        (invoice_document_id, d - timedelta(days=DUPLICATE_DATE_WINDOW_DAYS),
         d + timedelta(days=DUPLICATE_DATE_WINDOW_DAYS))
    )
    rows = cursor.fetchall()
    conn.close()

    candidates = []
    for r in rows:
        if _normalize_vendor(r['vendor_name']) != normalized:
            continue
        matched_amount = float(r['total_amount'])
        if matched_amount == 0:
            continue
        diff_pct = (amount - matched_amount) / matched_amount * 100
        if abs(diff_pct) > DUPLICATE_AMOUNT_TOLERANCE_PCT:
            continue
        days_apart = abs((r['invoice_date'] - d).days)
        candidates.append((days_apart, r, diff_pct))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    days_apart, match, diff_pct = candidates[0]

    # Enterprise V3 Phase 2 compatibility (STEP 10): two DIFFERENTLY-
    # NUMBERED invoices that are both linked to the SAME purchase order
    # are legitimate split invoices, not a duplicate — same vendor and
    # equal/close amount is exactly the pattern many-to-many matching
    # introduces (e.g. a PO split into two equal-value invoices).
    # Invoice number remains the critical signal: a duplicate still
    # fires when the numbers match (or either is unknown).
    if invoice_number and match['invoice_number'] and invoice_number != match['invoice_number']:
        if _shares_linked_purchase_order(invoice_document_id, match['document_id']):
            return None

    return {
        'type': 'duplicate',
        'severity': 'high',
        'pattern': {
            'matched_invoice_no': match['invoice_number'],
            'matched_date': match['invoice_date'].isoformat(),
            'days_apart': days_apart,
            'amount_diff_pct': round(diff_pct, 2)
        }
    }


def _strip_markdown_fences(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _fallback_explanation(anomaly_type, pattern):
    if anomaly_type == 'amount':
        return {
            'explanation': (
                f"Invoice amount RM {pattern.get('current')} is well above this vendor's "
                f"average of RM {pattern.get('mean')} "
                f"(based on the last {pattern.get('sample_size')} invoices), "
                f"a deviation of {pattern.get('deviation_pct')}%."
            ),
            'recommendation': 'Verify this amount with the vendor before approving.'
        }
    if anomaly_type == 'round':
        return {
            'explanation': (
                f"The invoice amount RM {pattern.get('amount')} is an unusually round figure, "
                "which can indicate an estimated or fabricated amount rather than an itemized bill."
            ),
            'recommendation': 'Cross-check against a detailed quotation or itemized breakdown before approving.'
        }
    if anomaly_type == 'weekend':
        return {
            'explanation': (
                f"The invoice is dated {pattern.get('date')}, a {pattern.get('day_of_week')}, "
                "which is unusual for typical business operations."
            ),
            'recommendation': 'Confirm with the vendor that this date is correct and reflects an actual transaction.'
        }
    if anomaly_type == 'duplicate':
        return {
            'explanation': (
                f"This invoice closely matches invoice {pattern.get('matched_invoice_no')} "
                f"dated {pattern.get('matched_date')} ({pattern.get('days_apart')} days apart, "
                f"amount differs by {pattern.get('amount_diff_pct')}%), suggesting a possible "
                "duplicate submission."
            ),
            'recommendation': 'Compare both invoices side-by-side before approving to rule out duplicate payment.'
        }
    return {
        'explanation': 'An anomaly was detected but could not be automatically explained.',
        'recommendation': 'Manually review this record before approving.'
    }


def get_gemini_explanation(anomaly_signals, vendor_context):
    anomaly_type = anomaly_signals.get('type')
    severity = anomaly_signals.get('severity')
    pattern = anomaly_signals.get('pattern', {})
    fallback = _fallback_explanation(anomaly_type, pattern)

    if not Config.GEMINI_API_KEY:
        return fallback

    try:
        prompt = PROMPT_TEMPLATE.format(
            anomaly_type=anomaly_type,
            severity=severity,
            signals_json=json.dumps(pattern, indent=2, default=str),
            vendor_context=vendor_context
        )
        text = call_gemini_sdk(prompt, context='anomaly explanation')
        if text is None:
            return fallback
        parsed = json.loads(_strip_markdown_fences(text))

        explanation = parsed.get('explanation')
        recommendation = parsed.get('recommendation')
        if not explanation or not recommendation:
            return fallback
        return {'explanation': explanation, 'recommendation': recommendation}

    except Exception as e:
        print(f"DEBUG Gemini anomaly explanation error: {type(e).__name__}: {e}")
        return fallback


def run_anomaly_detection(invoice_document_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(
        '''SELECT invoice_number, vendor_name, invoice_date, total_amount
           FROM extracted_fields WHERE document_id = %s''',
        (invoice_document_id,)
    )
    fields = cursor.fetchone()
    conn.close()

    if not fields:
        return []

    vendor_name = fields['vendor_name']
    invoice_date = fields['invoice_date']
    amount = fields['total_amount']
    invoice_number = fields['invoice_number']

    candidates = [
        detect_amount_anomaly(invoice_document_id, vendor_name, amount),
        detect_round_amount(amount),
        detect_weekend_transaction(invoice_date),
        detect_duplicate_suspicion(invoice_document_id, vendor_name, amount, invoice_date, invoice_number),
    ]
    found = [a for a in candidates if a]
    if not found:
        return []

    vendor_context = (
        f"Vendor: {vendor_name or 'Unknown'}. "
        f"Invoice: {invoice_number or 'N/A'}, dated {invoice_date}, amount RM {amount}."
    )

    conn = get_db_connection()
    cursor = conn.cursor()
    created_ids = []
    try:
        for anomaly in found:
            ai = get_gemini_explanation(anomaly, vendor_context)
            cursor.execute(
                '''INSERT INTO anomalies
                   (invoice_document_id, anomaly_type, severity, detected_pattern,
                    ai_explanation, ai_recommendation)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING anomaly_id''',
                (invoice_document_id, anomaly['type'], anomaly['severity'],
                 json.dumps(anomaly['pattern'], default=str),
                 ai['explanation'], ai['recommendation'])
            )
            created_ids.append(cursor.fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    return created_ids
