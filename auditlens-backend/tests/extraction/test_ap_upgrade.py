"""Regression tests for the "AP Automation Extraction Accuracy Upgrade"
(vendor intelligence, PO reference priority, AP-aware amount scoring,
currency detection) — matches the exact Coilcraft invoice/PO/GR
expected values from the upgrade spec.

Pure in-process tests: no OCR call, no Gemini call, no DB connection.
Matches this repo's existing dependency-free scripts/ convention (no
pytest).

Usage:
    python tests/extraction/test_ap_upgrade.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helpers.ocr_helper import extract_fields, extract_po_fields, extract_gr_fields

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


def run_case_invoice():
    print('Case: Coilcraft invoice — vendor intelligence + AP amount scoring')
    ocr_text = (
        "COILCRAFT SINGAPORE PTE LTD\n"
        "INVOICE\n"
        "INVOICE NO: IX107587\n"
        "INVOICE DATE: 2 March 2026\n"
        "Bill To:\n"
        "EMITS TECHNOLOGY SDN BHD\n"
        "GST @ 6%: 481.20\n"
        "TOTAL (US$)\n"
        "8,020.00\n"
    )
    fields = extract_fields(ocr_text)
    check('vendor_name == COILCRAFT SINGAPORE PTE LTD (not the Bill To buyer)',
          fields['vendor_name'] == 'COILCRAFT SINGAPORE PTE LTD', fields['vendor_name'])
    check('total_amount == 8020.00 (not the GST figure)', fields['total_amount'] == 8020.00, fields['total_amount'])
    check('currency == USD', fields['currency'] == 'USD', fields['currency'])
    check('invoice_date == "2 March 2026"', fields['invoice_date'] == '2 March 2026', fields['invoice_date'])


def run_case_po():
    print('Case: Coilcraft PO — vendor intelligence + PO number/amount scoring')
    ocr_text = (
        "BUYER CO SDN BHD\n"
        "PO Ref No: 400-C008\n"
        "Document No.\n"
        "PO3006000\n"
        "Supplier: Coilcraft Singapore PTE LTD\n"
        "Total Payable Incl. Tax (RM)\n"
        "32,946.16\n"
    )
    fields = extract_po_fields(ocr_text)
    check('vendor_name == Coilcraft Singapore PTE LTD (not the buyer letterhead)',
          fields['vendor_name'] == 'Coilcraft Singapore PTE LTD', fields['vendor_name'])
    check('po_number == PO3006000', fields['po_number'] == 'PO3006000', fields['po_number'])
    check('total_amount == 32946.16', fields['total_amount'] == 32946.16, fields['total_amount'])
    check('currency == MYR', fields['currency'] == 'MYR', fields['currency'])


def run_case_gr():
    print('Case: Coilcraft GR — vendor intelligence + PO reference two-line fix')
    ocr_text = (
        "RECEIVING CO SDN BHD\n"
        "From Doc No.\n"
        "PO3006000\n"
        "From Doc Date: 17/12/2025\n"
        "Supplier: Coilcraft Singapore PTE LTD\n"
        "GR No: PD6011823\n"
        "Receipt Date: 04/03/2026\n"
    )
    fields = extract_gr_fields(ocr_text)
    check('vendor_name == Coilcraft Singapore PTE LTD (not the receiving letterhead)',
          fields['vendor_name'] == 'Coilcraft Singapore PTE LTD', fields['vendor_name'])
    check('gr_number == PD6011823', fields['gr_number'] == 'PD6011823', fields['gr_number'])
    check('receipt_date == 04/03/2026 (not From Doc Date)', fields['receipt_date'] == '04/03/2026', fields['receipt_date'])
    check('po_reference == PO3006000 (two-line "From Doc No." form)',
          fields['po_reference'] == 'PO3006000', fields['po_reference'])


def run_case_vendor_never_selects_buyer_labels():
    """Direct regression for the exact wrong/correct example from the
    spec: "Wrong: EMITS TECHNOLOGY SDN BHD / Correct: COILCRAFT SINGAPORE
    PTE LTD" — must hold even when the buyer's name is a more "complete"
    company-shaped match than the header."""
    print('Case: vendor scoring never selects a Bill To/Ship To/Customer company')
    for label in ('Bill To', 'Ship To', 'Customer', 'Deliver To'):
        ocr_text = f"COILCRAFT SINGAPORE PTE LTD\n{label}:\nEMITS TECHNOLOGY SDN BHD\n"
        fields = extract_fields(ocr_text)
        check(f'[{label}] vendor_name == COILCRAFT SINGAPORE PTE LTD',
              fields['vendor_name'] == 'COILCRAFT SINGAPORE PTE LTD', fields['vendor_name'])


def run_case_vendor_no_label_at_top_scores_high():
    """v5 regression: a plain header vendor with NO "Vendor:"/"Supplier:"
    label at all (the overwhelmingly common real-invoice case) must score
    high (>=85, not needs_review) purely from its top-of-document
    position — absence of a label must never be treated as low
    confidence."""
    print('Case: unlabeled header vendor scores high, not needs_review')
    ocr_text = "COILCRAFT SINGAPORE PTE LTD\nINVOICE\nINVOICE NO: IX107587\nTOTAL (US$)\n8,020.00\n"
    fields = extract_fields(ocr_text)
    check('vendor_name == COILCRAFT SINGAPORE PTE LTD', fields['vendor_name'] == 'COILCRAFT SINGAPORE PTE LTD', fields['vendor_name'])
    check('vendor confidence >= 85 (no label required for high confidence)',
          fields['_confidence']['vendor_name']['confidence'] >= 85, fields['_confidence']['vendor_name'])
    check('vendor not needs_review', fields['_confidence']['vendor_name']['needs_review'] is False,
          fields['_confidence']['vendor_name'])


def run_case_vendor_survives_early_false_positive_label():
    """v5 regression — the actual production bug: a "Sold To Reference"
    field (an unrelated reference code, NOT the real Bill To/customer
    section) appearing BEFORE the real vendor line used to corrupt the
    old relative "before the Bill To section" heuristic (invoice_to_index
    landed on this line, making the genuine header vendor score as if it
    were NOT at the top). The v5 scoring is based on ABSOLUTE
    top-of-document position instead, so this must no longer happen."""
    print('Case: early unrelated "Sold To" mention must not demote the real header vendor')
    ocr_text = (
        "Some Header Noise\n"
        "Sold To Reference: N/A\n"
        "Coilcraft Singapore Pte Ltd\n"
        "INVOICE\n"
        "INVOICE NO: IX107587\n"
        "TOTAL (US$)\n"
        "8,020.00\n"
    )
    fields = extract_fields(ocr_text)
    check('vendor_name == Coilcraft Singapore Pte Ltd', fields['vendor_name'] == 'Coilcraft Singapore Pte Ltd', fields['vendor_name'])
    check('vendor confidence == 100 (top-of-document, absolute position)',
          fields['_confidence']['vendor_name']['confidence'] == 100, fields['_confidence']['vendor_name'])


if __name__ == '__main__':
    run_case_invoice()
    run_case_po()
    run_case_gr()
    run_case_vendor_never_selects_buyer_labels()
    run_case_vendor_no_label_at_top_scores_high()
    run_case_vendor_survives_early_false_positive_label()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
