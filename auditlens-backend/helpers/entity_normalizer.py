"""Vendor entity normalization — a company name extracted independently
from an invoice, a PO, and a GR can differ in spacing, line breaks,
capitalization, company-suffix wording, and OCR spelling mistakes even
when it's genuinely the same supplier (e.g. "COLCRAFT SINGAPORE PTE
LTD" vs "Coilcraft Singapore PTE LTD"). Exact-string or naive
substring/character-overlap comparison (the previous approach in
routes/auditor.py and routes/matching.py) reports these as DIFFERENT
even though a human reading both documents would immediately recognize
the same company. This module is the single, reusable source of truth
for "are these two vendor name strings the same company" — used by
every document-comparison path, not just one example document.
"""
import re
from difflib import SequenceMatcher

DEFAULT_SIMILARITY_THRESHOLD = 90

# Longest/most-specific phrases first — "private limited"/"pte ltd" must
# be stripped whole before the shorter "limited"/"ltd" alternative could
# otherwise match only part of them and leave a stray "private"/"pte"
# behind. Matched after punctuation has already been stripped, so a
# suffix like "Pte. Ltd." (which became "pte ltd" by then) is still
# recognized without needing separate punctuated variants here.
_COMPANY_SUFFIXES = (
    'private limited',
    'pte ltd',
    'sdn bhd',
    'corporation',
    'limited',
    'ltd',
    'corp',
    'inc',
)
_SUFFIX_RE = re.compile(r'\b(?:' + '|'.join(_COMPANY_SUFFIXES) + r')\b\.?\s*$')


def normalize_company_name(name):
    """1. lowercase; 2. strip punctuation/brackets/commas/periods/extra
    whitespace/newlines; 3. strip a trailing company suffix (repeated,
    in case more than one is chained). Returns '' for empty/None input.
    """
    if not name:
        return ''
    text = str(name).lower()
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'[.,()\[\]{}]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    prev = None
    while prev != text:
        prev = text
        text = _SUFFIX_RE.sub('', text).strip()
    return text


def calculate_entity_similarity(name_a, name_b):
    """0-100 similarity between two company names, computed on their
    NORMALIZED forms (see normalize_company_name()) via difflib's
    SequenceMatcher — tolerant of small OCR spelling mistakes (e.g. a
    single dropped/substituted letter) since ratio() is based on longest
    common matching subsequences, not exact character positions."""
    norm_a = normalize_company_name(name_a)
    norm_b = normalize_company_name(name_b)
    if not norm_a or not norm_b:
        return 0.0
    return round(SequenceMatcher(None, norm_a, norm_b).ratio() * 100, 1)


def is_same_company(name_a, name_b, threshold=DEFAULT_SIMILARITY_THRESHOLD):
    """Returns {match, similarity, normalized_source, normalized_target}.
    `match` is True when similarity >= threshold (default 90%)."""
    norm_a = normalize_company_name(name_a)
    norm_b = normalize_company_name(name_b)
    similarity = calculate_entity_similarity(name_a, name_b)
    return {
        'match': similarity >= threshold,
        'similarity': similarity,
        'normalized_source': norm_a,
        'normalized_target': norm_b,
    }


def log_entity_match_debug(source_label, source_original, target_label, target_original, result):
    """Structured production log:

    ENTITY MATCH DEBUG

    Source:
    Invoice vendor

    Original:
    COLCRAFT SINGAPORE PTE LTD

    Normalized:
    colcraft singapore


    Target:
    PO vendor

    Original:
    Coilcraft Singapore PTE LTD

    Normalized:
    coilcraft singapore


    Similarity:
    97.4%


    Result:
    MATCH
    """
    status = 'MATCH' if result['match'] else 'DIFFERENT'
    print(
        f"ENTITY MATCH DEBUG\n\n"
        f"Source:\n{source_label}\n\n"
        f"Original:\n{source_original}\n\n"
        f"Normalized:\n{result['normalized_source']}\n\n\n"
        f"Target:\n{target_label}\n\n"
        f"Original:\n{target_original}\n\n"
        f"Normalized:\n{result['normalized_target']}\n\n\n"
        f"Similarity:\n{result['similarity']}%\n\n\n"
        f"Result:\n{status}"
    )
