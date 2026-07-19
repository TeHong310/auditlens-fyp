import re
import csv
import io
from datetime import datetime, timedelta, date
from flask import Blueprint, jsonify, request, Response
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.ocr_helper import split_item_code_prefix

auditor_bp = Blueprint('auditor', __name__)


def _normalize_vendor(name):
    if not name:
        return ''
    v = name.lower()
    v = re.sub(r'[.,()]', '', v)
    v = re.sub(r'\bsdn\s*bhd\b', '', v)
    v = re.sub(r'\bberhad\b', '', v)
    v = re.sub(r'\s+', ' ', v).strip()
    return v


def _amounts_equal(a, b):
    if a is None or b is None:
        return None
    try:
        return abs(float(a) - float(b)) < 0.01
    except (TypeError, ValueError):
        return None


def _normalize_currency(cur):
    """Canonical form of a currency code/symbol, for BOTH comparison and
    display: 'RM'/'MYR'/'MYR (RM)' are all the same Malaysian Ringgit,
    just different notations depending on which extractor produced them
    (regex fallback defaults to 'MYR', Gemini sometimes returns 'RM') —
    without this, a genuinely equal amount in the same currency would
    show as "N/A" instead of "Match" just because one side said 'MYR'
    and the other said 'RM'. Only a GENUINELY different currency (USD,
    etc.) should ever compare as different. Returned value is also what's
    shown in the UI, so invoice/PO/GR display the same representation
    ('RM') for the same currency instead of whichever variant a given
    document's OCR/Gemini call happened to return."""
    if not cur:
        return None
    c = str(cur).upper()
    # Word-boundary match (not a stripped-then-equality check) so compound
    # notations like "MYR (RM)" — which would otherwise strip down to the
    # unrecognizable "MYRRM" — are still correctly recognized as Ringgit.
    if re.search(r'\b(?:RM|MYR)\b', c):
        return 'RM'
    stripped = re.sub(r'[^A-Za-z]', '', c)
    return stripped or None


def _normalize_ref(val):
    """Normalize a PO reference/ID value for comparison: uppercase,
    strip all whitespace, then strip a leading "PO-"/"PO " prefix.
    Different documents' OCR extraction may or may not retain the "PO"
    prefix depending on layout — e.g. an invoice's "PO No: PO-2026-0087"
    line captures "PO-2026-0087", but a differently-worded label
    elsewhere might capture just "2026-0087" — they're the same
    reference and must compare equal, not show a false mismatch.
    None for empty/missing so it's excluded from the match set rather
    than compared as ''."""
    if not val:
        return None
    v = re.sub(r'\s+', '', str(val).upper())
    v = re.sub(r'^PO-?', '', v)
    return v or None


def _normalize_text(val):
    """Normalize free text (item/description) for comparison:
    lowercase, collapse whitespace."""
    if not val:
        return None
    v = re.sub(r'\s+', ' ', str(val).lower()).strip()
    return v or None


def _line_item_tokens(item):
    """(code_key, desc_key) for one line item, used by _find_line_item_
    match() below. code_key is the normalized item_code (uppercase,
    non-alphanumerics stripped) or None. desc_key is the normalized
    description via _normalize_text, with any leading item-code-shaped
    token STRIPPED first (split_item_code_prefix) — this is what makes
    matching robust even when item_code wasn't split out on one side:
    "SLT-MOS-N60R MOSFET N-Ch 600V TO-220" and "MOSFET N-Ch 600V TO-220"
    both reduce to the same desc_key regardless of which document's
    extraction path already normalized it (persistence-time
    normalization in routes/documents.py's _sanitize_line_items() is
    the primary fix for NEW uploads; this is the defense-in-depth
    fallback that also fixes matching for records already in the DB
    from before that fix existed)."""
    code = item.get('item_code')
    code_key = re.sub(r'[^A-Za-z0-9]', '', str(code)).upper() if code else None
    desc = item.get('description') or ''
    _, stripped = split_item_code_prefix(desc)
    desc_key = _normalize_text(stripped or desc)
    return code_key, desc_key


def _find_line_item_match(target, candidates, used):
    """Finds the first not-yet-used candidate matching `target`, trying
    (in order): exact item_code, exact normalized description, then a
    containment check (one normalized description contains the other —
    guards against a length-4 description trivially matching everything
    by requiring at least 6 normalized characters). Returns the matching
    index into `candidates`, or None."""
    target_code, target_desc = _line_item_tokens(target)

    if target_code:
        for i, cand in enumerate(candidates):
            if i in used:
                continue
            cand_code, _ = _line_item_tokens(cand)
            if cand_code and cand_code == target_code:
                return i

    if not target_desc:
        return None

    for i, cand in enumerate(candidates):
        if i in used:
            continue
        _, cand_desc = _line_item_tokens(cand)
        if cand_desc and cand_desc == target_desc:
            return i

    if len(target_desc) >= 6:
        for i, cand in enumerate(candidates):
            if i in used:
                continue
            _, cand_desc = _line_item_tokens(cand)
            if cand_desc and len(cand_desc) >= 6 and (target_desc in cand_desc or cand_desc in target_desc):
                return i

    return None


def _match_line_items(invoice_items, po_items, gr_items):
    """
    Matches line items across Invoice/PO/GR by item_code (when present)
    else normalized description — with a containment fallback so
    "SLT-MOS-N60R MOSFET N-Ch 600V TO-220" (code left inline, e.g. from
    the regex fallback or an older upload) still pairs with "MOSFET
    N-Ch 600V TO-220" + item_code "SLT-MOS-N60R" (code split out, e.g.
    by Gemini) — see _find_line_item_match(). Compares quantity/amount
    per matched item. Each of invoice_items/po_items/gr_items is a list
    of dicts (from document_line_items: 'item_code','description',
    'quantity','unit_price','amount') for a document that WAS uploaded,
    or None for a document that was NOT uploaded — that distinction
    matters for missing-item detection: an item can only be "missing on
    PO" if a PO actually exists and simply doesn't have it, not because
    no PO was uploaded at all (a separate, already-handled state).

    A document that WAS uploaded but produced ZERO line items (extraction
    failure, not a real "nothing ordered") is treated the same as "not
    uploaded" for missing-item purposes — otherwise a single failed
    extraction would flag EVERY invoice item as "missing" on that
    document, which is a false signal about extraction, not the audit.

    Returns (rows, has_hard_mismatch, has_soft_mismatch):
      rows: one dict per distinct matched item, in invoice-then-PO-only-
        then-GR-only order: {'description', 'item_code', 'invoice_
        quantity', 'po_quantity', 'gr_quantity', 'quantity_match',
        'amount_match', 'missing_on_po', 'missing_on_gr',
        'missing_on_invoice'}
      has_hard_mismatch: True if any item has a quantity mismatch or a
        missing-item finding (on ANY of the three documents, including
        an item present on PO/GR but missing from the invoice) — a HARD
        failure (escalates overall_status to FAIL), same severity as
        the existing vendor/amount/quantity checks.
      has_soft_mismatch: True if any item has an amount mismatch (with
        no hard issue on that same item) — a SOFT signal (REVIEW), same
        severity as the existing po_reference/item checks.
    """
    po_present = po_items is not None and len(po_items) > 0
    gr_present = gr_items is not None and len(gr_items) > 0
    po_items = po_items if po_present else []
    gr_items = gr_items if gr_present else []
    invoice_items = invoice_items or []

    po_used, gr_used = set(), set()
    rows = []
    has_hard_mismatch = False
    has_soft_mismatch = False

    def build_row(inv_item, po_item, gr_item):
        nonlocal has_hard_mismatch, has_soft_mismatch

        # Canonical label: prefer whichever side has item_code split out
        # already (the "clean" representation) over one that still has
        # it inline in description, so the row shows ONE consistent
        # label regardless of which document's extraction happened to
        # be picked — not two different formats for the same product.
        description = item_code = None
        for it in (inv_item, po_item, gr_item):
            if it and it.get('item_code'):
                description, item_code = it.get('description'), it.get('item_code')
                break
        if item_code is None:
            for it in (inv_item, po_item, gr_item):
                if it and it.get('description'):
                    description, item_code = it.get('description'), it.get('item_code')
                    break

        missing_on_invoice = inv_item is None
        missing_on_po = po_present and po_item is None
        missing_on_gr = gr_present and gr_item is None

        qty_values = [it['quantity'] for it in (inv_item, po_item, gr_item)
                      if it and it.get('quantity') is not None]
        quantity_match = None
        if len(qty_values) >= 2:
            quantity_match = all(abs(v - qty_values[0]) < 0.01 for v in qty_values[1:])

        amt_values = [it['amount'] for it in (inv_item, po_item, gr_item)
                      if it and it.get('amount') is not None]
        amount_match = None
        if len(amt_values) >= 2:
            amount_match = all(abs(v - amt_values[0]) < 0.01 for v in amt_values[1:])

        if quantity_match is False or missing_on_invoice or missing_on_po or missing_on_gr:
            has_hard_mismatch = True
        if amount_match is False:
            has_soft_mismatch = True

        rows.append({
            'description':        description,
            'item_code':          item_code,
            'invoice_quantity':   inv_item['quantity'] if inv_item else None,
            'po_quantity':        po_item['quantity'] if po_item else None,
            'gr_quantity':        gr_item['quantity'] if gr_item else None,
            'quantity_match':     quantity_match,
            'amount_match':       amount_match,
            'missing_on_invoice': missing_on_invoice,
            'missing_on_po':      missing_on_po,
            'missing_on_gr':      missing_on_gr,
        })

    # Anchor on invoice items first (existing row-ordering convention:
    # invoice-then-PO-only-then-GR-only), pairing each with its best
    # match (if any) among the not-yet-used PO/GR items.
    for inv_item in invoice_items:
        po_idx = _find_line_item_match(inv_item, po_items, po_used) if po_present else None
        gr_idx = _find_line_item_match(inv_item, gr_items, gr_used) if gr_present else None
        if po_idx is not None:
            po_used.add(po_idx)
        if gr_idx is not None:
            gr_used.add(gr_idx)
        build_row(inv_item, po_items[po_idx] if po_idx is not None else None,
                  gr_items[gr_idx] if gr_idx is not None else None)

    # PO items with no invoice counterpart — still try to pair with GR.
    for i, po_item in enumerate(po_items):
        if i in po_used:
            continue
        gr_idx = _find_line_item_match(po_item, gr_items, gr_used) if gr_present else None
        if gr_idx is not None:
            gr_used.add(gr_idx)
        build_row(None, po_item, gr_items[gr_idx] if gr_idx is not None else None)

    # GR items with no invoice/PO counterpart at all.
    for i, gr_item in enumerate(gr_items):
        if i in gr_used:
            continue
        build_row(None, None, gr_item)

    return rows, has_hard_mismatch, has_soft_mismatch


def _three_way_match(values):
    """True if every present (non-None) value is identical, False if
    they differ, None if fewer than 2 are present to compare."""
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return None
    return len(set(present)) <= 1


def _quantities_match(values):
    """Same idea as _three_way_match but with numeric tolerance (OCR/
    float noise), like _amounts_equal."""
    present = []
    for v in values:
        if v is None:
            continue
        try:
            present.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(present) < 2:
        return None
    first = present[0]
    return all(abs(v - first) < 0.01 for v in present[1:])


def _build_comparison(cursor, invoice_document_id):
    """Shared by GET /record/<id>/comparison and the exceptions detector,
    so match logic only lives in one place. Returns None if the invoice
    document doesn't exist."""
    cursor.execute(
        '''SELECT d.document_id, d.file_name, d.uploaded_at,
                  ef.invoice_number, ef.vendor_name, ef.invoice_date,
                  ef.total_amount, ef.ocr_confidence, ef.currency,
                  ef.po_reference, ef.item_description, ef.quantity
           FROM documents d
           LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
           WHERE d.document_id = %s''',
        (invoice_document_id,)
    )
    inv_row = cursor.fetchone()
    if not inv_row:
        return None

    cursor.execute(
        '''SELECT po_id, file_name, po_number, vendor_name, po_date, total_amount,
                  currency, item_description, quantity
           FROM purchase_orders WHERE document_id = %s
           ORDER BY uploaded_at DESC LIMIT 1''',
        (invoice_document_id,)
    )
    po_row = cursor.fetchone()

    cursor.execute(
        '''SELECT gr_id, file_name, gr_number, vendor_name, receipt_date,
                  po_reference, item_description, quantity
           FROM goods_receipts WHERE document_id = %s
           ORDER BY uploaded_at DESC LIMIT 1''',
        (invoice_document_id,)
    )
    gr_row = cursor.fetchone()

    # Line-item level 3-way matching (every row of each document's table,
    # not just the first) — document_line_items is keyed by the SAME
    # invoice document_id across all 3 types, matching how purchase_
    # orders/goods_receipts already key off it (not po_id/gr_id).
    cursor.execute(
        '''SELECT document_type, item_code, description, quantity, unit_price, amount
           FROM document_line_items WHERE document_id = %s ORDER BY document_type, line_no''',
        (invoice_document_id,)
    )
    line_item_rows_by_type = {'invoice': [], 'po': [], 'gr': []}
    for row in cursor.fetchall():
        doc_type = row['document_type']
        if doc_type in line_item_rows_by_type:
            line_item_rows_by_type[doc_type].append({
                'item_code':   row['item_code'],
                'description': row['description'],
                'quantity':    float(row['quantity']) if row['quantity'] is not None else None,
                'unit_price':  float(row['unit_price']) if row['unit_price'] is not None else None,
                'amount':      float(row['amount']) if row['amount'] is not None else None,
            })

    invoice = {
        'document_id':      inv_row['document_id'],
        'filename':         inv_row['file_name'],
        'ocr_confidence':   float(inv_row['ocr_confidence']) if inv_row['ocr_confidence'] is not None else None,
        'invoice_no':       inv_row['invoice_number'],
        'vendor_name':      inv_row['vendor_name'],
        'invoice_date':     inv_row['invoice_date'].isoformat() if inv_row['invoice_date'] else None,
        'total_amount':     float(inv_row['total_amount']) if inv_row['total_amount'] is not None else None,
        'currency':         _normalize_currency(inv_row['currency']),
        'uploaded_at':      inv_row['uploaded_at'].isoformat() if inv_row['uploaded_at'] else None,
        'po_reference':     inv_row['po_reference'],
        'item_description': inv_row['item_description'],
        'quantity':         float(inv_row['quantity']) if inv_row['quantity'] is not None else None,
    }

    po = None
    if po_row:
        po = {
            'po_id':            po_row['po_id'],
            'filename':         po_row['file_name'],
            'po_no':            po_row['po_number'],
            'vendor_name':      po_row['vendor_name'],
            'po_date':          po_row['po_date'].isoformat() if po_row['po_date'] else None,
            'total_amount':     float(po_row['total_amount']) if po_row['total_amount'] is not None else None,
            'currency':         _normalize_currency(po_row['currency']),
            'item_description': po_row['item_description'],
            'quantity':         float(po_row['quantity']) if po_row['quantity'] is not None else None,
        }

    gr = None
    if gr_row:
        gr = {
            'gr_id':            gr_row['gr_id'],
            'filename':         gr_row['file_name'],
            'gr_no':            gr_row['gr_number'],
            'vendor_name':      gr_row['vendor_name'],
            'receipt_date':     gr_row['receipt_date'].isoformat() if gr_row['receipt_date'] else None,
            'po_reference':     gr_row['po_reference'],
            'item_description': gr_row['item_description'],
            'quantity':         float(gr_row['quantity']) if gr_row['quantity'] is not None else None,
        }

    # ── Line items: EVERY row of each document's table, not just the
    # first — po_items/gr_items are None when that document type isn't
    # uploaded at all (a different state from "uploaded with 0 items"),
    # see _match_line_items()'s docstring for why that distinction
    # matters for missing-item detection. ──
    line_items, line_items_hard_mismatch, line_items_soft_mismatch = _match_line_items(
        line_item_rows_by_type['invoice'],
        line_item_rows_by_type['po'] if po else None,
        line_item_rows_by_type['gr'] if gr else None,
    )
    line_items_match = (not line_items_hard_mismatch) if line_items else None
    line_items_price_match = (not line_items_soft_mismatch) if line_items else None

    # ── Vendor match: compare normalized vendor_name across every
    # present doc (invoice always present; PO/GR only if uploaded) ──
    vendor_names = [invoice['vendor_name']]
    if po:
        vendor_names.append(po['vendor_name'])
    if gr:
        vendor_names.append(gr['vendor_name'])
    normalized = [_normalize_vendor(v) for v in vendor_names if v]
    vendor_match = len(set(normalized)) <= 1 if normalized else None

    # ── Amount match: Invoice vs PO only (GR carries no monetary
    # total by design). currency is already normalized above (RM/MYR
    # both become 'RM'), so this only treats amounts as "not applicable"
    # (None) when the two sides are in GENUINELY different currencies
    # (e.g. invoice in USD, PO in RM) — comparing USD against RM as if
    # they were the same unit would be meaningless, but RM vs MYR is the
    # SAME currency and must compare normally. ──
    if po and invoice['currency'] and po['currency'] and invoice['currency'] != po['currency']:
        amount_match = None
    else:
        amount_match = _amounts_equal(invoice['total_amount'], po['total_amount']) if po else None

    # ── PO reference match: the PO number each document independently
    # references — Invoice's po_reference (OCR'd "PO No:" line), the
    # PO's own po_number (its anchor identity), GR's po_reference
    # (OCR'd "PO No:" line). These are now real, regex-extracted fields
    # (previously hardcoded None — po_reference didn't exist as a
    # separate field on the invoice/GR OCR data until this change). ──
    po_refs = [_normalize_ref(invoice['po_reference'])]
    if po:
        po_refs.append(_normalize_ref(po['po_no']))
    if gr:
        po_refs.append(_normalize_ref(gr['po_reference']))
    po_reference_match = _three_way_match(po_refs)

    # ── Item/description match: best-effort regex extraction, so exact
    # text equality across independently-OCR'd documents is optimistic
    # — same simplification already used for amount/quantity (a single
    # representative value, not itemized line-by-line). ──
    items = [_normalize_text(invoice['item_description'])]
    if po:
        items.append(_normalize_text(po['item_description']))
    if gr:
        items.append(_normalize_text(gr['item_description']))
    item_match = _three_way_match(items)

    # ── Quantity match: THE key audit field — PO ordered vs GR received
    # vs Invoice billed. A mismatch here (e.g. ordered 100 / received 90
    # / billed 100) is exactly what this comparison exists to surface. ──
    quantities = [invoice['quantity']]
    if po:
        quantities.append(po['quantity'])
    if gr:
        quantities.append(gr['quantity'])
    quantity_match = _quantities_match(quantities)

    # ── Date order: PO date <= GR date <= Invoice date, skipping the
    # check if any required date is missing. Computed and still returned
    # in match_result (and still used as supplementary detail text in
    # _classify_exception below), but — as of this fix — deliberately
    # NOT one of the checks that can flip overall_status to FAIL on its
    # own. It was previously included there, but the Field Comparison
    # table stopped showing a Date row two entries ago; an invisible
    # check silently failing the whole banner with no visible row for
    # the auditor to investigate is exactly the bug this fix addresses
    # (a "perfect match" record — every visible field showing ✓ Match —
    # still showed "Mismatch Detected" because of this). ──
    date_order_valid = None
    if po and gr and po['po_date'] and gr['receipt_date'] and invoice['invoice_date']:
        date_order_valid = po['po_date'] <= gr['receipt_date'] <= invoice['invoice_date']
    elif po and invoice['invoice_date'] and po['po_date'] and not gr:
        date_order_valid = po['po_date'] <= invoice['invoice_date']
    elif gr and invoice['invoice_date'] and gr['receipt_date'] and not po:
        date_order_valid = gr['receipt_date'] <= invoice['invoice_date']

    # overall_status is driven by EVERY check that has a visible row/pill
    # in the Field Comparison table (Vendor, Amount, PO Ref, Line Items)
    # — invariant: the banner can say "All Fields Match" (PASS, green) if
    # and only if NONE of these rows is showing a Mismatch pill.
    # Previously po_reference_match/item_match were excluded here
    # entirely, which meant a record could show a real "✗ Mismatch" pill
    # on the PO Ref or Item row while the banner still read "All Fields
    # Match — Ready for approval" — a direct contradiction an auditor
    # could act on (approving a record with a visible mismatch).
    #
    # The single-value item_match/quantity_match (first line item only)
    # are STILL computed and returned in match_result below for backward
    # compatibility, but — like date_order_valid — deliberately EXCLUDED
    # from the checks that drive overall_status: the Field Comparison
    # table no longer has a single "Item / Description"/"Quantity" row
    # for them (replaced by the per-line-item Line Items section), so an
    # invisible check silently failing the banner with no matching visible
    # row is exactly the bug the date_order_valid fix addressed, and
    # would reintroduce it here for a different pair of fields.
    # line_items_match (any line item quantity-mismatched or missing on
    # PO/GR — a "critical audit finding") REPLACES quantity_match as the
    # HARD, line-item-level check; line_items_price_match (any matched
    # item's amount/unit_price disagrees) REPLACES item_match as the
    # SOFT check — both computed over EVERY line item, not just the
    # first, closing the "invoice with 3 items only ever compares item
    # #1" gap. vendor_match/amount_match are HARD mismatches (FAIL, red)
    # — the mature, document-level checks. po_reference_match/line_items_
    # price_match are SOFT mismatches (REVIEW, amber) — best-effort
    # fields more prone to false positives, so a soft mismatch alone
    # doesn't escalate all the way to FAIL, but it MUST still stop the
    # banner from claiming a clean match. Every check here already
    # correctly treats a missing/absent value as "not applicable" (None),
    # never as a false mismatch.
    hard_checks = [vendor_match, amount_match, line_items_match]
    soft_checks = [po_reference_match, line_items_price_match]
    applicable_checks = [c for c in hard_checks + soft_checks if c is not None]

    if any(c is False for c in hard_checks):
        overall_status = 'FAIL'
    elif any(c is False for c in soft_checks):
        overall_status = 'REVIEW'
    elif not po or not gr:
        overall_status = 'PARTIAL'
    elif applicable_checks:
        overall_status = 'PASS'
    else:
        overall_status = 'PARTIAL'

    # Per-check breakdown, always logged — this is exactly what to check
    # in Render's logs if overall_status ever looks wrong for a record:
    # every check that's None was skipped as "not applicable" (missing/
    # absent value), not a mismatch; only an explicit False is a real
    # disagreement.
    print(f"DEBUG comparison doc={invoice_document_id}: "
          f"vendor_match={vendor_match} amount_match={amount_match} "
          f"po_reference_match={po_reference_match} "
          f"line_items_match={line_items_match} line_items_price_match={line_items_price_match} "
          f"(legacy item_match={item_match} quantity_match={quantity_match} "
          f"date_order_valid={date_order_valid} all excluded from overall_status — see comment above) "
          f"-> overall_status={overall_status}")

    return {
        'invoice': invoice,
        'po': po,
        'gr': gr,
        'line_items': line_items,
        'match_result': {
            'vendor_match':           vendor_match,
            'amount_match':           amount_match,
            'po_reference_match':     po_reference_match,
            'line_items_match':       line_items_match,
            'line_items_price_match': line_items_price_match,
            'item_match':             item_match,
            'quantity_match':         quantity_match,
            'date_order_valid':       date_order_valid,
            'overall_status':         overall_status,
        }
    }


# ------------------------------------------------------------
# GET FULL 3-WAY COMPARISON FOR AN INVOICE RECORD
# GET /auditor/record/<invoice_document_id>/comparison
# Auditor only
# ------------------------------------------------------------
@auditor_bp.route('/record/<int:invoice_document_id>/comparison', methods=['GET'])
@jwt_required()
def get_record_comparison(invoice_document_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        result = _build_comparison(cursor, invoice_document_id)
        conn.close()

        if result is None:
            return jsonify({'error': 'Invoice document not found'}), 404

        return jsonify(result), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# EXCEPTION DETECTION
# Classifies one invoice's comparison result + document row into at
# most one exception (highest severity wins), or None if clean.
# Severity order: mismatch > sent_back = missing_document > low_confidence
# ------------------------------------------------------------
def _classify_exception(cursor, doc_row, comparison):
    candidates = []  # (rank, type, label, detail, severity)
    mr = comparison['match_result']

    if mr['overall_status'] == 'FAIL':
        parts = []
        label_parts = []
        if mr['vendor_match'] is False:
            parts.append('Vendor names differ')
            label_parts.append('Vendor')
        if mr['amount_match'] is False:
            inv_amt = comparison['invoice']['total_amount']
            inv_cur = comparison['invoice']['currency'] or 'RM'
            po_amt  = comparison['po']['total_amount'] if comparison['po'] else None
            po_cur  = (comparison['po']['currency'] or 'RM') if comparison['po'] else 'RM'
            parts.append(f"Amount differs: Invoice {inv_cur}{inv_amt} vs PO {po_cur}{po_amt}")
            label_parts.append('Amount')
        if mr['line_items_match'] is False:
            # Line-item level (every row, not just the first) — a
            # quantity mismatch OR a missing-item finding (present in one
            # document, legitimately absent in another uploaded one) is a
            # HARD failure per the request ("this is a critical audit
            # finding"), same severity as vendor/amount.
            bad_items = [
                li for li in comparison['line_items']
                if li['quantity_match'] is False or li['missing_on_po'] or li['missing_on_gr']
            ]
            item_parts = []
            for li in bad_items[:3]:
                desc = li['description'] or 'Unnamed item'
                if li['missing_on_po']:
                    item_parts.append(f'"{desc}" missing on PO')
                elif li['missing_on_gr']:
                    item_parts.append(f'"{desc}" missing on GR')
                else:
                    item_parts.append(
                        f'"{desc}": PO {li["po_quantity"]} vs GR {li["gr_quantity"]} vs Invoice {li["invoice_quantity"]}'
                    )
            if len(bad_items) > 3:
                item_parts.append(f'+{len(bad_items) - 3} more')
            parts.append('Line item mismatch: ' + '; '.join(item_parts))
            label_parts.append('Line Items')
        if mr['date_order_valid'] is False:
            parts.append('Document dates out of expected order')
            label_parts.append('Date')
        label = (' & '.join(label_parts) + ' Mismatch') if label_parts else 'Mismatch'
        detail = '; '.join(parts) or 'Fields do not match'
        candidates.append((4, 'mismatch', label, detail, 'high'))

    # REVIEW: a soft mismatch (PO Ref/Item) with no hard mismatch — same
    # reasoning as overall_status above, surfaced here too so a record
    # showing an amber "Review Required" banner also shows up as an
    # exception, rather than only being visible if an auditor happens to
    # open that specific record's detail page.
    if mr['overall_status'] == 'REVIEW':
        parts = []
        label_parts = []
        if mr['po_reference_match'] is False:
            parts.append('PO reference differs across documents')
            label_parts.append('PO Ref')
        if mr['line_items_price_match'] is False:
            parts.append('Line item amount/unit price differs across documents')
            label_parts.append('Line Item Amount')
        label = (' & '.join(label_parts) + ' Differs') if label_parts else 'Review Required'
        detail = '; '.join(parts) or 'Some fields differ — review recommended'
        candidates.append((2, 'review', label, detail, 'medium'))

    if doc_row['status'] == 'returned':
        cursor.execute(
            '''SELECT remarks FROM review_records
               WHERE document_id = %s AND action = 'returned'
               ORDER BY reviewed_at DESC LIMIT 1''',
            (doc_row['document_id'],)
        )
        remark_row = cursor.fetchone()
        detail = remark_row['remarks'] if remark_row and remark_row['remarks'] else 'Sent back to Finance for correction'
        candidates.append((3, 'sent_back', 'Sent Back to Finance', detail, 'medium'))

    if not comparison['po'] or not comparison['gr']:
        missing = []
        if not comparison['po']:
            missing.append('PO')
        if not comparison['gr']:
            missing.append('GR')
        label = 'Missing ' + ' and '.join(missing)
        candidates.append((3, 'missing_document', label,
                            f"Invoice uploaded but {' and '.join(missing)} not yet received", 'medium'))

    ocr_confidence = comparison['invoice']['ocr_confidence']
    if ocr_confidence is not None and ocr_confidence < 80:
        pct = round(ocr_confidence)
        candidates.append((1, 'low_confidence', f'Low OCR Confidence ({pct}%)',
                            f'OCR confidence {pct}% — verify extracted fields', 'low'))

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[0])
    return candidates[0]


# ------------------------------------------------------------
# GET EXCEPTIONS LIST
# GET /auditor/exceptions
# Auditor only
# ------------------------------------------------------------
@auditor_bp.route('/exceptions', methods=['GET'])
@jwt_required()
def get_exceptions():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    type_filter = request.args.get('type', 'all')
    limit  = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Scope: invoices still "in flight" for audit — under review,
        # resubmitted after correction, or just sent back (so the
        # sent_back exception type itself has something to surface).
        # Already-approved invoices are excluded; they're resolved.
        cursor.execute(
            '''SELECT document_id, uploaded_at, status
               FROM documents
               WHERE status IN ('under_review', 'resubmitted', 'returned')
               ORDER BY uploaded_at DESC'''
        )
        doc_rows = cursor.fetchall()

        exceptions = []
        for doc_row in doc_rows:
            comparison = _build_comparison(cursor, doc_row['document_id'])
            if not comparison:
                continue
            classified = _classify_exception(cursor, doc_row, comparison)
            if not classified:
                continue
            _, exc_type, label, detail, severity = classified

            if type_filter != 'all' and exc_type != type_filter:
                continue

            exceptions.append({
                'invoice_document_id': doc_row['document_id'],
                'invoice_no':          comparison['invoice']['invoice_no'],
                'vendor_name':         comparison['invoice']['vendor_name'],
                'uploaded_at':         comparison['invoice']['uploaded_at'],
                'exception_type':      exc_type,
                'exception_label':     label,
                'detail':              detail,
                'severity':            severity,
            })

        conn.close()

        return jsonify(exceptions[offset:offset + limit]), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# REPORT: SUMMARY STATS + 30-DAY TIMELINE
# GET /auditor/report/summary
# Auditor only
# ------------------------------------------------------------
def _period_start(period):
    now = datetime.utcnow()
    if period == 'today':
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        return now - timedelta(days=7)
    if period == 'month':
        return now - timedelta(days=30)
    return None  # 'all'


@auditor_bp.route('/report/summary', methods=['GET'])
@jwt_required()
def get_report_summary():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    period = request.args.get('period', 'month')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        period_start = _period_start(period)

        # ── Stats: approved/sent_back counted as events within the
        # period; pending/exceptions are current-state snapshots (a
        # "how many right now", not something a past period bounds). ──
        if period_start:
            cursor.execute(
                '''SELECT rr.action, COUNT(*) AS cnt
                   FROM review_records rr
                   JOIN users u ON rr.reviewed_by = u.user_id
                   WHERE u.role = 'auditor' AND rr.action IN ('approved', 'returned')
                     AND rr.reviewed_at >= %s
                   GROUP BY rr.action''',
                (period_start,)
            )
        else:
            cursor.execute(
                '''SELECT rr.action, COUNT(*) AS cnt
                   FROM review_records rr
                   JOIN users u ON rr.reviewed_by = u.user_id
                   WHERE u.role = 'auditor' AND rr.action IN ('approved', 'returned')
                   GROUP BY rr.action'''
            )
        action_counts = {row['action']: row['cnt'] for row in cursor.fetchall()}

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM documents WHERE status IN ('under_review', 'resubmitted')"
        )
        pending = cursor.fetchone()['cnt']

        # NOTE (perf): exceptions are recomputed per-call by scanning
        # every in-flight invoice through the same matching logic as
        # /auditor/exceptions, rather than reading a cached status
        # column. There's no such column in the current schema, and
        # this app's invoice volume is small (FYP/demo scale), so it's
        # fine for now — flagged for a real cache (e.g. a match_status
        # column updated on upload/approve/return) if volume grows.
        cursor.execute(
            '''SELECT document_id, uploaded_at, status
               FROM documents
               WHERE status IN ('under_review', 'resubmitted', 'returned')'''
        )
        doc_rows = cursor.fetchall()
        exception_count = 0
        for doc_row in doc_rows:
            comparison = _build_comparison(cursor, doc_row['document_id'])
            if comparison and _classify_exception(cursor, doc_row, comparison):
                exception_count += 1

        stats = {
            'approved':   action_counts.get('approved', 0),
            'sent_back':  action_counts.get('returned', 0),
            'pending':    pending,
            'exceptions': exception_count,
        }

        # ── Timeline: always the last 30 days, regardless of `period` ──
        thirty_days_ago = datetime.utcnow() - timedelta(days=29)
        thirty_days_ago = thirty_days_ago.replace(hour=0, minute=0, second=0, microsecond=0)

        cursor.execute(
            '''SELECT DATE(rr.reviewed_at) AS day, rr.action, COUNT(*) AS cnt
               FROM review_records rr
               JOIN users u ON rr.reviewed_by = u.user_id
               WHERE u.role = 'auditor' AND rr.action IN ('approved', 'returned')
                 AND rr.reviewed_at >= %s
               GROUP BY DATE(rr.reviewed_at), rr.action''',
            (thirty_days_ago,)
        )
        action_by_day = {}
        for row in cursor.fetchall():
            action_by_day.setdefault(row['day'], {})[row['action']] = row['cnt']

        cursor.execute(
            '''SELECT DATE(uploaded_at) AS day, COUNT(*) AS cnt
               FROM documents
               WHERE status IN ('under_review', 'resubmitted') AND uploaded_at >= %s
               GROUP BY DATE(uploaded_at)''',
            (thirty_days_ago,)
        )
        pending_by_day = {row['day']: row['cnt'] for row in cursor.fetchall()}

        conn.close()

        timeline = []
        for i in range(30):
            day = (thirty_days_ago + timedelta(days=i)).date()
            day_actions = action_by_day.get(day, {})
            timeline.append({
                'date':      day.isoformat(),
                'approved':  day_actions.get('approved', 0),
                'sent_back': day_actions.get('returned', 0),
                'pending':   pending_by_day.get(day, 0),
            })

        return jsonify({
            'period':   period,
            'stats':    stats,
            'timeline': timeline,
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# REPORT: AUDIT TRAIL (approve/send-back/need-review history)
# GET /auditor/report/audit-trail
# Auditor only
# ------------------------------------------------------------
ACTION_DB_TO_API = {'approved': 'approved', 'returned': 'sent_back', 'need_review': 'need_review'}
ACTION_API_TO_DB = {v: k for k, v in ACTION_DB_TO_API.items()}


def _audit_trail_query(action_filter, start_date, end_date):
    where = ["u.role = 'auditor'", "rr.action IN ('approved', 'returned', 'need_review')"]
    params = []

    if action_filter and action_filter != 'all':
        db_action = ACTION_API_TO_DB.get(action_filter)
        if db_action:
            where.append('rr.action = %s')
            params.append(db_action)

    if start_date:
        where.append('rr.reviewed_at >= %s')
        params.append(start_date)
    if end_date:
        where.append('rr.reviewed_at <= %s')
        params.append(end_date)

    where_clause = ' AND '.join(where)
    base = f'''FROM review_records rr
               JOIN users u ON rr.reviewed_by = u.user_id
               JOIN documents d ON rr.document_id = d.document_id
               LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
               WHERE {where_clause}'''
    return base, params


@auditor_bp.route('/report/audit-trail', methods=['GET'])
@jwt_required()
def get_audit_trail():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    action_filter = request.args.get('action', 'all')
    start_date    = request.args.get('start_date')
    end_date      = request.args.get('end_date')
    limit         = request.args.get('limit', 50, type=int)
    offset        = request.args.get('offset', 0, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        base, params = _audit_trail_query(action_filter, start_date, end_date)

        cursor.execute(f'SELECT COUNT(*) AS cnt {base}', params)
        total = cursor.fetchone()['cnt']

        cursor.execute(
            f'''SELECT rr.reviewed_at, u.full_name AS auditor_name, u.email AS auditor_email,
                       rr.action, ef.invoice_number, rr.document_id, rr.remarks
                {base}
                ORDER BY rr.reviewed_at DESC
                LIMIT %s OFFSET %s''',
            params + [limit, offset]
        )
        rows = cursor.fetchall()
        conn.close()

        entries = [{
            'timestamp':           row['reviewed_at'].isoformat() if row['reviewed_at'] else None,
            'auditor_name':        row['auditor_name'],
            'auditor_email':       row['auditor_email'],
            'action':              ACTION_DB_TO_API.get(row['action'], row['action']),
            'invoice_no':          row['invoice_number'],
            'invoice_document_id': row['document_id'],
            'remarks':             row['remarks'],
        } for row in rows]

        return jsonify({'total': total, 'entries': entries}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# REPORT: AUDIT TRAIL CSV EXPORT
# GET /auditor/report/audit-trail/export.csv
# Auditor only
# ------------------------------------------------------------
@auditor_bp.route('/report/audit-trail/export.csv', methods=['GET'])
@jwt_required()
def export_audit_trail_csv():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] != 'auditor':
        return jsonify({'error': 'Access denied. Auditor only.'}), 403

    action_filter = request.args.get('action', 'all')
    start_date    = request.args.get('start_date')
    end_date      = request.args.get('end_date')
    limit         = request.args.get('limit', 50, type=int)
    offset        = request.args.get('offset', 0, type=int)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        base, params = _audit_trail_query(action_filter, start_date, end_date)

        cursor.execute(
            f'''SELECT rr.reviewed_at, u.full_name AS auditor_name, u.email AS auditor_email,
                       rr.action, ef.invoice_number, rr.remarks
                {base}
                ORDER BY rr.reviewed_at DESC
                LIMIT %s OFFSET %s''',
            params + [limit, offset]
        )
        rows = cursor.fetchall()
        conn.close()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(['Timestamp', 'Auditor Name', 'Auditor Email', 'Action', 'Invoice No', 'Remarks'])
        for row in rows:
            writer.writerow([
                row['reviewed_at'].isoformat() if row['reviewed_at'] else '',
                row['auditor_name'] or '',
                row['auditor_email'] or '',
                ACTION_DB_TO_API.get(row['action'], row['action']),
                row['invoice_number'] or '',
                row['remarks'] or '',
            ])

        return Response(
            buffer.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=audit_trail.csv'}
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 500
