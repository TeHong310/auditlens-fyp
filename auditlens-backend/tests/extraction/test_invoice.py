"""Regression tests for invoice extraction (helpers/ocr_helper.py::
extract_fields()) — part of the v2 candidate-based extraction engine
(helpers/extraction_engine.py). Covers the real Coilcraft invoice case
plus the currency-tag orderings/two-line forms the engine scores.

Pure in-process tests: no OCR call, no Gemini call, no DB connection.
Matches this repo's existing dependency-free scripts/ convention (no
pytest).

Usage:
    python tests/extraction/test_invoice.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.ocr_helper import extract_fields

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_coilcraft_invoice():
    """Real Coilcraft invoice: IX107587, dated 2 March 2026, TOTAL (US$)
    printed as a label-only line with the amount on the next line."""
    print('Case: Coilcraft invoice — TOTAL (US$) two-line form')
    ocr_text = (
        "COILCRAFT SINGAPORE PTE LTD\n"
        "INVOICE\n"
        "INVOICE NO: IX107587\n"
        "INVOICE DATE: 2 March 2026\n"
        "TOTAL (US$)\n"
        "8,020.00\n"
    )
    fields = extract_fields(ocr_text)
    check('invoice_number == IX107587', fields['invoice_number'] == 'IX107587', fields['invoice_number'])
    # extract_fields() returns the raw OCR date string — ISO normalization
    # happens downstream in routes/documents.py before validation.
    check('invoice_date == "2 March 2026"', fields['invoice_date'] == '2 March 2026', fields['invoice_date'])
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == USD', fields['currency'] == 'USD', fields['currency'])
    # AP amount scoring: a bare "Total" (as in "TOTAL (US$)") scores at
    # the +30 "Total Amount" tier — below "Grand Total"/"Total Payable"
    # (+50) and "Amount Due"/"Invoice Total" (+40) — so not needs_review,
    # but not the top confidence tier either.
    check('total_amount not needing review',
          fields['_confidence']['total_amount']['needs_review'] is False,
          fields['_confidence']['total_amount'])


def run_case_different_currency_format():
    """A different supplier's layout: RM total, bare-word 'TOTAL AMOUNT'
    label, no parentheses — must not depend on the exact Coilcraft wording."""
    print('Case: different currency format (TOTAL AMOUNT: RM 32,946.16)')
    fields = extract_fields("TOTAL AMOUNT: RM 32,946.16\n")
    check('total_amount == 32946.16', fields['total_amount'] == 32946.16, fields['total_amount'])
    check('currency == MYR', fields['currency'] == 'MYR', fields['currency'])


def run_case_amount_before_currency():
    """Bare 'amount USD' with no repeated 'total' label on that line."""
    print('Case: amount-before-currency form (8,020.00 USD)')
    ocr_text = "SOME VENDOR SDN BHD\nTOTAL\n8,020.00 USD\n"
    fields = extract_fields(ocr_text)
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == USD', fields['currency'] == 'USD', fields['currency'])


def run_case_coilcraft_real_ocr_reverse_layout():
    """Real Coilcraft production OCR layout: Google Vision read the
    totals mini-table's VALUE column (Subtotal/GST/Total amounts) BEFORE
    its LABEL row, so "TOTAL (US$)" is the LAST line, with its own value
    several lines above it rather than below (reverse-proximity form)."""
    print('Case: Coilcraft invoice — real OCR reverse-layout totals table')
    ocr_text = (
        "COILCRAFT SINGAPORE PTE LTD\n"
        "INVOICE\n"
        "INVOICE NO: IX107587\n"
        "INVOICE DATE: 2 March 2026\n"
        "SUB-TOTAL:\n"
        "GST (0%)\n"
        "8,020.00\n"
        "0.00\n"
        "8,020.00\n"
        "TOTAL (US$)\n"
    )
    fields = extract_fields(ocr_text)
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == USD', fields['currency'] == 'USD', fields['currency'])


def run_case_negative_keyword_not_selected():
    """A GST line must never outrank a real TOTAL line, even though both
    are numerically present — this is the "don't blindly select the
    largest number" requirement."""
    print('Case: GST line must not be selected over the real total')
    ocr_text = "GST @ 6%: 9999.00\nTOTAL: 500.00\n"
    fields = extract_fields(ocr_text)
    check('total_amount == 500.00 (not the larger GST figure)', fields['total_amount'] == 500.00, fields['total_amount'])


if __name__ == '__main__':
    run_case_coilcraft_invoice()
    run_case_different_currency_format()
    run_case_amount_before_currency()
    run_case_coilcraft_real_ocr_reverse_layout()
    run_case_negative_keyword_not_selected()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
