"""
v7 spec — accurate document evidence highlighting.
v8 spec — document-type-aware party/stamp detection.

Builds real, Google-Vision-derived polygon regions for the parties and
stamps each document type actually carries, by matching Google Vision
OCR word boxes against values this app already has (extracted vendor
name) or against real document structure (letterhead position, "Bill
To" style labels, stamp keywords) — never by estimating/guessing a
pixel position.

Deliberately separate from helpers/ocr_helper.py: that module's
run_google_vision_ocr() is the Finance-upload-time OCR call (feeds
extracted_fields.raw_ocr_text) and is left completely untouched — it
still discards box geometry, on purpose, since nothing in the upload
flow needs it. This module adds a SECOND Vision call (word geometry
kept, this time) made on-demand from run_authenticity_check(), against
the exact same canonical image bytes Claude/Gemini already analyzed
for the same check (helpers/authenticity_check.py passes through
prepare_gemini_image_payload()'s own output) — so OCR and the
authenticity preview are always looking at identical pixels.

DOCUMENT-TYPE RULES (v8) — same real-world counterparty (e.g. the
buyer/receiver company running this AuditLens instance), three
different roles, three different structural positions:
  - INVOICE: the SUPPLIER owns the page's own letterhead (matched via
    extracted_vendor_name); the BUYER is a separately labelled block
    ("Bill To" / "Sold To" / "Customer"). No supplier-address box is
    built for an invoice — an invoice's own letterhead already IS the
    supplier evidence, and address inference risks bleeding into the
    buyer's Bill-To block instead, so this category is skipped
    entirely rather than risk a wrong label.
  - PURCHASE ORDER: the BUYER issues the PO, so the buyer owns the
    page's own top letterhead; the SUPPLIER appears in a separately
    labelled Vendor/Supplier field, matched via extracted_vendor_name
    same as always. Supplier address IS built (structural inference,
    same-block-as-supplier-name).
  - GOODS RECEIVED NOTE: same shape as a PO — the RECEIVER (buyer) owns
    the top letterhead, the SUPPLIER is matched via extracted_vendor_name.
    Two distinct buyer-side processing stamps are looked for
    separately (QC Passed, Key-In Store) — never a supplier stamp, and
    never triggered by the word "Received" in the document's own title
    "Goods Received Note" (that word isn't in either GR stamp keyword
    set at all).
  - Only INVOICE looks for a stamp keyword set containing "received"
    (the Buyer Received Stamp) — this is why the same word never false-
    positives on a GR's title.

This mirrors the SAME buyer-vs-supplier reasoning already encoded in
helpers/claude_extractor.py's CLAUDE_AUTHENTICITY_PROMPT (see its
Coilcraft/EMITS example) — just applied here to produce a precise
on-image coordinate, not a text verdict.

Handwritten annotations are NOT implemented here: Google Vision's
DOCUMENT_TEXT_DETECTION has no handwriting-specific classification to
key off (it OCRs handwritten and printed text identically), so there
is no reliable coordinate signal to build a box from — including for a
Goods Received Note's handwritten processing notes. Per this feature's
own "do not manually estimate coordinates" rule, that means correctly
building nothing, not fabricating a placeholder.

Rotated stamp text: every bounding box here is the min/max envelope of
Vision's own 4 reported vertices (see _word_bbox/_merge_bbox) rather
than an assumed axis-aligned rectangle, so a rotated stamp word is
still enclosed correctly — no separate "rotation mode" is needed.
"""

import re
import base64
import difflib

import requests

from config import Config

# ── Google Vision call (boxes kept, unlike ocr_helper.run_google_vision_ocr) ──


def run_google_vision_ocr_with_boxes(image_bytes):
    """
    Same DOCUMENT_TEXT_DETECTION call as helpers/ocr_helper.py's
    run_google_vision_ocr(), but walks the full word-level geometry
    instead of discarding it after pulling out the plain text string.

    Returns (words, page_width, page_height):
      words: [{'text', 'vertices': [{'x','y'} x4], 'confidence',
               'block_index', 'paragraph_index'}, ...] in natural
        reading order — block_index/paragraph_index let callers group
        words that belong to the same visual block (used for the
        address/buyer structural heuristics below).
      page_width/page_height: Vision's OWN reported page pixel
        dimensions (fullTextAnnotation.pages[0].width/height) — the
        ground truth for what pixel space `vertices` are in; never
        assumed from anything else.

    Returns ([], None, None) on any failure or when Vision found no
    text — never raises (matches this codebase's established
    resilience style for AI/OCR helpers).
    """
    try:
        image_data = base64.b64encode(image_bytes).decode('utf-8')
        url = f"https://vision.googleapis.com/v1/images:annotate?key={Config.GOOGLE_VISION_API_KEY}"
        payload = {
            "requests": [{
                "image": {"content": image_data},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]
            }]
        }
        response = requests.post(url, json=payload, timeout=30)
        result = response.json()

        responses = result.get('responses') or []
        if not responses:
            return [], None, None
        annotation = responses[0].get('fullTextAnnotation')
        if not annotation:
            return [], None, None
        pages = annotation.get('pages') or []
        if not pages:
            return [], None, None

        page = pages[0]
        page_width = page.get('width')
        page_height = page.get('height')

        words = []
        for block_index, block in enumerate(page.get('blocks', [])):
            for paragraph_index, paragraph in enumerate(block.get('paragraphs', [])):
                for word in paragraph.get('words', []):
                    vertices = (word.get('boundingBox') or {}).get('vertices') or []
                    if len(vertices) != 4:
                        continue
                    text = ''.join(s.get('text', '') for s in word.get('symbols', []))
                    if not text.strip():
                        continue
                    words.append({
                        'text': text,
                        'vertices': [{'x': v.get('x', 0), 'y': v.get('y', 0)} for v in vertices],
                        'confidence': word.get('confidence', 0.0),
                        'block_index': block_index,
                        'paragraph_index': paragraph_index,
                    })
        return words, page_width, page_height
    except Exception as e:
        print(f"DEBUG Vision OCR (with boxes) error: {type(e).__name__}: {e}")
        return [], None, None


# ── Geometry helpers ──

def _normalize(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def _word_bbox(word):
    xs = [v['x'] for v in word['vertices']]
    ys = [v['y'] for v in word['vertices']]
    return min(xs), min(ys), max(xs), max(ys)


def _merge_bbox(words):
    boxes = [_word_bbox(w) for w in words]
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[2] for b in boxes)
    y2 = max(b[3] for b in boxes)
    return x1, y1, x2, y2


def _rect_polygon(x1, y1, x2, y2, pad=0):
    x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
    x2, y2 = x2 + pad, y2 + pad
    return [
        {'x': x1, 'y': y1},
        {'x': x2, 'y': y1},
        {'x': x2, 'y': y2},
        {'x': x1, 'y': y2},
    ]


# ── Supplier name / address matching ──

_NAME_MATCH_MIN_CONFIDENCE = 0.72


def match_supplier_name(target_text, words):
    """
    Slides a window across the OCR words (in reading order) looking for
    the contiguous run whose joined, normalized text best matches
    target_text (the vendor_name the extraction pipeline already
    found — the SAME column for all 3 document types), scored via
    difflib's SequenceMatcher ratio. This is real text matching against
    real OCR words — not a guessed position.

    Returns (matched_words, confidence, (start_index, end_index)) or
    (None, 0.0, None) if nothing scores above a disclosed confidence
    floor — a genuine "no reliable match", not a low-confidence guess.
    """
    target_norm = _normalize(target_text)
    if not target_norm or not words:
        return None, 0.0, None

    target_word_count = max(1, len((target_text or '').split()))
    max_window = min(len(words), target_word_count + 2)  # slack for OCR token splits

    best_words, best_ratio, best_indices = None, 0.0, None
    n = len(words)
    for start in range(n):
        candidate = ''
        for size in range(1, max_window + 1):
            end = start + size
            if end > n:
                break
            candidate += _normalize(words[end - 1]['text'])
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, target_norm, candidate).ratio()
            if ratio > best_ratio:
                best_words, best_ratio, best_indices = words[start:end], ratio, (start, end)

    if best_ratio < _NAME_MATCH_MIN_CONFIDENCE:
        return None, 0.0, None
    return best_words, best_ratio, best_indices


_ADDRESS_STRUCTURAL_CONFIDENCE = 0.65


def find_address_near_name(words, name_indices):
    """
    No extraction step in this app captures a supplier ADDRESS string
    anywhere (checked extracted_fields/purchase_orders/goods_receipts —
    only vendor_name exists), so there is no ground-truth text to fuzzy-
    match against the way match_supplier_name() does. Instead, this
    uses Vision's OWN structural grouping — real, Vision-reported data,
    not a guess: a PO/GR's supplier field is very commonly laid out as
    [(optional "Vendor:"/"Supplier:" label), Supplier Name line, Address
    line(s)] inside one visual block, so the remaining words in the SAME
    block AFTER the matched supplier name are taken as the address
    candidate — deliberately never words BEFORE it, which would instead
    be a preceding field label, not part of the address. Only called
    for PO/GR — see build_vision_evidence_boxes()'s per-document-type
    rules.

    Returns (words, confidence) or (None, 0.0) if the matched name's
    block has no words after it (nothing to call an address) —
    confidence is capped below a direct text match's, to honestly
    reflect that this is a structural inference, not a verified string
    match.
    """
    if not name_indices:
        return None, 0.0
    start, end = name_indices
    name_block = words[start]['block_index']
    following_words = [
        w for i, w in enumerate(words)
        if i >= end and w['block_index'] == name_block
    ]
    if len(following_words) >= 2:
        return following_words, _ADDRESS_STRUCTURAL_CONFIDENCE
    return None, 0.0


# ── Buyer / issuer / receiver ("counterparty") detection ──
#
# No extraction step captures this party's name anywhere either (only
# vendor_name/supplier exists) so — mirroring find_address_near_name's
# honesty principle — this is real Vision structural inference, not a
# guessed text match, and is capped at the same structural confidence.
# The structural rule mirrors the buyer-identification reasoning
# already encoded in helpers/claude_extractor.py's
# CLAUDE_AUTHENTICITY_PROMPT: on an Invoice the supplier owns the top
# letterhead and the buyer is a separately labelled block; on a PO/GR
# the buyer is normally the document's OWN top letterhead (the PO/GR is
# issued/stamped by the buyer) and the supplier appears in a separately
# labelled Vendor/Supplier field instead.

_COUNTERPARTY_CONFIDENCE = 0.6
_BUYER_LABEL_PHRASES = {'billto', 'soldto', 'invoiceto', 'billedto', 'customer'}


def _label_anchor_end(words, phrases, max_phrase_words=2):
    """Scans for a 1-2 word run whose normalized, concatenated text
    exactly matches one of `phrases` (a structural label, not a fuzzy
    name) — returns the index just after the matched label, or None."""
    n = len(words)
    for start in range(n):
        for size in range(1, max_phrase_words + 1):
            end = start + size
            if end > n:
                break
            candidate = ''.join(_normalize(w['text']) for w in words[start:end])
            if candidate in phrases:
                return end
    return None


def find_buyer_block_by_label(words):
    """Invoice-style buyer detection: the buyer is whatever remaining
    words share the SAME visual block as a 'Bill To' / 'Sold To' /
    'Customer' style label — covers both "Bill To: EMITS..." on one
    line and a label followed by the name on its own line, since a
    real Bill-To block is normally one distinct text element either
    way. Returns (words, confidence) or (None, 0.0) if no such label is
    found or nothing follows it in that block."""
    anchor_end = _label_anchor_end(words, _BUYER_LABEL_PHRASES)
    if anchor_end is None or anchor_end >= len(words):
        return None, 0.0
    label_block = words[anchor_end - 1]['block_index']
    following = [w for w in words[anchor_end:] if w['block_index'] == label_block]
    if not following:
        return None, 0.0
    return following, _COUNTERPARTY_CONFIDENCE


def find_buyer_block_by_top_position(words, exclude_block_index):
    """PO/GR-style buyer detection: the buyer is normally the document's
    OWN top letterhead — the topmost block on the page, excluding
    whichever block the supplier was matched in — since a PO/GR is
    issued/stamped by the buyer, not the supplier. Only the block's
    FIRST paragraph is taken (the header/company-name line), not every
    following address/contact line, so the box stays tight to the
    party name. Returns (words, confidence) or (None, 0.0)."""
    block_words = {}
    block_min_y = {}
    for w in words:
        bi = w['block_index']
        if bi == exclude_block_index:
            continue
        block_words.setdefault(bi, []).append(w)
        _, y1, _, _ = _word_bbox(w)
        block_min_y[bi] = min(block_min_y.get(bi, y1), y1)
    if not block_words:
        return None, 0.0
    top_block_index = min(block_min_y, key=lambda bi: block_min_y[bi])
    top_words = block_words[top_block_index]  # already in natural reading order
    first_paragraph = top_words[0]['paragraph_index']
    name_words = [w for w in top_words if w['paragraph_index'] == first_paragraph]
    if not name_words:
        return None, 0.0
    return name_words, _COUNTERPARTY_CONFIDENCE


# ── Stamp text matching ──
#
# Keyword sets are deliberately per-document-type and never share the
# word "received" outside of the invoice set — this is what stops a
# Goods Received Note's own title ("Goods RECEIVED Note") from ever
# being mistaken for a stamp: 'received' simply never appears in either
# GR keyword set. Generic single-character-risk words ("in") are
# excluded entirely; the remaining generic-sounding words ("store",
# "dept") are only trusted when at least 2 keyword words cluster
# together (min_words), which a single incidental page word can't do.

_INVOICE_STAMP_KEYWORDS = {'received', 'stamp'}
_GR_QC_STAMP_KEYWORDS = {'qc', 'qc1', 'passed', 'pass', 'dept'}
_GR_KEYIN_STAMP_KEYWORDS = {'keyin', 'key', 'store'}


def find_stamp_region(words, keywords, min_words=1):
    """
    Finds OCR words matching a recognised stamp keyword set, then
    clusters them by physical proximity (scaled to the matched words'
    own height, so the threshold isn't tied to any one render
    resolution, and unaffected by stamp rotation since clustering only
    uses each word's own center point) — words belonging to the same
    physical stamp are tightly packed; an unrelated keyword occurrence
    elsewhere on the page forms its own cluster and is not merged in.
    Returns the largest cluster's words and a confidence that grows
    with how many keyword words agree, or (None, 0.0) if no cluster
    reaches at least `min_words` matched words.
    """
    matches = [w for w in words if _normalize(w['text']) in keywords]
    if not matches:
        return None, 0.0

    def centroid(w):
        x1, y1, x2, y2 = _word_bbox(w)
        return (x1 + x2) / 2, (y1 + y2) / 2, (y2 - y1)

    avg_h = sum(centroid(w)[2] for w in matches) / len(matches)
    threshold = max(avg_h * 4, 40)

    clusters = []
    for w in matches:
        cx, cy, _ = centroid(w)
        placed = False
        for cluster in clusters:
            ccx, ccy = cluster['center']
            if abs(cx - ccx) <= threshold and abs(cy - ccy) <= threshold:
                cluster['words'].append(w)
                xs = [centroid(x)[0] for x in cluster['words']]
                ys = [centroid(x)[1] for x in cluster['words']]
                cluster['center'] = (sum(xs) / len(xs), sum(ys) / len(ys))
                placed = True
                break
        if not placed:
            clusters.append({'center': (cx, cy), 'words': [w]})

    best = max(clusters, key=lambda c: len(c['words']))
    if len(best['words']) < min_words:
        return None, 0.0
    confidence = min(0.95, 0.6 + 0.1 * len(best['words']))
    return best['words'], confidence


# ── Validation + entry construction ──

_MIN_BOX_CONFIDENCE = 0.55


def _build_box_entry(box_type, label, page, source_width, source_height, polygon, confidence):
    """Applies the v7 validation rules — valid dimensions, acceptable
    confidence, polygon clamped inside the source image — before ever
    returning an entry a caller could render. Returns None (never a
    partially-valid entry) if the dimensions or confidence don't clear
    the bar."""
    if not source_width or not source_height or source_width <= 0 or source_height <= 0:
        return None
    if confidence < _MIN_BOX_CONFIDENCE:
        return None
    clamped = []
    for pt in polygon:
        clamped.append({
            'x': max(0, min(source_width, pt['x'])),
            'y': max(0, min(source_height, pt['y'])),
        })
    return {
        'id': f'{box_type}-1',
        'type': box_type,
        'label': label,
        'page': page,
        'source_width': source_width,
        'source_height': source_height,
        'polygon': clamped,
        'confidence': round(confidence, 2),
        'coordinate_source': 'google_vision',
    }


def build_vision_evidence_boxes(document_id, document_type, image_bytes, extracted_vendor_name):
    """
    Main entry point, called from run_authenticity_check(). Runs Vision
    OCR (with box geometry) against the SAME canonical image bytes
    Claude/Gemini already analyzed, and builds accurate polygon
    evidence regions for the parties and stamps THIS document_type
    actually carries (see module docstring's DOCUMENT-TYPE RULES):

      invoice: supplier_name, buyer_name, buyer_received_stamp
      po:      supplier_name, supplier_address, buyer_name
      gr:      supplier_name, supplier_address, buyer_name,
               qc_passed_stamp, key_in_store_stamp

    Returns a list of v7-schema dicts (see _build_box_entry), each
    already validated and safe to render as-is. Never contains two
    entries of the same `type` (each is built at most once), so a
    caller can never end up with a duplicate box. Returns [] on any
    failure, when Vision found no usable text, or when nothing matched
    confidently enough — never raises.
    """
    try:
        words, page_width, page_height = run_google_vision_ocr_with_boxes(image_bytes)
        if not words or not page_width or not page_height:
            return []

        results = []
        name_indices = None

        if extracted_vendor_name:
            name_words, name_conf, name_indices = match_supplier_name(extracted_vendor_name, words)
            if name_words:
                x1, y1, x2, y2 = _merge_bbox(name_words)
                entry = _build_box_entry(
                    'supplier_name', 'Supplier Name', 1, page_width, page_height,
                    _rect_polygon(x1, y1, x2, y2, pad=4), name_conf,
                )
                if entry:
                    results.append(entry)

                # Supplier address is only meaningful for PO/GR — an
                # invoice's own letterhead already IS the supplier
                # evidence and doesn't need a separate address box; per
                # the task spec an invoice must never surface one.
                if document_type in ('po', 'gr'):
                    addr_words, addr_conf = find_address_near_name(words, name_indices)
                    if addr_words:
                        ax1, ay1, ax2, ay2 = _merge_bbox(addr_words)
                        addr_entry = _build_box_entry(
                            'supplier_address', 'Supplier Address', 1, page_width, page_height,
                            _rect_polygon(ax1, ay1, ax2, ay2, pad=4), addr_conf,
                        )
                        if addr_entry:
                            results.append(addr_entry)

        # ── Buyer / issuer / receiver — structural, never a name match. ──
        supplier_block = words[name_indices[0]]['block_index'] if name_indices else None
        if document_type == 'invoice':
            buyer_words, buyer_conf = find_buyer_block_by_label(words)
            buyer_label = 'Buyer'
        else:  # po, gr
            buyer_words, buyer_conf = find_buyer_block_by_top_position(words, supplier_block)
            buyer_label = 'PO Issuer / Buyer' if document_type == 'po' else 'Receiver'
        if buyer_words:
            bx1, by1, bx2, by2 = _merge_bbox(buyer_words)
            buyer_entry = _build_box_entry(
                'buyer_name', buyer_label, 1, page_width, page_height,
                _rect_polygon(bx1, by1, bx2, by2, pad=4), buyer_conf,
            )
            if buyer_entry:
                results.append(buyer_entry)

        # ── Stamps — type-specific keyword sets only; PO carries none. ──
        if document_type == 'invoice':
            stamp_words, stamp_conf = find_stamp_region(words, _INVOICE_STAMP_KEYWORDS)
            if stamp_words:
                sx1, sy1, sx2, sy2 = _merge_bbox(stamp_words)
                entry = _build_box_entry(
                    'buyer_received_stamp', 'Buyer Received Stamp', 1, page_width, page_height,
                    _rect_polygon(sx1, sy1, sx2, sy2, pad=10), stamp_conf,
                )
                if entry:
                    results.append(entry)
        elif document_type == 'gr':
            qc_words, qc_conf = find_stamp_region(words, _GR_QC_STAMP_KEYWORDS, min_words=2)
            if qc_words:
                x1, y1, x2, y2 = _merge_bbox(qc_words)
                entry = _build_box_entry(
                    'qc_passed_stamp', 'QC Passed Stamp', 1, page_width, page_height,
                    _rect_polygon(x1, y1, x2, y2, pad=10), qc_conf,
                )
                if entry:
                    results.append(entry)

            keyin_words, keyin_conf = find_stamp_region(words, _GR_KEYIN_STAMP_KEYWORDS, min_words=2)
            if keyin_words:
                x1, y1, x2, y2 = _merge_bbox(keyin_words)
                entry = _build_box_entry(
                    'key_in_store_stamp', 'Key-In Store Stamp', 1, page_width, page_height,
                    _rect_polygon(x1, y1, x2, y2, pad=10), keyin_conf,
                )
                if entry:
                    results.append(entry)

        return results
    except Exception as e:
        print(f"DEBUG Vision evidence boxes error for doc={document_id} type={document_type}: "
              f"{type(e).__name__}: {e}")
        return []
