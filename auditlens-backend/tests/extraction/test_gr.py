"""Regression tests for GR extraction (helpers/ocr_helper.py::
extract_gr_fields()) — part of the v2 candidate-based extraction engine
(helpers/extraction_engine.py). Covers the real date-confusion bug
report: "From Doc Date" (the referenced PO's date) vs. the GR's own
"Document Date"/"Date".

Pure in-process tests: no OCR call, no Gemini call, no DB connection.
Matches this repo's existing dependency-free scripts/ convention (no
pytest).

Usage:
    python tests/extraction/test_gr.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.ocr_helper import extract_gr_fields

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_gr_with_from_doc_date():
    """Real GR layout: "From Doc Date" (the referenced PO's date) appears
    BEFORE the GR's own "Date" in document order — the engine must still
    select the GR's own date, not the first date found."""
    print('Case: GR with "From Doc Date" appearing before the GR\'s own Date')
    ocr_text = (
        "RECEIVING CO SDN BHD\n"
        "From Doc No.\n"
        "PO3006000\n"
        "From Doc Date: 17/12/2025\n"
        "Supplier: Coilcraft Inc\n"
        "Doc No PD6011823\n"
        "Date 04/03/2026\n"
    )
    fields = extract_gr_fields(ocr_text)
    check('gr_number == PD6011823', fields['gr_number'] == 'PD6011823', fields['gr_number'])
    check('receipt_date == 04/03/2026 (not 17/12/2025)', fields['receipt_date'] == '04/03/2026', fields['receipt_date'])


def run_case_gr_with_document_date_label():
    """A different supplier's layout using an explicit "Document Date"
    label instead of a bare "Date" — must score just as high."""
    print('Case: GR with explicit "Document Date" label')
    ocr_text = (
        "WAREHOUSE CO SDN BHD\n"
        "GR No: GRN-8842\n"
        "Supplier Ref Date: 01/01/2026\n"
        "Document Date: 20/02/2026\n"
    )
    fields = extract_gr_fields(ocr_text)
    check('gr_number == GRN-8842', fields['gr_number'] == 'GRN-8842', fields['gr_number'])
    check('receipt_date == 20/02/2026 (not the supplier ref date)', fields['receipt_date'] == '20/02/2026', fields['receipt_date'])


if __name__ == '__main__':
    run_case_gr_with_from_doc_date()
    run_case_gr_with_document_date_label()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
