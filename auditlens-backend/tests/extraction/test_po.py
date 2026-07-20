"""Regression tests for PO extraction (helpers/ocr_helper.py::
extract_po_fields()) — part of the v2 candidate-based extraction engine
(helpers/extraction_engine.py). Covers two different supplier layouts
(Coilcraft, NEXAWAVE) plus a PO that never prints a "Total" label at all,
to confirm the engine degrades gracefully instead of returning None.

Pure in-process tests: no OCR call, no Gemini call, no DB connection.
Matches this repo's existing dependency-free scripts/ convention (no
pytest).

Usage:
    python tests/extraction/test_po.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.ocr_helper import extract_po_fields

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_coilcraft_po():
    """Real Coilcraft PO layout: "PO Ref No" (a different, buyer-side
    reference field) must be rejected in favor of the two-line
    "Document No." / "Total Payable Incl. Tax (RM)" fields."""
    print('Case: Coilcraft PO — two-line Document No. + Total Payable Incl. Tax (RM)')
    ocr_text = (
        "BUYER CO SDN BHD\n"
        "PO Ref No: 400-C008\n"
        "Document No.\n"
        "PO3006000\n"
        "Supplier: Coilcraft Inc\n"
        "Total Payable Incl. Tax (RM)\n"
        "8,020.00\n"
    )
    fields = extract_po_fields(ocr_text)
    check('po_number == PO3006000 (not "400-C008" or "Ref")', fields['po_number'] == 'PO3006000', fields['po_number'])
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == MYR', fields['currency'] == 'MYR', fields['currency'])


def run_case_nexawave_po():
    """A different supplier's layout entirely: "Purchase Order No" same-
    line, "Order Total (RM)" same-line — different label wording from
    Coilcraft, must not depend on Coilcraft-specific phrasing."""
    print('Case: NEXAWAVE PO — different label wording')
    ocr_text = (
        "NEXAWAVE ELECTRONICS SDN BHD\n"
        "Purchase Order No: NX-4471\n"
        "Order Date: 15/01/2026\n"
        "Order Total (RM): 12,500.50\n"
    )
    fields = extract_po_fields(ocr_text)
    check('po_number == NX-4471', fields['po_number'] == 'NX-4471', fields['po_number'])
    check('total_amount == 12500.50', fields['total_amount'] == 12500.50, fields['total_amount'])
    check('po_date == 15/01/2026', fields['po_date'] == '15/01/2026', fields['po_date'])


def run_case_po_without_total_label():
    """A PO that never prints a "Total"/"Grand Total"/"Amount Due" label
    at all — only a Subtotal line. Must still return a usable (lower-
    confidence) value instead of None."""
    print('Case: PO without a Total label — Subtotal-only fallback')
    ocr_text = (
        "SOME SUPPLIER SDN BHD\n"
        "PO No: PO-9981\n"
        "Subtotal: 6,912.00\n"
    )
    fields = extract_po_fields(ocr_text)
    check('total_amount == 6912.00 (subtotal fallback, not None)', fields['total_amount'] == 6912.00, fields['total_amount'])
    check('needs_review is True (medium-confidence subtotal fallback)',
          fields['_confidence']['total_amount']['needs_review'] is True,
          fields['_confidence']['total_amount'])


if __name__ == '__main__':
    run_case_coilcraft_po()
    run_case_nexawave_po()
    run_case_po_without_total_label()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
