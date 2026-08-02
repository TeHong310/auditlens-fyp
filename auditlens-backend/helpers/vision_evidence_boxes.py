"""
v7 spec — accurate document evidence highlighting.
v8 spec — document-type-aware party/stamp detection.
v9 spec — hybrid Invoice evidence location: Claude supplies semantic
values only (issuer/buyer names, whether a stamp exists — NEVER a
coordinate), Google Vision OCR turns those values into precise pixel
coordinates against the canonical image, and OpenCV performs one
narrowly-scoped refinement step (isolating the red ink of a Buyer
Received stamp within a small area Vision itself already anchored via
a nearby date). See build_vision_evidence_boxes()'s docstring for the
full division of labor.

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

DOCUMENT-TYPE RULES — same real-world counterparty (e.g. the
buyer/receiver company running this AuditLens instance), three
different roles, three different detection strategies:
  - INVOICE (v9 hybrid — see build_vision_evidence_boxes()'s docstring):
    the SUPPLIER owns the page's own letterhead, matched via Claude's
    own detected supplier name (falls back to extracted_vendor_name);
    the BUYER is Claude's own detected buyer name, located near a
    priority-ordered anchor ("Accounts Payable" / "Bill To" / "Sold To"
    / "Customer" / "Ship To"); the Buyer Received Stamp is located by
    Vision anchoring a nearby date, then OpenCV isolating the red ink
    around it. No supplier-address box is built for an invoice — an
    invoice's own letterhead already IS the supplier evidence, and
    address inference risks bleeding into the buyer's block instead, so
    this category is skipped entirely rather than risk a wrong label.
  - PURCHASE ORDER (unchanged, structural only — no Claude semantic
    input): the BUYER issues the PO, so the buyer owns the page's own
    top letterhead; the SUPPLIER appears in a separately labelled
    Vendor/Supplier field, matched via extracted_vendor_name. Supplier
    address IS built (structural inference, same-block-as-supplier-name).
  - GOODS RECEIVED NOTE (unchanged, structural only): same shape as a
    PO — the RECEIVER (buyer) owns the top letterhead, the SUPPLIER is
    matched via extracted_vendor_name. Two distinct buyer-side
    processing stamps are looked for separately (QC Passed, Key-In
    Store) — never a supplier stamp, and never triggered by the word
    "Received" in the document's own title "Goods Received Note" (that
    word isn't in either GR stamp keyword set at all).

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
import numpy as np
import cv2

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


# v9: strict priority order for Invoice buyer anchors — "Accounts
# Payable" is checked FIRST and, if present anywhere at all, every
# lower-priority anchor is ignored even if it also appears on the page.
_BUYER_ANCHOR_PHRASES_PRIORITY = ['accountspayable', 'billto', 'soldto', 'customer', 'shipto']


def find_buyer_near_anchor(words, buyer_name):
    """
    v9 Invoice buyer detection: Claude has already read the page and
    returned buyer_name as plain text (semantic value, no coordinates —
    see build_vision_evidence_boxes()'s docstring). This function's ONLY
    job is to turn that text into a precise location using real Vision
    OCR data:

      1. Try each anchor phrase in _BUYER_ANCHOR_PHRASES_PRIORITY, in
         order — the first one found ANYWHERE in the OCR text wins;
         lower-priority anchors are only tried if every higher one is
         completely absent from the page.
      2. Within that anchor's own visual block, fuzzy-match buyer_name
         against the block's words (same technique as
         match_supplier_name) — never blindly assume every word in the
         block is the buyer, so a false anchor match can't pull in
         unrelated text.
      3. Any remaining words in that block AFTER the matched name are
         included too — the buyer's own nearby address line, per the
         "company name and nearby address" requirement — never words
         BEFORE it, which would be the anchor label itself.

    Returns (words, confidence) or (None, 0.0) if buyer_name is empty,
    no anchor is found at all, or an anchor was found but buyer_name
    doesn't match anything near it.
    """
    if not buyer_name:
        return None, 0.0
    for phrase in _BUYER_ANCHOR_PHRASES_PRIORITY:
        anchor_end = _label_anchor_end(words, {phrase})
        if anchor_end is None:
            continue
        anchor_block = words[anchor_end - 1]['block_index']
        block_words = [w for w in words if w['block_index'] == anchor_block]
        name_words, name_conf, name_idx = match_supplier_name(buyer_name, block_words)
        if not name_words:
            continue
        start, end = name_idx
        trailing = [w for i, w in enumerate(block_words) if i >= end]
        return name_words + trailing, name_conf
    return None, 0.0


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


# ── Stamp text matching (GR only — Invoice uses the date+OpenCV
#    pipeline below instead) ──
#
# Keyword sets are per-document-type and never contain "received" —
# this is what stops a Goods Received Note's own title ("Goods RECEIVED
# Note") from ever being mistaken for a stamp. Generic single-
# character-risk words ("in") are excluded entirely; the remaining
# generic-sounding words ("store", "dept") are only trusted when at
# least 2 keyword words cluster together (min_words), which a single
# incidental page word can't do.

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


# ── Received-stamp date anchor + OpenCV red-region refinement ──
#
# No extracted "received date" ground truth exists anywhere in this
# app's schema, so — unlike supplier/buyer name matching — this can
# only be a general PATTERN match (day + 3-letter month + year), never
# a text comparison against a known value. This is the one place a
# date-shaped string is treated as a location anchor rather than a
# fuzzy name.

_MONTH_ABBR = {'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'}
_DATE_SINGLE_TOKEN_RE = re.compile(r'^\d{1,2}[\-/.](\d{1,2}|[a-z]{3})[\-/.]\d{2,4}$')
_DAY_RE = re.compile(r'^\d{1,2}$')
_YEAR_RE = re.compile(r'^\d{4}$')


def find_date_near(words):
    """Scans the OCR words (reading order) for a date-shaped run: either
    one single token like "04-MAR-2026" / "04/03/2026", or 3 consecutive
    tokens forming day + 3-letter month + year, e.g. "04" "MAR" "2026" —
    the exact style the task's own Coilcraft stamp example uses. Returns
    the matched word(s) (for their bounding box, used only as a search
    ANCHOR for the OpenCV step below — never rendered as its own
    evidence box) or None if nothing date-shaped is found anywhere."""
    n = len(words)
    for i in range(n):
        if _DATE_SINGLE_TOKEN_RE.match(words[i]['text'].lower()):
            return [words[i]]
        if i + 2 < n:
            day, month, year = words[i]['text'], words[i + 1]['text'], words[i + 2]['text']
            if _DAY_RE.match(day) and _normalize(month) in _MONTH_ABBR and _YEAR_RE.match(year):
                return [words[i], words[i + 1], words[i + 2]]
    return None


def find_red_stamp_region(image_bytes, search_bbox, page_width, page_height):
    """
    OpenCV refinement step (v9) — the ONLY place in this module that
    touches raw pixels instead of Vision word geometry. Decodes the SAME
    canonical PNG bytes Vision OCR and the frontend preview both use,
    crops to a LIMITED area expanded around search_bbox (the date
    Vision just anchored — never the whole page, and never a hard-coded
    region), thresholds for red ink in HSV space, and merges every red
    contour above a noise-floor area into one bounding box.

    Returns (x1, y1, x2, y2) in full-image pixel coordinates, or None if
    no red ink is found in the search area at all — the caller must
    treat that as "not reliably located" (Location unavailable), never
    fall back to a guessed box.
    """
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    sx1, sy1, sx2, sy2 = search_bbox
    # A physical stamp is normally noticeably larger than just the date
    # line printed/stamped inside it, so the search crop is expanded
    # generously around the date's own (small) bounding box.
    margin_x = (sx2 - sx1) * 3 + 60
    margin_y = (sy2 - sy1) * 5 + 60
    cx1 = max(0, int(sx1 - margin_x))
    cy1 = max(0, int(sy1 - margin_y))
    cx2 = min(page_width, int(sx2 + margin_x))
    cy2 = min(page_height, int(sy2 + margin_y))
    if cx2 <= cx1 or cy2 <= cy1:
        return None

    crop = img[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # Red wraps around hue 0/180 in OpenCV's 0-179 hue range, so it
    # needs two bands, OR'd together.
    mask = (
        cv2.inRange(hsv, np.array([0, 70, 50]), np.array([10, 255, 255])) |
        cv2.inRange(hsv, np.array([170, 70, 50]), np.array([180, 255, 255]))
    )

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_contour_area = 15  # ignores single-pixel/antialiasing noise
    boxes = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) >= min_contour_area]
    if not boxes:
        return None

    bx1 = min(b[0] for b in boxes)
    by1 = min(b[1] for b in boxes)
    bx2 = max(b[0] + b[2] for b in boxes)
    by2 = max(b[1] + b[3] for b in boxes)

    # Map the crop-local box back to full canonical-image coordinates.
    return cx1 + bx1, cy1 + by1, cx1 + bx2, cy1 + by2


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


def build_vision_evidence_boxes(document_id, document_type, image_bytes, extracted_vendor_name,
                                  claude_supplier_name=None, claude_buyer_name=None,
                                  claude_stamp_detected=False):
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

    v9 hybrid division of labor (Invoice only — PO/GR are unchanged):
    Claude supplies SEMANTIC values only —
      claude_supplier_name:   Claude's own visually-detected issuer name
      claude_buyer_name:      Claude's own visually-detected buyer name
      claude_stamp_detected:  whether Claude confirmed a stamp exists
    — never a pixel coordinate. This function is the ONLY place those
    values are ever turned into coordinates, always by matching them
    against real Google Vision OCR word geometry (or, for the stamp,
    Vision anchoring a nearby date + a scoped OpenCV red-ink search).
    extracted_vendor_name (the separate extraction pipeline's value)
    remains the fallback for invoice supplier matching when Claude
    found nothing, and is still the ONLY source used for PO/GR.

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

        # Invoice prefers Claude's OWN visually-detected supplier name —
        # the same AI pass whose image Vision OCR is analyzing here —
        # falling back to the extraction pipeline's hint only if Claude
        # found nothing. PO/GR are untouched: always extracted_vendor_name.
        if document_type == 'invoice':
            supplier_match_target = claude_supplier_name or extracted_vendor_name
        else:
            supplier_match_target = extracted_vendor_name

        if supplier_match_target:
            name_words, name_conf, name_indices = match_supplier_name(supplier_match_target, words)
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

        # ── Buyer / issuer / receiver. ──
        supplier_block = words[name_indices[0]]['block_index'] if name_indices else None
        if document_type == 'invoice':
            # v9: Claude's buyer_name, precisely located via the
            # priority-ordered anchor search + fuzzy match. Falls back
            # to the older blind label-block grab only when Claude
            # found no buyer at all (e.g. the Gemini/fallback engine,
            # which has no buyer_identity concept) or nothing matched
            # near any anchor — never silently produces nothing when a
            # reasonable best-effort location still exists.
            buyer_words, buyer_conf = find_buyer_near_anchor(words, claude_buyer_name)
            if not buyer_words:
                buyer_words, buyer_conf = find_buyer_block_by_label(words)
            buyer_label = 'Buyer'
        else:  # po, gr — unchanged structural detection
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
            # v9: Claude must first confirm a stamp actually exists —
            # Vision/OpenCV only ever REFINE a location Claude already
            # semantically confirmed, never invent one on their own.
            if claude_stamp_detected:
                date_words = find_date_near(words)
                if date_words:
                    date_bbox = _merge_bbox(date_words)
                    red_bbox = find_red_stamp_region(image_bytes, date_bbox, page_width, page_height)
                    if red_bbox:
                        sx1, sy1, sx2, sy2 = red_bbox
                        entry = _build_box_entry(
                            'buyer_received_stamp', 'Buyer Received Stamp', 1, page_width, page_height,
                            _rect_polygon(sx1, sy1, sx2, sy2, pad=6), 0.85,
                        )
                        if entry:
                            results.append(entry)
                # No date anchor, or no red ink found near it: the stamp
                # "cannot be located reliably" per spec — deliberately NO
                # fallback to a different method here. The frontend shows
                # "Location unavailable" (Claude confirmed it exists;
                # this module just couldn't pin a trustworthy box).
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
