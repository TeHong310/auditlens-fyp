import re
import csv
import io
from decimal import Decimal
from datetime import datetime, timedelta, date
from flask import Blueprint, jsonify, request, Response
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.ocr_helper import split_item_code_prefix
from helpers.entity_normalizer import is_same_company, log_entity_match_debug
from helpers.document_relationships import (
    get_related_documents, get_related_purchase_orders, get_related_goods_receipts, get_related_invoices,
)
from helpers.enterprise_matching import compute_po_fulfilment, compute_invoice_result
from config import Config

auditor_bp = Blueprint('auditor', __name__)


def _vendor_match_all(named_vendors):
    """named_vendors: [(label, value), ...] for whichever of Invoice/PO/
    GR are present. Pairwise-compares every present pair via
    is_same_company() (normalized + OCR-typo-tolerant fuzzy similarity —
    see helpers/entity_normalizer.py) instead of the old exact-string-
    after-light-normalization check, which reported the SAME supplier as
    DIFFERENT whenever spacing/suffix/OCR-spelling varied across
    documents. Returns True only if every present pair matches, False if
    any pair doesn't, None if fewer than 2 vendor names are present to
    compare at all."""
    present = [(label, v) for label, v in named_vendors if v]
    if len(present) < 2:
        return None
    all_match = True
    for i in range(len(present)):
        for j in range(i + 1, len(present)):
            label_a, val_a = present[i]
            label_b, val_b = present[j]
            result = is_same_company(val_a, val_b)
            log_entity_match_debug(label_a, val_a, label_b, val_b, result)
            if not result['match']:
                all_match = False
    return all_match


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
    non-alphanumerics stripped) or None — this IS the "part_number"
    priority-1 match key: Gemini's schema fills item_code and part_number
    with the same value, and routes/documents.py's _sanitize_line_items()
    merges either one into the single item_code column document_line_
    items actually has, so checking item_code here already covers both
    names. desc_key is the normalized description via _normalize_text,
    with any leading item-code-shaped token STRIPPED first
    (split_item_code_prefix) — this is what makes matching robust even
    when item_code wasn't split out on one side: "SLT-MOS-N60R MOSFET
    N-Ch 600V TO-220" and "MOSFET N-Ch 600V TO-220" both reduce to the
    same desc_key regardless of which document's extraction path already
    normalized it (persistence-time normalization in routes/documents.py's
    _sanitize_line_items() is the primary fix for NEW uploads; this is
    the defense-in-depth fallback that also fixes matching for records
    already in the DB from before that fix existed)."""
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

    # ── Vendor match: fuzzy-compare vendor_name across every present doc
    # (invoice always present; PO/GR only if uploaded) — see
    # _vendor_match_all()/helpers/entity_normalizer.py for why this is a
    # normalized + OCR-typo-tolerant similarity check, not exact-string
    # equality. ──
    named_vendors = [('Invoice vendor', invoice['vendor_name'])]
    if po:
        named_vendors.append(('PO vendor', po['vendor_name']))
    if gr:
        named_vendors.append(('GR vendor', gr['vendor_name']))
    vendor_match = _vendor_match_all(named_vendors)

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
# ENTERPRISE V3 PHASE 2 — many-to-many matching engine.
#
# _build_comparison() above is COMPLETELY UNCHANGED (legacy, one-to-one,
# always available as a fallback — see build_comparison() below). This
# section adds a SEPARATE engine, _build_comparison_v2(), that computes
# cumulative fulfilment across every PO/GR reachable via document_
# relationships (Phase 1), and a shared dispatcher, build_comparison(),
# that every consumer in this app should call instead of _build_
# comparison() directly (see the 4 call sites in this file updated
# below, plus routes/ai_assistant.py::_build_case_context).
# ------------------------------------------------------------

def _po_dict_for_fulfilment(po):
    return {'po_id': po.get('po_id'), 'po_number': po.get('po_number'),
            'quantity': po.get('quantity'), 'total_amount': po.get('total_amount')}


def _build_comparison_v2(cursor, invoice_document_id):
    """Enterprise V3 Phase 2 matching engine. Only ever invoked by
    build_comparison() below when V2 is enabled/shadowed AND this
    invoice has at least one explicit document_relationships row — an
    invoice with none is left entirely to _build_comparison() (legacy),
    which already handles that case correctly and cheaply, and Phase 1's
    get_related_*() functions would just re-derive the same legacy
    attachment anyway.

    Preserves every key _build_comparison() returns (invoice/po/gr/
    line_items/match_result — computed the same way as legacy, against
    the relationship-selected PRIMARY po/gr) so every existing consumer
    (_classify_exception, the Exception page, Report counts, the Record
    Detail Field Comparison table) keeps working unchanged even when V2
    is active. Adds the new Phase 2 fields additively: engine_version,
    relationship_mode, invoice_result, po_fulfilment, related_invoices/
    purchase_orders/goods_receipts, issues, warnings, evidence.

    Known limitation: document_line_items has no po_id/gr_id column — it
    is keyed by (invoice document_id, document_type), so line-item-level
    detail is only available when the primary po/gr happens to be
    co-located under THIS invoice's own document_id (the common case,
    including every legacy-fallback invoice). For a genuinely cross-
    invoice-shared PO/GR, line_items is left empty (line_items_match/
    line_items_price_match = None, i.e. "not computed" — never a false
    mismatch) rather than guessed.
    """
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
    invoice_raw = {'quantity': inv_row['quantity'], 'total_amount': inv_row['total_amount']}

    related_pos = get_related_purchase_orders('invoice', invoice_document_id)
    related_grs = get_related_goods_receipts('invoice', invoice_document_id)

    sibling_invoices, seen_invoice_ids = [], {invoice_document_id}
    for po in related_pos:
        for inv in get_related_invoices('po', po['po_id']):
            if inv['document_id'] not in seen_invoice_ids:
                sibling_invoices.append(inv)
                seen_invoice_ids.add(inv['document_id'])

    # ── PO-level cumulative fulfilment: every invoice/GR linked to each
    # related PO (not just this one), so the PO's totals reflect ALL its
    # invoices/GRs — the actual many-to-many calculation. ──
    po_fulfilment = []
    po_allocations_for_this_invoice = []
    for po in related_pos:
        po_id = po['po_id']
        invoice_allocations = []
        this_invoice_alloc = None
        for inv in get_related_invoices('po', po_id):
            rel = inv.get('relationship')
            if rel:
                matched_qty, matched_amt = rel.get('matched_quantity'), rel.get('matched_amount')
            else:
                # Legacy fallback: this PO has no explicit relationships
                # at all, so it's a pure one-to-one attachment — the
                # single invoice gets full credit, same as legacy.
                matched_qty, matched_amt = inv.get('quantity'), inv.get('total_amount')
            alloc = {'document_id': inv['document_id'], 'matched_quantity': matched_qty, 'matched_amount': matched_amt}
            invoice_allocations.append(alloc)
            if inv['document_id'] == invoice_document_id:
                this_invoice_alloc = alloc

        gr_quantities = [gr.get('quantity') for gr in get_related_goods_receipts('po', po_id) if gr.get('quantity') is not None]

        fulfilment = compute_po_fulfilment(_po_dict_for_fulfilment(po), invoice_allocations, gr_quantities)
        po_fulfilment.append(fulfilment)

        if this_invoice_alloc is not None:
            other_allocs = [a for a in invoice_allocations if a['document_id'] != invoice_document_id]
            ordered = po.get('quantity')
            po_amount = po.get('total_amount')
            remaining_before_qty = None
            remaining_before_amt = None
            if ordered is not None:
                other_qty = sum((Decimal(str(a['matched_quantity'])) for a in other_allocs if a['matched_quantity'] is not None), Decimal('0'))
                remaining_before_qty = Decimal(str(ordered)) - other_qty
            if po_amount is not None:
                other_amt = sum((Decimal(str(a['matched_amount'])) for a in other_allocs if a['matched_amount'] is not None), Decimal('0'))
                remaining_before_amt = Decimal(str(po_amount)) - other_amt

            vendor_check = is_same_company(invoice['vendor_name'], po.get('vendor_name'))
            po_allocations_for_this_invoice.append({
                'po_id': po_id, 'po_number': po.get('po_number'),
                'matched_quantity': this_invoice_alloc['matched_quantity'],
                'matched_amount': this_invoice_alloc['matched_amount'],
                'remaining_before_this_invoice_quantity': remaining_before_qty,
                'remaining_before_this_invoice_amount': remaining_before_amt,
                'vendor_match': vendor_check['match'] if invoice['vendor_name'] and po.get('vendor_name') else None,
            })

    # gr_count for the invoice-level PASS/REVIEW_REQUIRED verdict:
    # prefer a DIRECT invoice_gr link count when one exists; when it's
    # zero (which happens whenever a PO has multiple invoices AND
    # multiple GRs — goods_receipts has no invoice-specific field, so
    # the builder deliberately never guesses which receipt belongs to
    # which sibling invoice, see helpers/relationship_builder.py), fall
    # back to "is the PO(s) behind this invoice receiving evidence at
    # all" (po_fulfilment's own received_quantity_cumulative), which is
    # the best available signal without a per-invoice receipt record.
    gr_count = len(related_grs)
    if gr_count == 0:
        gr_count = sum(1 for pf in po_fulfilment if (pf['received_quantity_cumulative'] or 0) > 0)

    invoice_result = compute_invoice_result(invoice_raw, po_allocations_for_this_invoice, gr_count)

    # ── Legacy-shaped keys (invoice/po/gr/match_result/line_items):
    # computed against the PRIMARY (first related, or legacy-fallback)
    # po/gr, using the SAME comparison helpers as _build_comparison(),
    # so every existing consumer of these keys keeps working unchanged. ──
    primary_po_row = related_pos[0] if related_pos else None
    primary_gr_row = related_grs[0] if related_grs else None

    po = None
    if primary_po_row:
        po = {
            'po_id':            primary_po_row['po_id'],
            'filename':         primary_po_row.get('file_name'),
            'po_no':            primary_po_row.get('po_number'),
            'vendor_name':      primary_po_row.get('vendor_name'),
            'po_date':          primary_po_row['po_date'].isoformat() if primary_po_row.get('po_date') else None,
            'total_amount':     float(primary_po_row['total_amount']) if primary_po_row.get('total_amount') is not None else None,
            'currency':         _normalize_currency(primary_po_row.get('currency')),
            'item_description': primary_po_row.get('item_description'),
            'quantity':         float(primary_po_row['quantity']) if primary_po_row.get('quantity') is not None else None,
        }

    gr = None
    if primary_gr_row:
        gr = {
            'gr_id':            primary_gr_row['gr_id'],
            'filename':         primary_gr_row.get('file_name'),
            'gr_no':            primary_gr_row.get('gr_number'),
            'vendor_name':      primary_gr_row.get('vendor_name'),
            'receipt_date':     primary_gr_row['receipt_date'].isoformat() if primary_gr_row.get('receipt_date') else None,
            'po_reference':     primary_gr_row.get('po_reference'),
            'item_description': primary_gr_row.get('item_description'),
            'quantity':         float(primary_gr_row['quantity']) if primary_gr_row.get('quantity') is not None else None,
        }

    line_items = []
    line_items_match = None
    line_items_price_match = None
    po_co_located = po is not None and primary_po_row.get('document_id') == invoice_document_id
    gr_co_located = gr is not None and primary_gr_row.get('document_id') == invoice_document_id
    if po_co_located or gr_co_located or (po is None and gr is None):
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
        line_items, hard_mm, soft_mm = _match_line_items(
            line_item_rows_by_type['invoice'],
            line_item_rows_by_type['po'] if po_co_located else None,
            line_item_rows_by_type['gr'] if gr_co_located else None,
        )
        line_items_match = (not hard_mm) if line_items else None
        line_items_price_match = (not soft_mm) if line_items else None

    named_vendors = [('Invoice vendor', invoice['vendor_name'])]
    if po:
        named_vendors.append(('PO vendor', po['vendor_name']))
    if gr:
        named_vendors.append(('GR vendor', gr['vendor_name']))
    vendor_match = _vendor_match_all(named_vendors)

    if po and invoice['currency'] and po['currency'] and invoice['currency'] != po['currency']:
        amount_match = None
    else:
        amount_match = _amounts_equal(invoice['total_amount'], po['total_amount']) if po else None

    po_refs = [_normalize_ref(invoice['po_reference'])]
    if po:
        po_refs.append(_normalize_ref(po['po_no']))
    if gr:
        po_refs.append(_normalize_ref(gr['po_reference']))
    po_reference_match = _three_way_match(po_refs)

    items = [_normalize_text(invoice['item_description'])]
    if po:
        items.append(_normalize_text(po['item_description']))
    if gr:
        items.append(_normalize_text(gr['item_description']))
    item_match = _three_way_match(items)

    quantities = [invoice['quantity']]
    if po:
        quantities.append(po['quantity'])
    if gr:
        quantities.append(gr['quantity'])
    quantity_match = _quantities_match(quantities)

    date_order_valid = None
    if po and gr and po['po_date'] and gr['receipt_date'] and invoice['invoice_date']:
        date_order_valid = po['po_date'] <= gr['receipt_date'] <= invoice['invoice_date']
    elif po and invoice['invoice_date'] and po['po_date'] and not gr:
        date_order_valid = po['po_date'] <= invoice['invoice_date']
    elif gr and invoice['invoice_date'] and gr['receipt_date'] and not po:
        date_order_valid = gr['receipt_date'] <= invoice['invoice_date']

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

    evidence = [{
        'relationship_type':  r['relationship_type'],
        'other_type':         r['other_type'],
        'other_id':           r['other_id'],
        'confidence_score':   float(r['confidence_score']) if r['confidence_score'] is not None else None,
    } for r in get_related_documents('invoice', invoice_document_id)]

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
        },
        'engine_version':      'v2',
        'relationship_mode':   True,
        'invoice_result':      invoice_result,
        'po_fulfilment':       po_fulfilment,
        'related_invoices':    sibling_invoices,
        'related_purchase_orders': related_pos,
        'related_goods_receipts':  related_grs,
        'issues':   invoice_result['issues'],
        'warnings': invoice_result['warnings'],
        'evidence': evidence,
    }


def _shape_shadow_comparison(invoice_document_id, legacy_result, v2_result, has_relationships):
    """Enterprise V3 Phase 3 (STEP 2) — pure formatting only, no DB
    access: turns an already-computed legacy_result/v2_result pair into
    the structured side-by-side comparison shape. Factored out of
    build_shadow_comparison() below so build_comparison()'s shadow-mode
    branch can reuse it on results it already computed for its own
    purposes, instead of computing legacy/V2 a second time (STEP 6:
    "avoid repeated expensive queries")."""
    legacy_mr = legacy_result['match_result']
    legacy_status = legacy_mr['overall_status']
    legacy_summary = {
        'vendor_match':       legacy_mr['vendor_match'],
        'amount_match':       legacy_mr['amount_match'],
        'po_reference_match': legacy_mr['po_reference_match'],
        'line_items_match':   legacy_mr['line_items_match'],
        'po_present':         legacy_result['po'] is not None,
        'gr_present':         legacy_result['gr'] is not None,
    }

    enterprise_v2 = None
    differences = []
    if v2_result is not None:
        inv_result = v2_result['invoice_result']
        v2_status = inv_result['status']
        v2_summary = {
            'matched_po_count':   inv_result['matched_po_count'],
            'matched_gr_count':   inv_result['matched_gr_count'],
            'allocated_quantity': inv_result['allocated_quantity'],
            'allocated_amount':   inv_result['allocated_amount'],
            'related_invoice_count': len(v2_result['related_invoices']),
        }
        enterprise_v2 = {'status': v2_status, 'matching_summary': v2_summary}

        legacy_po_count = 1 if legacy_summary['po_present'] else 0
        if (legacy_status == 'PASS') != (v2_status == 'PASS') or legacy_status != v2_status:
            if v2_summary['related_invoice_count'] > 0:
                reason = 'Enterprise engine detected multiple related invoices'
            elif v2_summary['matched_po_count'] > legacy_po_count:
                reason = 'Enterprise engine detected multiple related purchase orders'
            elif legacy_status != 'PASS' and v2_status == 'PASS':
                reason = 'Enterprise engine found cumulative allocation evidence the legacy one-to-one engine could not see'
            else:
                reason = 'Legacy and Enterprise engines disagree on overall matching status'
            differences.append({
                'field':        'matching_status',
                'legacy_value': legacy_status,
                'v2_value':     v2_status,
                'reason':       reason,
            })

        if v2_summary['matched_po_count'] > legacy_po_count:
            differences.append({
                'field':        'matched_po_count',
                'legacy_value': legacy_po_count,
                'v2_value':     v2_summary['matched_po_count'],
                'reason':       'Enterprise engine detected additional related purchase orders',
            })

        if v2_summary['related_invoice_count'] > 0:
            differences.append({
                'field':        'related_invoices',
                'legacy_value': 0,
                'v2_value':     v2_summary['related_invoice_count'],
                'reason':       'Enterprise engine detected multiple related invoices sharing a purchase order',
            })

    return {
        'document_id':       invoice_document_id,
        'relationship_mode': has_relationships,
        'legacy':        {'status': legacy_status, 'matching_summary': legacy_summary},
        'enterprise_v2': enterprise_v2,
        'differences':   differences,
    }


def build_shadow_comparison(cursor, invoice_document_id):
    """Enterprise V3 Phase 3 (STEP 2) — a structured, side-by-side
    legacy-vs-V2 comparison for ONE invoice, computed independently of
    build_comparison()'s own dispatch flow. Used by the read-only debug
    endpoint (routes/document_relationships.py::get_matching_
    comparison) so an auditor can inspect a comparison on demand
    regardless of the current feature-flag state. Purely observational:
    computes both results, never writes anything, never calls
    _classify_exception, never touches any workflow/status table, never
    calls Claude/Gemini. Returns None if the invoice doesn't exist.

    V2 is only computed when this invoice has explicit document_
    relationships rows — otherwise V2 would just re-derive the
    identical legacy-shaped result via Phase 1's fallback helpers,
    which is not an interesting comparison; enterprise_v2 is None and
    differences is empty in that case.
    """
    legacy_result = _build_comparison(cursor, invoice_document_id)
    if legacy_result is None:
        return None

    has_relationships = bool(get_related_documents('invoice', invoice_document_id))
    v2_result = None
    if has_relationships:
        try:
            v2_result = _build_comparison_v2(cursor, invoice_document_id)
        except Exception as e:
            print(f"WARNING shadow comparison V2 computation failed for doc={invoice_document_id}: {type(e).__name__}: {e}")

    return _shape_shadow_comparison(invoice_document_id, legacy_result, v2_result, has_relationships)


def _log_shadow_comparison(comparison):
    """Enterprise V3 Phase 3 (STEP 3) — logs ONLY document_id, timestamp,
    legacy status, V2 status, and difference TYPES (field names, never
    the reason text or actual values, which is already non-sensitive
    but kept out of logs anyway as the stricter reading of "do not log
    ... full extracted fields"). No invoice content, no vendor name, no
    amounts, ever."""
    if comparison is None or comparison['enterprise_v2'] is None:
        return
    diff_types = [d['field'] for d in comparison['differences']]
    print(f"DEBUG Matching Shadow Comparison: document={comparison['document_id']} "
          f"timestamp={datetime.utcnow().isoformat()} "
          f"legacy={comparison['legacy']['status']} "
          f"enterprise={comparison['enterprise_v2']['status']} "
          f"differences={diff_types if diff_types else 'none'}")


def build_comparison(cursor, invoice_document_id):
    """Single shared entry point every matching-result consumer in this
    app should call instead of _build_comparison() directly — this IS
    the "smallest safe change" that lets Enterprise V3 Phase 2 reach
    every existing surface (Exception page, Report, Record Detail, AI
    Assistant, Workflow Timeline) without duplicating any of their own
    logic. _build_comparison() itself is never modified and remains the
    permanent fallback:
      - both flags off (the default): legacy only, zero V2 code runs.
      - ENTERPRISE_MATCHING_V2_SHADOW_MODE on: legacy result is what's
        returned (user-facing behavior never changes); V2 is ALSO
        computed (only when this invoice has explicit document_
        relationships rows) purely to log a structured, safe comparison
        (see _shape_shadow_comparison/_log_shadow_comparison below) —
        no AI calls, no duplicate exceptions, no logging of full
        document content.
      - ENTERPRISE_MATCHING_V2_ENABLED on: V2 is used when this invoice
        has explicit relationships; any exception during V2 computation
        is caught and logged, falling back to legacy rather than ever
        5xx-ing a page.
    """
    v2_on = Config.ENTERPRISE_MATCHING_V2_ENABLED
    shadow_on = Config.ENTERPRISE_MATCHING_V2_SHADOW_MODE

    if not v2_on and not shadow_on:
        return _build_comparison(cursor, invoice_document_id)

    has_relationships = bool(get_related_documents('invoice', invoice_document_id))
    if not has_relationships:
        return _build_comparison(cursor, invoice_document_id)

    legacy_result = _build_comparison(cursor, invoice_document_id) if shadow_on else None

    v2_result = None
    try:
        v2_result = _build_comparison_v2(cursor, invoice_document_id)
    except Exception as e:
        print(f"WARNING V2 matching failed for doc={invoice_document_id}, falling back to legacy: {type(e).__name__}: {e}")

    if shadow_on and legacy_result is not None:
        comparison = _shape_shadow_comparison(invoice_document_id, legacy_result, v2_result, has_relationships)
        _log_shadow_comparison(comparison)

    if v2_on and v2_result is not None:
        return v2_result

    return legacy_result if legacy_result is not None else _build_comparison(cursor, invoice_document_id)


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

        result = build_comparison(cursor, invoice_document_id)
        conn.close()

        if result is None:
            return jsonify({'error': 'Invoice document not found'}), 404

        return jsonify(result), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _matching_status_for_comparison(comparison):
    """Enterprise V3 Phase 4 (STEP 1/3/5/6) — the single normalized
    matching status ('PASS'|'REVIEW'|'FAIL'|'PARTIAL') for ONE
    comparison result, preferring the Enterprise V2 engine's own
    invoice-level verdict (invoice_result.status: 'PASS'|
    'REVIEW_REQUIRED') when V2 actually ran for this invoice, and
    falling back to the legacy match_result.overall_status otherwise.
    Every V2-aware consumer (Report counts, exception classification,
    Workflow Timeline, AI Assistant context via _build_case_context)
    reads from this ONE function, so "use the active matching
    dispatcher result, not raw legacy fields" only has to be true in a
    single place."""
    if comparison.get('engine_version') == 'v2' and comparison.get('invoice_result'):
        return 'PASS' if comparison['invoice_result']['status'] == 'PASS' else 'REVIEW'
    return comparison['match_result']['overall_status']


# ------------------------------------------------------------
# EXCEPTION DETECTION
# Classifies one invoice's comparison result + document row into at
# most one exception (highest severity wins), or None if clean.
# Severity order: mismatch > sent_back = missing_document > low_confidence
#
# Enterprise V3 Phase 4 (STEP 3): when `comparison` is V2-shaped
# (comparison['engine_version'] == 'v2'), the matching-related
# candidates (mismatch/review/missing_document) are derived from V2's
# own invoice_result.status/issues instead of the legacy match_result
# fields — this is the actual fix for "do not create false exceptions
# from legacy logic" (the PO3006231 example: legacy alone reports a
# hard Amount mismatch because it only ever compares an invoice
# against a PO's FULL total; V2 correctly sees the invoice as fully
# allocated and PASS, so no matching-related exception is created at
# all). sent_back and low_confidence are workflow/OCR-quality signals,
# not "matching interpretation", so they are computed identically for
# both engines, unchanged from before this phase.
# ------------------------------------------------------------
def _classify_exception(cursor, doc_row, comparison):
    candidates = []  # (rank, type, label, detail, severity)
    mr = comparison['match_result']
    is_v2 = comparison.get('engine_version') == 'v2'

    if is_v2:
        inv_result = comparison['invoice_result']
        if inv_result['status'] != 'PASS':
            reasons = list(inv_result['issues']) or ['Enterprise matching flagged this invoice for review']
            evidence_parts = []
            for pf in (comparison.get('po_fulfilment') or []):
                evidence_parts.append(
                    f"PO {pf.get('po_number') or pf.get('po_id')}: "
                    f"invoiced {pf.get('invoiced_quantity_cumulative')}/{pf.get('ordered_quantity')}, "
                    f"received {pf.get('received_quantity_cumulative')}/{pf.get('ordered_quantity')}, "
                    f"remaining {pf.get('remaining_to_invoice')}"
                )
            detail = '; '.join(reasons)
            if evidence_parts:
                detail += ' | Evidence: ' + '; '.join(evidence_parts)
            candidates.append((4, 'mismatch', 'Enterprise Matching Review Required', detail, 'high'))
        # PASS under V2 -> no matching-related exception candidate at
        # all (this is the fix: a missing-GR-only warning, or a PO that
        # is only partially fulfilled overall, must NOT force an
        # exception when this specific invoice is fully supported).

    elif mr['overall_status'] == 'FAIL':
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
    # open that specific record's detail page. Skipped entirely under
    # V2 — the block above already covers V2's own review verdict.
    if not is_v2 and mr['overall_status'] == 'REVIEW':
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

    # Skipped entirely under V2 — a missing PO is already covered by
    # the V2 branch above ("No reliable PO relationship found" forces
    # REVIEW_REQUIRED, rank 4, which always outranks this rank-3
    # candidate anyway), and a missing GR alone is a non-blocking
    # warning under V2 (matching this codebase's long-standing "missing
    # GR/PO is PARTIAL, not FAIL" philosophy) — flagging it here too
    # would be exactly the false exception STEP 3 exists to prevent.
    if not is_v2 and (not comparison['po'] or not comparison['gr']):
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
            comparison = build_comparison(cursor, doc_row['document_id'])
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
        # Audit Quality Overview (Report page) — three-way match PASS/
        # REVIEW counts, read from the SAME comparison already computed
        # per document for exception_count below (no extra query).
        # Enterprise V3 Phase 4 (STEP 5): uses the active matching
        # dispatcher's own normalized status (_matching_status_for_
        # comparison — V2's invoice_result.status when V2 ran for that
        # invoice, else legacy's match_result.overall_status), not a
        # raw legacy field directly, so these counts stay correct once
        # ENTERPRISE_MATCHING_V2_ENABLED is turned on. single_document_
        # match_count/multi_document_match_count (STEP 5's optional
        # "Enterprise Matching Coverage") come from the SAME comparison
        # dicts already in hand — relationship_mode is only ever True
        # when V2 actually ran with real document_relationships data,
        # never fabricated.
        match_pass_count = 0
        match_review_count = 0
        single_document_match_count = 0
        multi_document_match_count = 0
        for doc_row in doc_rows:
            comparison = build_comparison(cursor, doc_row['document_id'])
            if not comparison:
                continue
            overall_status = _matching_status_for_comparison(comparison)
            if overall_status == 'PASS':
                match_pass_count += 1
            elif overall_status == 'REVIEW':
                match_review_count += 1
            if comparison.get('relationship_mode'):
                multi_document_match_count += 1
            else:
                single_document_match_count += 1
            if _classify_exception(cursor, doc_row, comparison):
                exception_count += 1

        stats = {
            'approved':      action_counts.get('approved', 0),
            'sent_back':     action_counts.get('returned', 0),
            'pending':       pending,
            'exceptions':    exception_count,
            'match_pass':    match_pass_count,
            'match_review':  match_review_count,
            'enterprise_matching_coverage': {
                'single_document_matches': single_document_match_count,
                'multi_document_matches':  multi_document_match_count,
            },
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
                       rr.action, ef.invoice_number, ef.vendor_name, rr.document_id, rr.remarks
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
            'vendor_name':         row['vendor_name'],
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
