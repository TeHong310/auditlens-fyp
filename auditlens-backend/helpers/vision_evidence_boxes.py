"""
v7 spec — accurate document evidence highlighting.

Builds real, Google-Vision-derived polygon regions for supplier name,
supplier address, and stamp text, by matching Google Vision OCR word
boxes against the values this app has already extracted — never by
estimating/guessing a pixel position.

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

Handwritten annotations are NOT implemented here: Google Vision's
DOCUMENT_TEXT_DETECTION has no handwriting-specific classification to
key off (it OCRs handwritten and printed text identically), so there
is no reliable coordinate signal to build a box from. Per this
feature's own "do not manually estimate coordinates" rule, that means
correctly building nothing, not fabricating a placeholder.
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
        supplier-address heuristic below).
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
    found), scored via difflib's SequenceMatcher ratio. This is real
    text matching against real OCR words — not a guessed position.

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
    not a guess: an invoice/PO/GR header block is very commonly laid
    out as [Company Name line, Address line(s), ...] inside one visual
    block, so the remaining words in the SAME block as the matched
    supplier name are taken as the address candidate.

    Returns (words, confidence) or (None, 0.0) if the matched name's
    block has no other words (nothing to call an address) — confidence
    is capped below a direct text match's, to honestly reflect that
    this is a structural inference, not a verified string match.
    """
    if not name_indices:
        return None, 0.0
    start, end = name_indices
    name_block = words[start]['block_index']
    same_block_words = [
        w for i, w in enumerate(words)
        if w['block_index'] == name_block and not (start <= i < end)
    ]
    if len(same_block_words) >= 2:
        return same_block_words, _ADDRESS_STRUCTURAL_CONFIDENCE
    return None, 0.0


# ── Stamp text matching ──

# "Recognisable stamp text" per the spec's own examples — a disclosed,
# fixed keyword set, not a guess at any particular document's layout.
STAMP_KEYWORDS = {
    'received', 'qc', 'passed', 'approved', 'verified',
    'inspected', 'checked', 'accepted', 'pass',
}


def find_stamp_region(words):
    """
    Finds OCR words matching a recognised stamp keyword, then clusters
    them by physical proximity (scaled to the matched words' own
    height, so the threshold isn't tied to any one render resolution)
    — words belonging to the same physical stamp are tightly packed;
    an unrelated keyword occurrence elsewhere on the page forms its own
    cluster and is not merged in. Returns the largest cluster's words
    and a confidence that grows with how many keyword words agree,
    or (None, 0.0) if no stamp keyword was found at all.
    """
    matches = [w for w in words if _normalize(w['text']) in STAMP_KEYWORDS]
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
    evidence regions for supplier name / supplier address / stamp text
    — the categories real OCR word-matching actually supports.

    Returns a list of v7-schema dicts (see module docstring), each
    already validated and safe to render as-is. Returns [] on any
    failure, when Vision found no usable text, or when nothing matched
    confidently enough — never raises.
    """
    try:
        words, page_width, page_height = run_google_vision_ocr_with_boxes(image_bytes)
        if not words or not page_width or not page_height:
            return []

        results = []

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

                addr_words, addr_conf = find_address_near_name(words, name_indices)
                if addr_words:
                    ax1, ay1, ax2, ay2 = _merge_bbox(addr_words)
                    addr_entry = _build_box_entry(
                        'supplier_address', 'Supplier Address', 1, page_width, page_height,
                        _rect_polygon(ax1, ay1, ax2, ay2, pad=4), addr_conf,
                    )
                    if addr_entry:
                        results.append(addr_entry)

        stamp_words, stamp_conf = find_stamp_region(words)
        if stamp_words:
            sx1, sy1, sx2, sy2 = _merge_bbox(stamp_words)
            stamp_entry = _build_box_entry(
                'stamp_text', 'Stamp', 1, page_width, page_height,
                _rect_polygon(sx1, sy1, sx2, sy2, pad=10), stamp_conf,
            )
            if stamp_entry:
                results.append(stamp_entry)

        return results
    except Exception as e:
        print(f"DEBUG Vision evidence boxes error for doc={document_id} type={document_type}: "
              f"{type(e).__name__}: {e}")
        return []
