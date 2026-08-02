"""Regression tests for helpers/vision_evidence_boxes.py's v8 document-
type-aware party/stamp detection — supplier/buyer matching, per-type
stamp keyword sets, and the v7 box-validation rules. No real Google
Vision API call: requests.post is monkey-patched with a hand-built
DOCUMENT_TEXT_DETECTION response shaped like a real Coilcraft/EMITS
document (the same example the feature spec itself uses), so
match_supplier_name, find_address_near_name, find_buyer_block_by_label,
find_buyer_block_by_top_position, find_stamp_region, and
build_vision_evidence_boxes all run for real against realistic word
geometry.

Usage:
    python tests/extraction/test_vision_evidence_boxes.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import helpers.vision_evidence_boxes as veb

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


PAGE_W, PAGE_H = 1836, 2376


def word(text, x1, y1, x2, y2, block, para, conf=0.98):
    return {
        'boundingBox': {'vertices': [
            {'x': x1, 'y': y1}, {'x': x2, 'y': y1}, {'x': x2, 'y': y2}, {'x': x1, 'y': y2}
        ]},
        'symbols': [{'text': text}],
        'confidence': conf,
        '_block': block, '_para': para,
    }


def page_from_words(width, height, words_flat):
    """Groups a flat list of word() dicts (each carrying its own
    _block/_para) into the blocks/paragraphs/words nesting Vision's
    real response uses."""
    blocks = {}
    for w in words_flat:
        blocks.setdefault(w['_block'], {}).setdefault(w['_para'], []).append(w)
    block_list = []
    for block_index in sorted(blocks):
        paragraphs = blocks[block_index]
        block_list.append({'paragraphs': [
            {'words': [{'boundingBox': w['boundingBox'], 'symbols': w['symbols'], 'confidence': w['confidence']}
                       for w in paragraphs[p]]}
            for p in sorted(paragraphs)
        ]})
    return {'width': width, 'height': height, 'blocks': block_list}


def mock_post_for(words_flat):
    def _mock_post(*args, **kwargs):
        resp = MagicMock()
        resp.json.return_value = {'responses': [{'fullTextAnnotation': {
            'pages': [page_from_words(PAGE_W, PAGE_H, words_flat)]
        }}]}
        return resp
    return _mock_post


def run_ocr(words_flat):
    with patch('helpers.vision_evidence_boxes.requests.post', side_effect=mock_post_for(words_flat)):
        return veb.run_google_vision_ocr_with_boxes(b'fake-png-bytes')


def boxes_for(document_type, words_flat, extracted_vendor_name):
    with patch('helpers.vision_evidence_boxes.requests.post', side_effect=mock_post_for(words_flat)):
        return veb.build_vision_evidence_boxes(1, document_type, b'fake-png-bytes', extracted_vendor_name)


def by_type(boxes, box_type):
    return next((b for b in boxes if b['type'] == box_type), None)


# ══════════════════════════ INVOICE ══════════════════════════
# Coilcraft's own invoice: Coilcraft owns the top letterhead (supplier),
# EMITS is a separately labelled "Bill To:" block (buyer), plus a lone
# Buyer Received stamp lower on the page.

invoice_words = [
    # Block 0: supplier letterhead (top)
    word('Coilcraft', 100, 100, 260, 135, 0, 0),
    word('Singapore', 265, 100, 400, 135, 0, 0),
    word('Pte', 405, 100, 450, 135, 0, 0),
    word('Ltd', 455, 100, 500, 135, 0, 0),
    # Block 1: "Bill To:" label (para 0) + buyer name on the next line (para 1)
    word('Bill', 100, 300, 160, 328, 1, 0),
    word('To:', 165, 300, 210, 328, 1, 0),
    word('EMITS', 100, 335, 200, 363, 1, 1),
    word('Technology', 205, 335, 380, 363, 1, 1),
    word('Sdn.', 385, 335, 440, 363, 1, 1),
    word('Bhd.', 445, 335, 500, 363, 1, 1),
    # Block 2: page title containing "Invoice" (irrelevant noise word)
    word('INVOICE', 1400, 100, 1600, 135, 2, 0),
    # Block 3: lone Buyer Received stamp
    word('RECEIVED', 1300, 1900, 1550, 1950, 3, 0),
]


def test_invoice():
    boxes = boxes_for('invoice', invoice_words, 'Coilcraft Singapore Pte Ltd')
    types = {b['type'] for b in boxes}
    check('invoice: supplier_name found', 'supplier_name' in types, types)
    check('invoice: NO supplier_address ever built', 'supplier_address' not in types, types)
    check('invoice: buyer_name found via Bill-To label', 'buyer_name' in types, types)
    check('invoice: buyer_received_stamp found', 'buyer_received_stamp' in types, types)
    check('invoice: exactly 3 boxes, no duplicates', len(boxes) == 3, [b['type'] for b in boxes])

    supplier = by_type(boxes, 'supplier_name')
    buyer = by_type(boxes, 'buyer_name')
    check('invoice: supplier box does not overlap "EMITS" text',
          supplier and max(p['x'] for p in supplier['polygon']) < 550, supplier)
    check('invoice: buyer box is the EMITS block, not the supplier letterhead',
          buyer and min(p['y'] for p in buyer['polygon']) > 250, buyer)
    for b in boxes:
        check(f'invoice: {b["type"]} has coordinate_source google_vision', b['coordinate_source'] == 'google_vision')
        check(f'invoice: {b["type"]} polygon has 4 points', len(b['polygon']) == 4)


# ══════════════════════════ PURCHASE ORDER ══════════════════════════
# EMITS issues the PO (buyer owns the top letterhead), Coilcraft appears
# in a Vendor field further down WITH its address in the same block —
# no "Bill To" label anywhere, so buyer detection must fall back to
# top-of-page position, excluding whichever block the supplier matched in.

po_words = [
    # Block 0: buyer/issuer letterhead (top) — no label needed
    word('EMITS', 100, 100, 220, 135, 0, 0),
    word('Technology', 225, 100, 400, 135, 0, 0),
    word('Sdn.', 405, 100, 460, 135, 0, 0),
    word('Bhd.', 465, 100, 520, 135, 0, 0),
    # Block 1: Vendor field — a "Vendor:" label (para 0) BEFORE the
    # supplier name (para 1) + address (para 2), all in the same block —
    # the label must never bleed into the inferred address.
    word('Vendor:', 100, 370, 200, 398, 1, 0),
    word('Coilcraft', 100, 400, 260, 435, 1, 1),
    word('Singapore', 265, 400, 400, 435, 1, 1),
    word('Pte', 405, 400, 450, 435, 1, 1),
    word('Ltd', 455, 400, 500, 435, 1, 1),
    word('164', 100, 440, 140, 468, 1, 2),
    word('Ang', 145, 440, 190, 468, 1, 2),
    word('Mo', 195, 440, 230, 468, 1, 2),
    word('Kio', 235, 440, 280, 468, 1, 2),
    word('Avenue', 285, 440, 380, 468, 1, 2),
]


def test_po():
    boxes = boxes_for('po', po_words, 'Coilcraft Singapore Pte Ltd')
    types = {b['type'] for b in boxes}
    check('po: supplier_name found', 'supplier_name' in types, types)
    check('po: supplier_address found (same-block words after name)', 'supplier_address' in types, types)
    check('po: buyer_name found via top-of-page position', 'buyer_name' in types, types)
    check('po: NO stamp of any kind built for a PO', not any('stamp' in t for t in types), types)
    check('po: exactly 3 boxes, no duplicates', len(boxes) == 3, [b['type'] for b in boxes])

    buyer = by_type(boxes, 'buyer_name')
    check('po: buyer box is the TOP block (EMITS), not the Vendor field',
          buyer and max(p['y'] for p in buyer['polygon']) < 300, buyer)

    supplier = by_type(boxes, 'supplier_name')
    address = by_type(boxes, 'supplier_address')
    check('po: address box starts BELOW the supplier name (label excluded, no overlap)',
          supplier and address and min(p['y'] for p in address['polygon']) >= max(p['y'] for p in supplier['polygon']) - 5,
          (supplier, address))


def test_po_missing_vendor_name_still_finds_buyer():
    # Even when extraction found no vendor_name at all (no supplier
    # match possible), the buyer/issuer letterhead is still structurally
    # findable — it never depends on the supplier having matched first.
    boxes = boxes_for('po', po_words, None)
    types = {b['type'] for b in boxes}
    check('po (no vendor_name): buyer_name still found', 'buyer_name' in types, types)
    check('po (no vendor_name): no supplier boxes built', 'supplier_name' not in types and 'supplier_address' not in types, types)


# ══════════════════════════ GOODS RECEIVED NOTE ══════════════════════════
# EMITS receives the goods (buyer/receiver owns the top letterhead),
# Coilcraft is the supplier (matched + address inferred, same as PO),
# the document's own title contains the word "Received" (must NOT be
# mistaken for a stamp), and two DISTINCT buyer-side stamps are present:
# QC Passed and Key-In Store, spatially far apart.

gr_words = [
    # Block 0: receiver/buyer letterhead (top)
    word('EMITS', 100, 100, 220, 135, 0, 0),
    word('Technology', 225, 100, 400, 135, 0, 0),
    word('Sdn.', 405, 100, 460, 135, 0, 0),
    word('Bhd.', 465, 100, 520, 135, 0, 0),
    # Block 1: document title — contains "Received" but is NOT a stamp
    word('Goods', 100, 200, 220, 232, 1, 0),
    word('Received', 225, 200, 400, 232, 1, 0),
    word('Note', 405, 200, 480, 232, 1, 0),
    # Block 2: supplier field — "Supplier:" label (para 0) BEFORE the
    # name (para 1) + address (para 2), same block
    word('Supplier:', 100, 370, 220, 398, 2, 0),
    word('Coilcraft', 100, 400, 260, 435, 2, 1),
    word('Singapore', 265, 400, 400, 435, 2, 1),
    word('Pte', 405, 400, 450, 435, 2, 1),
    word('Ltd', 455, 400, 500, 435, 2, 1),
    word('164', 100, 440, 140, 468, 2, 2),
    word('Ang', 145, 440, 190, 468, 2, 2),
    word('Mo', 195, 440, 230, 468, 2, 2),
    word('Kio', 235, 440, 280, 468, 2, 2),
    # Block 3: QC Passed stamp (top-right area)
    word('QC1', 1300, 1600, 1400, 1645, 3, 0),
    word('PASSED', 1300, 1650, 1480, 1695, 3, 0),
    word('QC', 1300, 1700, 1360, 1745, 3, 0),
    word('DEPT.', 1365, 1700, 1460, 1745, 3, 0),
    # Block 4: Key-In Store stamp — spatially far from the QC stamp
    word('EMITS', 1300, 2000, 1420, 2045, 4, 0),
    word('KEY-IN', 1300, 2050, 1450, 2095, 4, 0),
    word('STORE', 1300, 2100, 1440, 2145, 4, 0),
]


def test_gr():
    boxes = boxes_for('gr', gr_words, 'Coilcraft Singapore Pte Ltd')
    types = {b['type'] for b in boxes}
    check('gr: supplier_name found', 'supplier_name' in types, types)
    check('gr: supplier_address found', 'supplier_address' in types, types)
    check('gr: buyer_name found via top-of-page position', 'buyer_name' in types, types)
    check('gr: qc_passed_stamp found (2+ clustered keywords)', 'qc_passed_stamp' in types, types)
    check('gr: key_in_store_stamp found (2+ clustered keywords)', 'key_in_store_stamp' in types, types)
    check('gr: exactly 5 boxes, no duplicates', len(boxes) == 5, [b['type'] for b in boxes])

    qc = by_type(boxes, 'qc_passed_stamp')
    keyin = by_type(boxes, 'key_in_store_stamp')
    check('gr: QC stamp and Key-In stamp are two SEPARATE regions (not merged)',
          qc and keyin and max(p['y'] for p in qc['polygon']) < min(p['y'] for p in keyin['polygon']) - 100,
          (qc, keyin))
    check('gr: "Received" in the document title never becomes a stamp box',
          not any('received' in b['type'] for b in boxes), types)

    supplier = by_type(boxes, 'supplier_name')
    address = by_type(boxes, 'supplier_address')
    check('gr: address box starts BELOW the supplier name ("Supplier:" label excluded, no overlap)',
          supplier and address and min(p['y'] for p in address['polygon']) >= max(p['y'] for p in supplier['polygon']) - 5,
          (supplier, address))


def test_gr_lone_generic_word_does_not_false_positive():
    # A single stray "store" or "qc" word with nothing else nearby must
    # NOT become a stamp — min_words=2 is the safety net for the
    # generic-sounding keywords in the GR sets.
    lone_words = [
        word('EMITS', 100, 100, 220, 135, 0, 0),
        word('Coilcraft', 100, 400, 260, 435, 1, 0),
        word('store', 900, 900, 950, 930, 2, 0),  # single stray word, far from anything
    ]
    boxes = boxes_for('gr', lone_words, 'Coilcraft')
    types = {b['type'] for b in boxes}
    check('gr: lone "store" word alone never becomes key_in_store_stamp', 'key_in_store_stamp' not in types, types)


# ══════════════════════════ Unit-level checks ══════════════════════════

def test_find_buyer_block_by_label_handles_label_on_own_line():
    words, _, _ = run_ocr(invoice_words)
    buyer_words, conf = veb.find_buyer_block_by_label(words)
    check('find_buyer_block_by_label: finds EMITS words', buyer_words is not None and len(buyer_words) == 4, buyer_words)
    check('find_buyer_block_by_label: confidence is the structural constant',
          conf == veb._COUNTERPARTY_CONFIDENCE, conf)


def test_find_buyer_block_by_top_position_excludes_supplier_block():
    words, _, _ = run_ocr(po_words)
    name_words, _, name_indices = veb.match_supplier_name('Coilcraft Singapore Pte Ltd', words)
    supplier_block = words[name_indices[0]]['block_index']
    buyer_words, conf = veb.find_buyer_block_by_top_position(words, supplier_block)
    check('find_buyer_block_by_top_position: finds EMITS (block 0), not the Vendor field',
          buyer_words is not None and all(w['block_index'] != supplier_block for w in buyer_words), buyer_words)


def test_find_address_near_name_excludes_preceding_label():
    # A "Vendor:"/"Supplier:" label line immediately BEFORE the matched
    # name, in the same block, must never be pulled into the address —
    # it's a field label, not part of the address, and including it
    # would make the address box's Y-range wrap around and overlap the
    # supplier-name box above it.
    label_words = [
        word('Supplier:', 100, 600, 200, 630, 2, 0),
        word('Coilcraft', 100, 648, 260, 678, 2, 1),
        word('Singapore', 265, 648, 400, 678, 2, 1),
        word('Pte', 405, 648, 450, 678, 2, 1),
        word('Ltd', 455, 648, 500, 678, 2, 1),
        word('164', 100, 696, 140, 720, 2, 2),
        word('Ang', 145, 696, 190, 720, 2, 2),
    ]
    words, _, _ = run_ocr(label_words)
    _, _, name_indices = veb.match_supplier_name('Coilcraft Singapore Pte Ltd', words)
    addr_words, addr_conf = veb.find_address_near_name(words, name_indices)
    addr_texts = [w['text'] for w in addr_words] if addr_words else []
    check('find_address_near_name: excludes the preceding "Supplier:" label', 'Supplier:' not in addr_texts, addr_texts)
    check('find_address_near_name: still includes the real address words', addr_texts == ['164', 'Ang'], addr_texts)


def test_find_stamp_region_min_words():
    words, _, _ = run_ocr(gr_words)
    qc_words, qc_conf = veb.find_stamp_region(words, veb._GR_QC_STAMP_KEYWORDS, min_words=2)
    check('find_stamp_region: QC cluster has >= 2 words', qc_words is not None and len(qc_words) >= 2, qc_words)
    single_keyword_words = [w for w in words if w['text'].lower() not in ('qc', 'qc1', 'passed', 'dept.')]
    result, conf = veb.find_stamp_region(single_keyword_words, {'nonexistent'}, min_words=1)
    check('find_stamp_region: no matching keyword at all -> (None, 0.0)', result is None and conf == 0.0)


if __name__ == '__main__':
    test_invoice()
    test_po()
    test_po_missing_vendor_name_still_finds_buyer()
    test_gr()
    test_gr_lone_generic_word_does_not_false_positive()
    test_find_buyer_block_by_label_handles_label_on_own_line()
    test_find_buyer_block_by_top_position_excludes_supplier_block()
    test_find_address_near_name_excludes_preceding_label()
    test_find_stamp_region_min_words()

    print('\n' + '=' * 60)
    print(f'{len(FAILURES)} FAILED' if FAILURES else 'ALL PASSED')
    for f in FAILURES:
        print('FAIL:', f)
    sys.exit(1 if FAILURES else 0)
