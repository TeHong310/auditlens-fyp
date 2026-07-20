"""Regression tests for the reusable currency scorer (helpers/
extraction_engine.py::detect_currency_candidates()/select_currency()) —
v2.1 fix for currency always defaulting to MYR. Covers USD/MYR/SGD/EUR
keyword detection, the "US$ contains SGD's S$" ambiguity, and the "no
currency marker anywhere -> None" requirement (never a hardcoded default).

Pure in-process tests: no OCR call, no Gemini call, no DB connection.
Matches this repo's existing dependency-free scripts/ convention (no
pytest).

Usage:
    python tests/extraction/test_currency.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.extraction_engine import detect_currency_candidates, select_currency
from helpers.ocr_helper import extract_fields, extract_po_fields, extract_gr_fields

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_scorer_usd():
    print('Case: currency scorer — USD keywords (USD, US$, U.S.$, Dollar)')
    for text in ('TOTAL (US$) 8,020.00', 'TOTAL USD 8,020.00', 'U.S.$ 8,020.00', '8,020.00 Dollar'):
        cur = select_currency(detect_currency_candidates(text))
        check(f'{text!r} -> USD', cur == 'USD', cur)


def run_case_scorer_myr():
    print('Case: currency scorer — MYR keywords (RM, MYR, Ringgit)')
    for text in ('Total Payable Incl. Tax (RM) 32,946.16', 'TOTAL MYR 32,946.16', '32,946.16 Ringgit'):
        cur = select_currency(detect_currency_candidates(text))
        check(f'{text!r} -> MYR', cur == 'MYR', cur)
    # "RM" must not false-positive inside an unrelated word.
    cur = select_currency(detect_currency_candidates('Payment Term: 500.00'))
    check('"Payment Term" does not false-positive as MYR', cur != 'MYR', cur)


def run_case_scorer_sgd_eur():
    print('Case: currency scorer — SGD (S$, SGD) and EUR (EUR, €)')
    cur = select_currency(detect_currency_candidates('TOTAL S$ 500.00'))
    check('S$ -> SGD', cur == 'SGD', cur)
    cur = select_currency(detect_currency_candidates('TOTAL SGD 500.00'))
    check('SGD -> SGD', cur == 'SGD', cur)
    cur = select_currency(detect_currency_candidates('TOTAL EUR 500.00'))
    check('EUR -> EUR', cur == 'EUR', cur)
    cur = select_currency(detect_currency_candidates('TOTAL €500.00'))
    check('€ -> EUR', cur == 'EUR', cur)


def run_case_scorer_us_dollar_not_confused_with_sgd():
    """"US$" textually CONTAINS SGD's "S$" keyword — the longer/more
    specific match must win, not the shorter substring."""
    print('Case: "US$" is not misdetected as SGD')
    cur = select_currency(detect_currency_candidates('TOTAL (US$) 8,020.00'))
    check('US$ -> USD (not SGD)', cur == 'USD', cur)


def run_case_scorer_no_currency_returns_none():
    print('Case: no currency keyword anywhere -> None (never a default)')
    cur = select_currency(detect_currency_candidates('TOTAL: 8,020.00'))
    check('no currency marker -> None', cur is None, cur)
    check('detect_currency_candidates returns [] when nothing found',
          detect_currency_candidates('TOTAL: 8,020.00') == [], detect_currency_candidates('TOTAL: 8,020.00'))


def run_case_invoice_currency_usd():
    print('Case: invoice extraction — currency == USD from TOTAL (US$)')
    ocr_text = "TOTAL (US$)\n8,020.00\n"
    fields = extract_fields(ocr_text)
    check('currency == USD', fields['currency'] == 'USD', fields['currency'])


def run_case_po_currency_myr():
    print('Case: PO extraction — currency == MYR from Total Payable Incl. Tax (RM)')
    ocr_text = "Total Payable Incl. Tax (RM)\n32,946.16\n"
    fields = extract_po_fields(ocr_text)
    check('currency == MYR', fields['currency'] == 'MYR', fields['currency'])


def run_case_po_currency_none_when_undetected():
    """A bare "Total: 8,020.00" with no currency keyword anywhere in the
    document must return currency=None, never a defaulted MYR."""
    print('Case: PO extraction — currency == None when no currency keyword present')
    ocr_text = "SOME SUPPLIER SDN BHD\nPO No: PO-9981\nTotal: 8,020.00\n"
    fields = extract_po_fields(ocr_text)
    check('total_amount == 8020.00 (still extracted)', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency is None (not defaulted to MYR)', fields['currency'] is None, fields['currency'])


def run_case_gr_currency_none_when_undetected():
    print('Case: GR extraction — currency == None when no currency keyword present')
    ocr_text = "RECEIVING CO SDN BHD\nGR No: GRN-1001\nReceipt Date: 04/03/2026\n"
    fields = extract_gr_fields(ocr_text)
    check('currency is None (GR carries no monetary total here)', fields['currency'] is None, fields['currency'])


if __name__ == '__main__':
    run_case_scorer_usd()
    run_case_scorer_myr()
    run_case_scorer_sgd_eur()
    run_case_scorer_us_dollar_not_confused_with_sgd()
    run_case_scorer_no_currency_returns_none()
    run_case_invoice_currency_usd()
    run_case_po_currency_myr()
    run_case_po_currency_none_when_undetected()
    run_case_gr_currency_none_when_undetected()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
