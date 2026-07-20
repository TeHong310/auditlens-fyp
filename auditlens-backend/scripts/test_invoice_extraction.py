"""Regression test for invoice total_amount extraction priority
(helpers/ocr_helper.py::extract_fields()) — covers the real Coilcraft
invoice case where TOTAL (US$) was correctly recognized in its two-line
form but other common currency-tag orderings (amount-before-currency,
RM two-line) were not.

Pure in-process test: no OCR call, no Gemini call, no DB connection —
extract_fields() only takes a plain OCR text string. Matches this repo's
existing dependency-free scripts/ convention (no pytest).

Usage:
    python scripts/test_invoice_extraction.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    # happens downstream in routes/documents.py before validation, not
    # here (see the invoice_date validator-ordering fix from the prior
    # extraction-debugging task).
    check('invoice_date == "2 March 2026"', fields['invoice_date'] == '2 March 2026', fields['invoice_date'])
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == USD', fields['currency'] == 'USD', fields['currency'])


def run_case_amount_before_currency():
    """Bare 'amount USD' with no repeated 'total' label on that line —
    the format that was still failing after the two-line fix alone."""
    print('Case: bare amount-before-currency form (8,020.00 USD)')
    ocr_text = (
        "SOME VENDOR SDN BHD\n"
        "TOTAL\n"
        "8,020.00 USD\n"
    )
    fields = extract_fields(ocr_text)
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == USD', fields['currency'] == 'USD', fields['currency'])


def run_case_currency_then_amount_bare_word():
    """'TOTAL USD 8,020.00' / 'Total Amount: USD 8,020.00' — bare-word
    currency-then-amount form, no parentheses."""
    print('Case: bare-word currency-then-amount form (TOTAL USD 8,020.00)')
    fields = extract_fields("TOTAL USD 8,020.00\n")
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])

    fields2 = extract_fields("Total Amount: USD 8,020.00\n")
    check('total_amount == 8020.00 (Total Amount: label)', fields2['total_amount'] == 8020.00, fields2['total_amount'])


def run_case_rm_two_line():
    """TOTAL (RM) as a label-only line, value on the next line — same
    two-line pattern as TOTAL (US$), now also supported for RM/MYR."""
    print('Case: TOTAL (RM) two-line form')
    ocr_text = "TOTAL (RM)\n8,020.00\n"
    fields = extract_fields(ocr_text)
    check('total_amount == 8020.00', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == MYR', fields['currency'] == 'MYR', fields['currency'])


if __name__ == '__main__':
    run_case_coilcraft_invoice()
    run_case_amount_before_currency()
    run_case_currency_then_amount_bare_word()
    run_case_rm_two_line()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
