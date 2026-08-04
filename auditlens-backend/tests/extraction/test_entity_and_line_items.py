"""Regression tests for the "AP Automation Intelligence Upgrade":
- Part 1: vendor entity normalization (helpers/entity_normalizer.py),
  wired into routes/auditor.py's _vendor_match_all() and routes/
  matching.py's compare_vendor_field().
- Part 2: line-item matching priority (part_number/item_code exact
  match first, then description), routes/auditor.py's
  _match_line_items().

_vendor_match_all()/_match_line_items() are pure functions (plain
dicts/lists in, plain dicts/bool out) — no DB connection needed, so
these run fully in-process like every other file in tests/extraction/.
Importing routes.auditor/routes.matching only instantiates their Flask
Blueprint objects at module level, which doesn't require an app context.

Usage:
    python tests/extraction/test_entity_and_line_items.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.entity_normalizer import is_same_company, normalize_company_name, calculate_entity_similarity
from routes.auditor import _vendor_match_all, _match_line_items
from routes.matching import compare_vendor_field

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_test1_vendor_ocr_typo_match():
    """Test 1 (spec): Invoice vendor "COLCRAFT SINGAPORE PTE LTD" vs PO
    vendor "Coilcraft Singapore PTE LTD" -> MATCH."""
    print('Test 1: vendor OCR-typo + suffix/spacing tolerance -> MATCH')
    result = is_same_company('COLCRAFT SINGAPORE PTE LTD', 'Coilcraft Singapore PTE LTD')
    check('match is True', result['match'] is True, result)
    check('similarity > 90', result['similarity'] > 90, result['similarity'])
    check('normalized_source == "colcraft singapore"', result['normalized_source'] == 'colcraft singapore', result)
    check('normalized_target == "coilcraft singapore"', result['normalized_target'] == 'coilcraft singapore', result)

    vendor_match = _vendor_match_all([
        ('Invoice vendor', 'COLCRAFT SINGAPORE PTE LTD'),
        ('PO vendor', 'Coilcraft Singapore PTE LTD'),
    ])
    check('_vendor_match_all == True', vendor_match is True, vendor_match)

    matched, score = compare_vendor_field('COLCRAFT SINGAPORE PTE LTD', 'Coilcraft Singapore PTE LTD')
    check('compare_vendor_field matched == True', matched is True, (matched, score))


def run_case_vendor_three_way_all_same_supplier():
    """All three documents (Invoice/PO/GR) naming the same supplier with
    different capitalization/OCR spelling/line-break noise -> MATCH."""
    print('Case: 3-way vendor match with OCR/formatting noise on all sides')
    vendor_match = _vendor_match_all([
        ('Invoice vendor', 'COLCRAFT\nSINGAPORE PTE LTD'),
        ('PO vendor', 'Coilcraft Singapore PTE LTD'),
        ('GR vendor', 'Coilcraft Singapore Pte. Ltd.'),
    ])
    check('3-way vendor match == True', vendor_match is True, vendor_match)


def run_case_vendor_genuinely_different_companies():
    """Sanity check: genuinely different companies must NOT match — the
    normalization/fuzzy tolerance must not be so loose it collapses
    distinct suppliers together."""
    print('Case: genuinely different companies -> DIFFERENT')
    result = is_same_company('COILCRAFT SINGAPORE PTE LTD', 'EMITS TECHNOLOGY SDN BHD')
    check('match is False', result['match'] is False, result)


def run_case_test2_three_way_part_number_match():
    """Test 2 (spec): 0603DC-12NXGRW, Qty 4000 on Invoice/PO/GR -> 3-way
    MATCH, with the invoice's combined "CHIP INDUCTORS 0603DC-12NXGRW"
    description still pairing against the PO/GR's bare code-only rows."""
    print('Test 2: part_number exact match drives 3-way line item pairing')
    invoice_items = [{'item_code': '0603DC-12NXGRW', 'description': 'CHIP INDUCTORS', 'quantity': 4000, 'unit_price': 0.5, 'amount': 2000.0}]
    po_items = [{'item_code': '0603DC-12NXGRW', 'description': 'CHIP INDUCTORS', 'quantity': 4000, 'unit_price': 0.5, 'amount': 2000.0}]
    gr_items = [{'item_code': '0603DC-12NXGRW', 'description': 'CHIP INDUCTORS', 'quantity': 4000, 'unit_price': 0.5, 'amount': 2000.0}]

    rows, hard_mismatch, soft_mismatch = _match_line_items(invoice_items, po_items, gr_items)
    check('exactly 1 matched row (not 3 separate unmatched rows)', len(rows) == 1, rows)
    if rows:
        row = rows[0]
        check('item_code == 0603DC-12NXGRW', row['item_code'] == '0603DC-12NXGRW', row)
        check('invoice_quantity == 4000', row['invoice_quantity'] == 4000, row)
        check('po_quantity == 4000', row['po_quantity'] == 4000, row)
        check('gr_quantity == 4000', row['gr_quantity'] == 4000, row)
        check('quantity_match == True (3-way MATCH)', row['quantity_match'] is True, row)
        check('not missing on any document', not (row['missing_on_invoice'] or row['missing_on_po'] or row['missing_on_gr']), row)
    check('no hard mismatch', hard_mismatch is False, hard_mismatch)


def run_case_test3_multiple_line_items():
    """Test 3 (spec): 5 line items, all extracted and matched correctly
    across Invoice/PO/GR."""
    print('Test 3: 5 line items, all extracted and matched correctly')
    codes = ['0603DC-12NXGRW', 'SLT-MOS-N60R', 'MTC-IND-4R7M', 'CAP-100UF-25V', 'RES-10K-0603']
    invoice_items = [
        {'item_code': c, 'description': f'PRODUCT {i}', 'quantity': (i + 1) * 100, 'unit_price': 1.0, 'amount': (i + 1) * 100.0}
        for i, c in enumerate(codes)
    ]
    po_items = [dict(it) for it in invoice_items]
    gr_items = [dict(it) for it in invoice_items]

    rows, hard_mismatch, soft_mismatch = _match_line_items(invoice_items, po_items, gr_items)
    check('all 5 items produced exactly 5 matched rows', len(rows) == 5, len(rows))
    check('no hard mismatch across all 5 items', hard_mismatch is False, hard_mismatch)
    for i, row in enumerate(rows):
        check(f'item {i} ({codes[i]}) matched on all 3 documents',
              row['quantity_match'] is True and not row['missing_on_po'] and not row['missing_on_gr'],
              row)


def run_case_line_item_price_comparison_numeric_tolerance():
    """Reported bug: a real transaction whose Invoice/PO unit_price and
    line amount genuinely match (3000 x 0.63 = 1890.00 on both sides,
    GR carries no price at all for this line) was showing "Amount/unit
    price differs" in the UI. unit_price_match/amount_match must be
    compared numerically (float tolerance), never as formatted strings
    — 0.6300 and 0.63000 are the same number, just stored with
    different precision. GR's missing unit_price/amount must never be
    treated as a mismatch (result stays None/"not compared", not
    False)."""
    print('Case: Invoice unit_price 0.6300 vs PO unit_price 0.63000 -> numerically equal, not a mismatch')
    invoice_items = [{'item_code': '7550011', 'description': 'ITEM 7550011', 'quantity': 3000, 'unit_price': 0.6300, 'amount': 1890.00}]
    po_items = [{'item_code': '7550011', 'description': 'ITEM 7550011', 'quantity': 3000, 'unit_price': 0.63000, 'amount': 1890.00}]
    gr_items = [{'item_code': '7550011', 'description': 'ITEM 7550011', 'quantity': 3000, 'unit_price': None, 'amount': None}]

    rows, hard_mismatch, soft_mismatch = _match_line_items(invoice_items, po_items, gr_items)
    check('exactly 1 matched row', len(rows) == 1, rows)
    if rows:
        row = rows[0]
        check('quantity_match == True', row['quantity_match'] is True, row)
        check('unit_price_match == True (0.6300 == 0.63000 numerically)', row['unit_price_match'] is True, row)
        check('amount_match == True (1890.00 == 1890.00)', row['amount_match'] is True, row)
        check('invoice_unit_price/po_unit_price exposed for display',
              row['invoice_unit_price'] == 0.63 and row['po_unit_price'] == 0.63, row)
        check('invoice_amount/po_amount exposed for display',
              row['invoice_amount'] == 1890.0 and row['po_amount'] == 1890.0, row)
        # GR carries no price/amount at all for this line — must never
        # be treated as a mismatch (None = "not compared"/N/A).
        check('gr_unit_price is None (GR carries no price)', row['gr_unit_price'] is None, row)
        check('gr_amount is None (GR carries no amount)', row['gr_amount'] is None, row)
    check('no hard mismatch (quantity matches on all 3, no missing item)', hard_mismatch is False, hard_mismatch)
    check('no soft mismatch (price and amount both genuinely match)', soft_mismatch is False, soft_mismatch)


def run_case_line_item_price_genuine_mismatch_still_detected():
    """Sanity check: fixing the false positive above must not make the
    comparison blind to a REAL discrepancy — unit price and line amount
    are independent signals, so a genuine difference in one must be
    flagged even when the other genuinely matches."""
    print('Case: a genuine unit price difference is still correctly flagged, independently of line amount')
    invoice_items = [{'item_code': 'X1', 'description': 'ITEM X1', 'quantity': 100, 'unit_price': 1.00, 'amount': 100.00}]
    po_items = [{'item_code': 'X1', 'description': 'ITEM X1', 'quantity': 100, 'unit_price': 1.50, 'amount': 100.00}]

    rows, hard_mismatch, soft_mismatch = _match_line_items(invoice_items, po_items, None)
    check('exactly 1 matched row', len(rows) == 1, rows)
    if rows:
        row = rows[0]
        check('unit_price_match == False (1.00 != 1.50, a real difference)', row['unit_price_match'] is False, row)
        check('amount_match == True (100.00 == 100.00, genuinely unaffected)', row['amount_match'] is True, row)
    check('soft mismatch raised (unit price genuinely differs)', soft_mismatch is True, soft_mismatch)
    check('no hard mismatch (quantity still matches)', hard_mismatch is False, hard_mismatch)


def run_case_nicepack_gr_amount_polluted_by_quantity():
    """Real production bug (NICEPACK, invoice NP2606-0613 / PO 3006490):
    Invoice and PO genuinely match (3000 x 0.63 = 1890.00 on both), but
    the UI showed "Line amount differs". Root cause found in stored
    document_line_items: GR's 'amount' column held 3000.0 (== GR's own
    quantity, an extraction artifact) while GR's 'unit_price' was
    correctly NULL. The old code only excluded a value via
    `is not None`, so GR's bogus 3000.0 got compared against Invoice/PO's
    1890.0 and produced a false mismatch. Fix: an amount is only trusted
    when the same document also has a unit_price."""
    print('Case: NICEPACK real data — GR amount polluted with its own quantity, unit_price NULL')
    invoice_items = [{'item_code': '7550011', 'description': 'PLAIN NYLON VACUUM BAG 205 X 250 MM', 'quantity': 3000.0, 'unit_price': 0.63, 'amount': 1890.0}]
    po_items = [{'item_code': '7550011', 'description': 'Plain Nylon Vacumm Bag 205 X 250mm', 'quantity': 3000.0, 'unit_price': 0.63, 'amount': 1890.0}]
    gr_items = [{'item_code': '7550-1', 'description': 'Plain Nylon Vacumm Bag 205 X 250mm', 'quantity': 3000.0, 'unit_price': None, 'amount': 3000.0}]

    rows, hard_mismatch, soft_mismatch = _match_line_items(invoice_items, po_items, gr_items)
    check('exactly 1 matched row (sole-line-item fallback pairs across item_code mismatch)', len(rows) == 1, rows)
    if rows:
        row = rows[0]
        check('quantity_match == True', row['quantity_match'] is True, row)
        check('unit_price_match == True (GR unit_price is None, excluded, Invoice/PO both 0.63)', row['unit_price_match'] is True, row)
        check('amount_match == True (GR bogus amount excluded, Invoice/PO both 1890.00)', row['amount_match'] is True, row)
        check('invoice_amount == 1890.0', row['invoice_amount'] == 1890.0, row)
        check('po_amount == 1890.0', row['po_amount'] == 1890.0, row)
        check('gr_amount == 3000.0 (still exposed raw for display, just not compared)', row['gr_amount'] == 3000.0, row)
    check('no soft mismatch (GR bogus amount no longer corrupts the result)', soft_mismatch is False, soft_mismatch)


if __name__ == '__main__':
    run_case_test1_vendor_ocr_typo_match()
    run_case_vendor_three_way_all_same_supplier()
    run_case_vendor_genuinely_different_companies()
    run_case_test2_three_way_part_number_match()
    run_case_test3_multiple_line_items()
    run_case_line_item_price_comparison_numeric_tolerance()
    run_case_line_item_price_genuine_mismatch_still_detected()
    run_case_nicepack_gr_amount_polluted_by_quantity()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
