"""Regression tests for Enterprise V3 Phase 2's pure cumulative-matching
calculators (helpers/enterprise_matching.py::compute_po_fulfilment /
compute_invoice_result). No DB, no Flask, no AI — every function under
test here takes plain dicts and returns plain dicts, so these are pure
offline unit tests, same "no real DB" convention as the rest of this
suite.

Locks in the exact PO3006231 acceptance-scenario numbers from the task
spec (STEP 5) directly against the calculator, independent of any DB —
so these numbers can never regress even without a database available.

Usage:
    python tests/extraction/test_enterprise_matching.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.enterprise_matching import compute_po_fulfilment, compute_invoice_result

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


PO3006231 = {'po_id': 1, 'po_number': 'PO3006231', 'quantity': 30000, 'total_amount': 7710.00}


# ============================================================
# STEP 5 — the exact acceptance scenario, both states
# ============================================================

def run_case_acceptance_scenario_full_state():
    print('Case: PO3006231 acceptance scenario - FULL state (both invoices, both GRs)')
    invoice_allocations = [
        {'document_id': 1, 'matched_quantity': 15000, 'matched_amount': 3855.00},
        {'document_id': 2, 'matched_quantity': 15000, 'matched_amount': 3855.00},
    ]
    gr_quantities = [15000, 15000]

    fulfilment = compute_po_fulfilment(PO3006231, invoice_allocations, gr_quantities)

    check('invoiced quantity 30000/30000', fulfilment['invoiced_quantity_cumulative'] == 30000.0, fulfilment)
    check('received quantity 30000/30000', fulfilment['received_quantity_cumulative'] == 30000.0, fulfilment)
    check('cumulative invoiced amount RM 7710.00', fulfilment['invoiced_amount_cumulative'] == 7710.0, fulfilment)
    check('remaining quantity 0', fulfilment['remaining_to_invoice'] == 0.0, fulfilment)
    check('remaining amount RM 0.00', fulfilment['remaining_amount'] == 0.0, fulfilment)
    check('PO fulfilment status FULLY_FULFILLED', fulfilment['status'] == 'FULLY_FULFILLED', fulfilment)
    check('no unallocated invoices', fulfilment['unallocated_invoice_count'] == 0, fulfilment)

    for doc_id in (1, 2):
        other = [a for a in invoice_allocations if a['document_id'] != doc_id]
        remaining_before_qty = 30000 - sum(a['matched_quantity'] for a in other)
        remaining_before_amt = 7710.00 - sum(a['matched_amount'] for a in other)
        this_alloc = next(a for a in invoice_allocations if a['document_id'] == doc_id)
        result = compute_invoice_result(
            {'document_id': doc_id, 'quantity': this_alloc['matched_quantity'], 'total_amount': this_alloc['matched_amount']},
            [{'po_id': 1, 'po_number': 'PO3006231', 'matched_quantity': this_alloc['matched_quantity'],
              'matched_amount': this_alloc['matched_amount'],
              'remaining_before_this_invoice_quantity': remaining_before_qty,
              'remaining_before_this_invoice_amount': remaining_before_amt, 'vendor_match': True}],
            gr_count=1,
        )
        check(f'invoice {doc_id} individually PASSes (no false amount/partial mismatch)', result['status'] == 'PASS', result)
        check(f'invoice {doc_id} has no duplicate-style issue merely from equal vendor/amount', result['issues'] == [], result)


def run_case_acceptance_scenario_intermediate_state():
    print('Case: PO3006231 acceptance scenario - INTERMEDIATE state (only invoice A + GR A)')
    invoice_allocations = [{'document_id': 1, 'matched_quantity': 15000, 'matched_amount': 3855.00}]
    gr_quantities = [15000]

    fulfilment = compute_po_fulfilment(PO3006231, invoice_allocations, gr_quantities)

    check('remaining quantity 15000', fulfilment['remaining_to_invoice'] == 15000.0, fulfilment)
    check('remaining amount RM 3855.00', fulfilment['remaining_amount'] == 3855.0, fulfilment)
    check('PO status OPEN_PARTIALLY_INVOICED', fulfilment['status'] == 'OPEN_PARTIALLY_INVOICED', fulfilment)
    check('PO received_status OPEN_PARTIALLY_RECEIVED', fulfilment['received_status'] == 'OPEN_PARTIALLY_RECEIVED', fulfilment)

    result = compute_invoice_result(
        {'document_id': 1, 'quantity': 15000, 'total_amount': 3855.00},
        [{'po_id': 1, 'po_number': 'PO3006231', 'matched_quantity': 15000, 'matched_amount': 3855.00,
          'remaining_before_this_invoice_quantity': 30000, 'remaining_before_this_invoice_amount': 7710.00,
          'vendor_match': True}],
        gr_count=1,
    )
    check('invoice A PASSes when fully supported', result['status'] == 'PASS', result)


# ============================================================
# STEP 6 — required scenarios covered by the pure calculator
# ============================================================

def run_case_partial_invoice_against_larger_po():
    print('Case 6: partial invoice against a larger PO')
    fulfilment = compute_po_fulfilment({'po_id': 2, 'po_number': 'PO-X', 'quantity': 1000, 'total_amount': 1000.0},
                                        [{'document_id': 1, 'matched_quantity': 300, 'matched_amount': 300.0}], [])
    check('status OPEN_PARTIALLY_INVOICED', fulfilment['status'] == 'OPEN_PARTIALLY_INVOICED', fulfilment)
    check('remaining_to_invoice 700', fulfilment['remaining_to_invoice'] == 700.0, fulfilment)


def run_case_partial_gr():
    print('Case 7: partial GR')
    fulfilment = compute_po_fulfilment({'po_id': 3, 'po_number': 'PO-X', 'quantity': 1000, 'total_amount': 1000.0},
                                        [{'document_id': 1, 'matched_quantity': 1000, 'matched_amount': 1000.0}], [400])
    check('received_status OPEN_PARTIALLY_RECEIVED', fulfilment['received_status'] == 'OPEN_PARTIALLY_RECEIVED', fulfilment)
    check('remaining_to_receive 600', fulfilment['remaining_to_receive'] == 600.0, fulfilment)
    check('invoiced FULLY_INVOICED (not FULLY_FULFILLED, GR still partial)', fulfilment['status'] == 'FULLY_INVOICED', fulfilment)


def run_case_fully_fulfilled_po():
    print('Case 8: fully fulfilled PO (already covered by acceptance-scenario full state, kept for STEP 6 traceability)')
    fulfilment = compute_po_fulfilment({'po_id': 4, 'po_number': 'PO-X', 'quantity': 500, 'total_amount': 500.0},
                                        [{'document_id': 1, 'matched_quantity': 500, 'matched_amount': 500.0}], [500])
    check('status FULLY_FULFILLED', fulfilment['status'] == 'FULLY_FULFILLED', fulfilment)


def run_case_over_invoicing():
    print('Case 9: over-invoicing (invoiced exceeds ordered quantity)')
    fulfilment = compute_po_fulfilment({'po_id': 5, 'po_number': 'PO-X', 'quantity': 100, 'total_amount': 100.0},
                                        [{'document_id': 1, 'matched_quantity': 60, 'matched_amount': 60.0},
                                         {'document_id': 2, 'matched_quantity': 60, 'matched_amount': 60.0}], [])
    check('PO status OVER_INVOICED', fulfilment['status'] == 'OVER_INVOICED', fulfilment)

    result = compute_invoice_result(
        {'document_id': 2, 'quantity': 60, 'total_amount': 60.0},
        [{'po_id': 5, 'po_number': 'PO-X', 'matched_quantity': 60, 'matched_amount': 60.0,
          'remaining_before_this_invoice_quantity': 40, 'remaining_before_this_invoice_amount': 40.0,
          'vendor_match': True}],
        gr_count=0,
    )
    check('the overshooting invoice is REVIEW_REQUIRED', result['status'] == 'REVIEW_REQUIRED', result)
    check('issue names quantity exceeding remaining PO quantity',
          any('exceeds remaining quantity' in i for i in result['issues']), result)


def run_case_over_receipt():
    print('Case 10: over-receipt (received exceeds ordered quantity)')
    fulfilment = compute_po_fulfilment({'po_id': 6, 'po_number': 'PO-X', 'quantity': 100, 'total_amount': 100.0},
                                        [{'document_id': 1, 'matched_quantity': 100, 'matched_amount': 100.0}], [60, 60])
    check('PO status OVER_RECEIVED', fulfilment['status'] == 'OVER_RECEIVED', fulfilment)


def run_case_invoice_quantity_exceeds_remaining():
    print('Case 11: invoice quantity exceeds remaining PO quantity (per-invoice check)')
    result = compute_invoice_result(
        {'document_id': 1, 'quantity': 50, 'total_amount': 50.0},
        [{'po_id': 7, 'po_number': 'PO-X', 'matched_quantity': 50, 'matched_amount': 50.0,
          'remaining_before_this_invoice_quantity': 30, 'remaining_before_this_invoice_amount': 100.0,
          'vendor_match': True}],
        gr_count=0,
    )
    check('REVIEW_REQUIRED', result['status'] == 'REVIEW_REQUIRED', result)
    check('quantity-exceeds issue present', any('quantity exceeds' in i for i in result['issues']), result)


def run_case_invoice_amount_exceeds_remaining():
    print('Case 12: invoice amount exceeds remaining PO amount (per-invoice check)')
    result = compute_invoice_result(
        {'document_id': 1, 'quantity': 10, 'total_amount': 500.0},
        [{'po_id': 8, 'po_number': 'PO-X', 'matched_quantity': 10, 'matched_amount': 500.0,
          'remaining_before_this_invoice_quantity': 100, 'remaining_before_this_invoice_amount': 200.0,
          'vendor_match': True}],
        gr_count=0,
    )
    check('REVIEW_REQUIRED', result['status'] == 'REVIEW_REQUIRED', result)
    check('amount-exceeds issue present', any('amount exceeds' in i for i in result['issues']), result)


def run_case_vendor_mismatch():
    print('Case 13: vendor mismatch flags an issue')
    result = compute_invoice_result(
        {'document_id': 1, 'quantity': 10, 'total_amount': 100.0},
        [{'po_id': 9, 'po_number': 'PO-X', 'matched_quantity': 10, 'matched_amount': 100.0,
          'remaining_before_this_invoice_quantity': 100, 'remaining_before_this_invoice_amount': 1000.0,
          'vendor_match': False}],
        gr_count=0,
    )
    check('REVIEW_REQUIRED', result['status'] == 'REVIEW_REQUIRED', result)
    check('vendor mismatch issue present', any('Vendor mismatch' in i for i in result['issues']), result)


def run_case_no_reliable_po():
    print('Case: invoice with no PO relationship at all')
    result = compute_invoice_result({'document_id': 1, 'quantity': 10, 'total_amount': 100.0}, [], gr_count=0)
    check('REVIEW_REQUIRED', result['status'] == 'REVIEW_REQUIRED', result)
    check('"no reliable PO" issue present', 'No reliable PO relationship found' in result['issues'], result)


def run_case_multiple_pos_one_invoice_no_double_counting():
    print('Case 19: multiple POs allocated to one invoice - no double counting')
    result = compute_invoice_result(
        {'document_id': 1, 'quantity': 100, 'total_amount': 1000.0},
        [
            {'po_id': 10, 'po_number': 'PO-A', 'matched_quantity': 40, 'matched_amount': 400.0,
             'remaining_before_this_invoice_quantity': 100, 'remaining_before_this_invoice_amount': 1000.0, 'vendor_match': True},
            {'po_id': 11, 'po_number': 'PO-B', 'matched_quantity': 60, 'matched_amount': 600.0,
             'remaining_before_this_invoice_quantity': 100, 'remaining_before_this_invoice_amount': 1000.0, 'vendor_match': True},
        ],
        gr_count=1,
    )
    check('allocated_quantity sums exactly once per PO (100, not 200)', result['allocated_quantity'] == 100.0, result)
    check('allocated_amount sums exactly once per PO (1000, not 2000)', result['allocated_amount'] == 1000.0, result)
    check('PASS (fully allocated across both POs)', result['status'] == 'PASS', result)


def run_case_ambiguous_split_not_double_counted():
    print('Case: ambiguous multi-PO allocation (matched_quantity=None) is excluded from totals, not guessed')
    result = compute_invoice_result(
        {'document_id': 1, 'quantity': 100, 'total_amount': 1000.0},
        [
            {'po_id': 12, 'po_number': 'PO-A', 'matched_quantity': None, 'matched_amount': None,
             'remaining_before_this_invoice_quantity': None, 'remaining_before_this_invoice_amount': None, 'vendor_match': True},
            {'po_id': 13, 'po_number': 'PO-B', 'matched_quantity': None, 'matched_amount': None,
             'remaining_before_this_invoice_quantity': None, 'remaining_before_this_invoice_amount': None, 'vendor_match': True},
        ],
        gr_count=0,
    )
    check('allocated_quantity is 0 (never guessed)', result['allocated_quantity'] == 0.0, result)
    check('warning flags the ambiguous allocation', any('ambiguous' in w for w in result['warnings']), result)


def run_case_missing_gr_support_is_warning_not_blocking():
    print('Case: missing GR support is a warning, not a blocking issue (mirrors legacy PARTIAL, not FAIL)')
    result = compute_invoice_result(
        {'document_id': 1, 'quantity': 10, 'total_amount': 100.0},
        [{'po_id': 14, 'po_number': 'PO-X', 'matched_quantity': 10, 'matched_amount': 100.0,
          'remaining_before_this_invoice_quantity': 100, 'remaining_before_this_invoice_amount': 1000.0, 'vendor_match': True}],
        gr_count=0,
    )
    check('still PASSes despite no GR support', result['status'] == 'PASS', result)
    check('warning present for missing GR support', any('Goods Receipt' in w for w in result['warnings']), result)


def run_case_po_with_no_ordered_quantity_is_review_required():
    print('Case: PO with no ordered_quantity but real invoice activity -> REVIEW_REQUIRED (data quality issue)')
    fulfilment = compute_po_fulfilment({'po_id': 15, 'po_number': 'PO-X', 'quantity': None, 'total_amount': None},
                                        [{'document_id': 1, 'matched_quantity': 10, 'matched_amount': 100.0}], [])
    check('REVIEW_REQUIRED', fulfilment['status'] == 'REVIEW_REQUIRED', fulfilment)


if __name__ == '__main__':
    run_case_acceptance_scenario_full_state()
    run_case_acceptance_scenario_intermediate_state()

    run_case_partial_invoice_against_larger_po()
    run_case_partial_gr()
    run_case_fully_fulfilled_po()
    run_case_over_invoicing()
    run_case_over_receipt()
    run_case_invoice_quantity_exceeds_remaining()
    run_case_invoice_amount_exceeds_remaining()
    run_case_vendor_mismatch()
    run_case_no_reliable_po()
    run_case_multiple_pos_one_invoice_no_double_counting()
    run_case_ambiguous_split_not_double_counted()
    run_case_missing_gr_support_is_warning_not_blocking()
    run_case_po_with_no_ordered_quantity_is_review_required()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
