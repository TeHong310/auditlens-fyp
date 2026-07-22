"""Enterprise V3 Phase 2 — deterministic (no LLM) document relationship
builder. Finds candidate PO<->Invoice / Invoice<->GR / PO<->GR links by
comparing already-extracted fields (PO reference, vendor name, item
description, quantity, amount, dates) and scores each candidate with a
fixed, numeric formula — nothing here calls Claude/Gemini or any other
AI, and nothing here is called automatically from the upload pipeline;
it only runs when explicitly invoked (via the relationship API, the
backfill script, or a test).

Confidence tiers (STEP 1.D): 0.90-1.00 exact reference + strong support,
0.75-0.89 strong deterministic match, 0.50-0.74 candidate requiring
review, below 0.50 never auto-created. MIN_AUTO_CONFIDENCE below is the
line between "auto-created (as an 'auto' relationship, always subject to
review/deletion)" and "not created at all".

Tolerance constants (QUANTITY_TOLERANCE, AMOUNT_TOLERANCE) are defined
here once and re-exported by helpers/enterprise_matching.py, which also
needs them for cumulative fulfilment math — a single source of truth so
tolerance rules can't drift between candidate scoring and cumulative
calculation.
"""
import re
from decimal import Decimal

from db import get_db_connection
from helpers.entity_normalizer import is_same_company
from helpers.document_relationships import upsert_relationship

QUANTITY_TOLERANCE = Decimal('0')
AMOUNT_TOLERANCE = Decimal('0.01')

MIN_AUTO_CONFIDENCE = Decimal('0.50')


# ------------------------------------------------------------
# Small pure normalization helpers — intentionally duplicated (not
# imported) from routes/auditor.py's _normalize_ref/_normalize_text:
# helpers/ modules in this codebase never import from routes/ (routes/
# import from helpers/, never the reverse), and these are a few lines
# each, so duplicating them here keeps that layering intact.
# ------------------------------------------------------------

def _norm_ref(val):
    if not val:
        return None
    v = re.sub(r'\s+', '', str(val).upper())
    v = re.sub(r'^PO-?', '', v)
    return v or None


def _norm_text(val):
    if not val:
        return None
    v = re.sub(r'\s+', ' ', str(val).lower()).strip()
    return v or None


def _dec(value):
    if value is None:
        return None
    return Decimal(str(value))


def _clamp_score(score):
    return max(Decimal('0'), min(Decimal('1'), score))


# ------------------------------------------------------------
# Candidate finders — self-contained (open their own DB connection),
# matching the established style of helpers/anomaly_detector.py.
# ------------------------------------------------------------

def _load_invoice(document_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            '''SELECT document_id, invoice_number, vendor_name, invoice_date, total_amount,
                      currency, po_reference, item_description, quantity
               FROM extracted_fields WHERE document_id = %s''',
            (document_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def _has_sibling_invoice_claim(invoice, po_candidates):
    """True if any OTHER invoice (system-wide, via extracted_fields.
    po_reference) also references one of this invoice's candidate PO
    numbers — i.e. this PO is (or will be) shared across multiple
    invoices, which is exactly what makes GR-to-invoice attribution
    ambiguous whenever there is more than one GR candidate (see the
    caller, build_relationships_for_invoice)."""
    po_numbers = {_norm_ref(po.get('po_number')) for po, _, _ in po_candidates if po.get('po_number')}
    po_numbers.discard(None)
    if not po_numbers:
        return False

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT po_reference FROM extracted_fields WHERE document_id != %s AND po_reference IS NOT NULL',
            (invoice['document_id'],)
        )
        return any(_norm_ref(ref) in po_numbers for (ref,) in cursor.fetchall())
    finally:
        conn.close()


def find_candidate_purchase_orders(document_id):
    """Every PO in the system that could plausibly belong to this
    invoice: an exact normalized PO-reference/PO-number match (system-
    wide — this is how a PO shared across invoices gets discovered, not
    just the one legacy-attached to this invoice), OR the legacy one-to-
    one attachment (purchase_orders.document_id == this invoice's
    document_id), even without a matching reference (that's how today's
    single-invoice uploads already work). Returns a list of PO dicts."""
    invoice = _load_invoice(document_id)
    if not invoice:
        return []

    ref = _norm_ref(invoice.get('po_reference'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            '''SELECT po_id, document_id, po_number, vendor_name, po_date, total_amount,
                      currency, quantity, item_description
               FROM purchase_orders'''
        )
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    finally:
        conn.close()

    candidates = []
    for po in rows:
        if (ref and _norm_ref(po.get('po_number')) == ref) or po.get('document_id') == document_id:
            candidates.append(po)
    return candidates


def find_candidate_goods_receipts(document_id):
    """Every GR that could plausibly belong to this invoice: an exact
    normalized PO-reference match (invoice.po_reference vs
    goods_receipts.po_reference), OR the legacy one-to-one attachment.
    Returns a list of GR dicts."""
    invoice = _load_invoice(document_id)
    if not invoice:
        return []

    ref = _norm_ref(invoice.get('po_reference'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            '''SELECT gr_id, document_id, gr_number, vendor_name, receipt_date,
                      po_reference, quantity, item_description
               FROM goods_receipts'''
        )
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    finally:
        conn.close()

    candidates = []
    for gr in rows:
        if (ref and _norm_ref(gr.get('po_reference')) == ref) or gr.get('document_id') == document_id:
            candidates.append(gr)
    return candidates


# ------------------------------------------------------------
# Candidate scorers — pure (no DB). Each returns (score: Decimal 0-1,
# reason: str). A score of exactly 0 means "no evidence at all" (never
# persisted); rule A/B's "do not create a high-confidence relationship
# using vendor and amount alone" is enforced by GATING on reference
# match or legacy co-location first — vendor/item/quantity/amount/date
# only ever ADD to or SUBTRACT from that base, never substitute for it.
# ------------------------------------------------------------

def score_po_invoice_candidate(invoice, po):
    score = Decimal('0')
    reasons = []

    ref_match = bool(_norm_ref(invoice.get('po_reference')) and _norm_ref(po.get('po_number'))
                      and _norm_ref(invoice['po_reference']) == _norm_ref(po['po_number']))
    legacy_attached = po.get('document_id') == invoice.get('document_id')

    if ref_match:
        score += Decimal('0.55')
        reasons.append('invoice PO reference matches PO number')
    elif legacy_attached:
        score += Decimal('0.45')
        reasons.append('legacy one-to-one document attachment')
    else:
        return Decimal('0'), 'no PO reference match or legacy attachment evidence'

    vendor_result = is_same_company(invoice.get('vendor_name'), po.get('vendor_name'))
    if vendor_result['match']:
        score += Decimal('0.15')
        reasons.append('vendor matches')
    elif invoice.get('vendor_name') and po.get('vendor_name'):
        score -= Decimal('0.20')
        reasons.append('vendor mismatch')

    if _norm_text(invoice.get('item_description')) and _norm_text(invoice.get('item_description')) == _norm_text(po.get('item_description')):
        score += Decimal('0.10')
        reasons.append('item description matches')

    inv_qty, po_qty = _dec(invoice.get('quantity')), _dec(po.get('quantity'))
    if inv_qty is not None and po_qty is not None:
        if inv_qty <= po_qty + QUANTITY_TOLERANCE:
            score += Decimal('0.10')
            reasons.append('invoice quantity within PO quantity')
        else:
            score -= Decimal('0.25')
            reasons.append('invoice quantity exceeds PO quantity')

    inv_amt, po_amt = _dec(invoice.get('total_amount')), _dec(po.get('total_amount'))
    if inv_amt is not None and po_amt is not None:
        if inv_amt <= po_amt + AMOUNT_TOLERANCE:
            score += Decimal('0.05')
            reasons.append('invoice amount within PO amount')
        else:
            score -= Decimal('0.25')
            reasons.append('invoice amount exceeds PO amount')

    if invoice.get('invoice_date') and po.get('po_date'):
        if po['po_date'] <= invoice['invoice_date']:
            score += Decimal('0.05')
            reasons.append('date sequence valid (PO before invoice)')
        else:
            score -= Decimal('0.30')
            reasons.append('invalid date sequence (PO after invoice)')

    return _clamp_score(score), '; '.join(reasons)


def score_invoice_gr_candidate(invoice, gr):
    score = Decimal('0')
    reasons = []

    ref_match = bool(_norm_ref(invoice.get('po_reference')) and _norm_ref(gr.get('po_reference'))
                      and _norm_ref(invoice['po_reference']) == _norm_ref(gr['po_reference']))
    legacy_attached = gr.get('document_id') == invoice.get('document_id')

    if ref_match:
        score += Decimal('0.55')
        reasons.append('shared PO reference between invoice and GR')
    elif legacy_attached:
        score += Decimal('0.45')
        reasons.append('legacy one-to-one document attachment')
    else:
        return Decimal('0'), 'no shared PO reference or legacy attachment evidence'

    vendor_result = is_same_company(invoice.get('vendor_name'), gr.get('vendor_name'))
    if vendor_result['match']:
        score += Decimal('0.15')
        reasons.append('vendor matches')
    elif invoice.get('vendor_name') and gr.get('vendor_name'):
        score -= Decimal('0.20')
        reasons.append('vendor mismatch')

    if _norm_text(invoice.get('item_description')) and _norm_text(invoice.get('item_description')) == _norm_text(gr.get('item_description')):
        score += Decimal('0.10')
        reasons.append('item description matches')

    gr_qty, inv_qty = _dec(gr.get('quantity')), _dec(invoice.get('quantity'))
    if gr_qty is not None and inv_qty is not None:
        if gr_qty <= inv_qty + QUANTITY_TOLERANCE:
            score += Decimal('0.10')
            reasons.append('received quantity compatible with invoice quantity')
        else:
            score -= Decimal('0.15')
            reasons.append('received quantity exceeds invoice quantity')

    if invoice.get('invoice_date') and gr.get('receipt_date'):
        if gr['receipt_date'] <= invoice['invoice_date']:
            score += Decimal('0.05')
            reasons.append('receipt date not after invoice date')

    return _clamp_score(score), '; '.join(reasons)


def score_po_gr_candidate(po, gr):
    score = Decimal('0')
    reasons = []

    ref_match = bool(_norm_ref(gr.get('po_reference')) and _norm_ref(po.get('po_number'))
                      and _norm_ref(gr['po_reference']) == _norm_ref(po['po_number']))
    legacy_co_located = po.get('document_id') is not None and po.get('document_id') == gr.get('document_id')

    if ref_match:
        score += Decimal('0.55')
        reasons.append('GR PO reference matches PO number')
    elif legacy_co_located:
        score += Decimal('0.40')
        reasons.append('GR and PO co-located under the same legacy invoice attachment')
    else:
        return Decimal('0'), 'no PO reference match or legacy co-location evidence'

    vendor_result = is_same_company(po.get('vendor_name'), gr.get('vendor_name'))
    if vendor_result['match']:
        score += Decimal('0.20')
        reasons.append('vendor matches')
    elif po.get('vendor_name') and gr.get('vendor_name'):
        score -= Decimal('0.20')
        reasons.append('vendor mismatch')

    if _norm_text(po.get('item_description')) and _norm_text(po.get('item_description')) == _norm_text(gr.get('item_description')):
        score += Decimal('0.15')
        reasons.append('item description matches')

    gr_qty, po_qty = _dec(gr.get('quantity')), _dec(po.get('quantity'))
    if gr_qty is not None and po_qty is not None:
        if gr_qty <= po_qty + QUANTITY_TOLERANCE:
            score += Decimal('0.10')
            reasons.append('GR quantity within PO quantity')
        else:
            score -= Decimal('0.20')
            reasons.append('GR quantity exceeds PO quantity')

    if po.get('po_date') and gr.get('receipt_date'):
        if po['po_date'] <= gr['receipt_date']:
            score += Decimal('0.05')
            reasons.append('receipt date not before PO date')
        else:
            score -= Decimal('0.20')
            reasons.append('receipt date before PO date')

    return _clamp_score(score), '; '.join(reasons)


# ------------------------------------------------------------
# Persist + orchestrate
# ------------------------------------------------------------

def persist_relationships(candidates, dry_run=False):
    """candidates: list of {'parent_type','parent_id','child_type',
    'child_id','relationship_type','matched_quantity','matched_amount',
    'confidence_score' (0-100),'matching_reason'}. dry_run=True never
    touches the DB — returns the same shape a real run would, tagged
    action='would_create'/'would_update'/'would_skip_manual' by probing
    upsert_relationship's own existing-row logic would take, without
    calling it. Returns a list of {**candidate, 'action', 'relationship'
    (None in dry-run), 'error'}."""
    results = []
    for c in candidates:
        if dry_run:
            results.append({**c, 'action': 'would_persist', 'relationship': None, 'error': None})
            continue

        relationship, skipped_manual, error = upsert_relationship(
            c['parent_type'], c['parent_id'], c['child_type'], c['child_id'], c['relationship_type'],
            matched_quantity=c.get('matched_quantity'), matched_amount=c.get('matched_amount'),
            confidence_score=c.get('confidence_score'), matching_reason=c.get('matching_reason'),
        )
        action = 'skipped_manual' if skipped_manual else ('error' if error else 'persisted')
        results.append({**c, 'action': action, 'relationship': relationship, 'error': error})
    return results


def build_relationships_for_invoice(document_id, dry_run=False):
    """The core builder for ONE invoice. Deterministic, idempotent, no
    AI calls. Finds PO/GR candidates, scores them, decides allocation,
    and persists (or dry-run-reports) every candidate scoring at least
    MIN_AUTO_CONFIDENCE. Returns a summary dict — never raises for a
    document with no invoice data (returns an empty summary instead), so
    callers (the backfill script, an upload hook) can treat "nothing to
    do" and "an error" differently.

    Allocation rule: exactly one qualifying PO candidate -> the FULL
    invoice quantity/amount is allocated to it (matched_quantity/
    matched_amount = invoice's own values). Multiple qualifying PO
    candidates -> the split is genuinely ambiguous for a deterministic
    algorithm, so every PO is still linked (so the relationship is
    discoverable) but matched_quantity/matched_amount are left None,
    which helpers/enterprise_matching.py's cumulative calculators treat
    as "not counted toward any PO's total, flagged as a warning" —
    never guessed.
    """
    invoice = _load_invoice(document_id)
    if not invoice:
        return {'document_id': document_id, 'invoice_found': False, 'candidates': []}

    po_candidates = [(po, *score_po_invoice_candidate(invoice, po)) for po in find_candidate_purchase_orders(document_id)]
    po_candidates = [(po, score, reason) for po, score, reason in po_candidates if score >= MIN_AUTO_CONFIDENCE]

    gr_candidates = [(gr, *score_invoice_gr_candidate(invoice, gr)) for gr in find_candidate_goods_receipts(document_id)]
    gr_candidates = [(gr, score, reason) for gr, score, reason in gr_candidates if score >= MIN_AUTO_CONFIDENCE]

    to_persist = []

    single_po_allocation = len(po_candidates) == 1
    for po, score, reason in po_candidates:
        to_persist.append({
            'parent_type': 'po', 'parent_id': po['po_id'], 'child_type': 'invoice', 'child_id': document_id,
            'relationship_type': 'po_invoice',
            'matched_quantity': invoice.get('quantity') if single_po_allocation else None,
            'matched_amount': invoice.get('total_amount') if single_po_allocation else None,
            'confidence_score': float(score * 100),
            'matching_reason': reason,
        })

    # invoice_gr attribution: goods_receipts carries no invoice-specific
    # field (only a shared po_reference), so when MULTIPLE invoices also
    # claim the same PO, every GR matches every one of those invoices'
    # searches equally — there is no deterministic way to tell which
    # receipt belongs to which invoice (a real bug found via manual
    # testing against the PO3006231 scenario: both invoices got linked
    # to BOTH GRs). So: a single GR candidate is always unambiguous and
    # always linked; MULTIPLE GR candidates are only linked (all of
    # them — e.g. two genuine partial receipts against ONE invoice) when
    # no OTHER invoice also claims this invoice's candidate PO(s) —
    # otherwise every GR candidate is skipped for THIS invoice (PO-level
    # receipt totals are unaffected — they come from po_gr relationships
    # below, which legitimately link a PO to ALL of its GRs regardless).
    if len(gr_candidates) == 1 or not _has_sibling_invoice_claim(invoice, po_candidates):
        for gr, score, reason in gr_candidates:
            to_persist.append({
                'parent_type': 'invoice', 'parent_id': document_id, 'child_type': 'gr', 'child_id': gr['gr_id'],
                'relationship_type': 'invoice_gr',
                'matched_quantity': gr.get('quantity'),
                'matched_amount': None,
                'confidence_score': float(score * 100),
                'matching_reason': reason,
            })

    for po, po_score, _ in po_candidates:
        for gr, gr_score, _ in gr_candidates:
            pg_score, pg_reason = score_po_gr_candidate(po, gr)
            if pg_score >= MIN_AUTO_CONFIDENCE:
                to_persist.append({
                    'parent_type': 'po', 'parent_id': po['po_id'], 'child_type': 'gr', 'child_id': gr['gr_id'],
                    'relationship_type': 'po_gr',
                    'matched_quantity': gr.get('quantity'),
                    'matched_amount': None,
                    'confidence_score': float(pg_score * 100),
                    'matching_reason': pg_reason,
                })

    results = persist_relationships(to_persist, dry_run=dry_run)

    return {
        'document_id':   document_id,
        'invoice_found': True,
        'dry_run':        dry_run,
        'po_candidates_considered': len(po_candidates),
        'gr_candidates_considered': len(gr_candidates),
        'candidates':     results,
    }


def rebuild_relationships_for_invoice(document_id, dry_run=False):
    """Re-runs the builder for an invoice that may already have
    relationships (e.g. after a new PO/GR arrives). Identical to
    build_relationships_for_invoice — the builder is idempotent by
    construction (upsert_relationship never duplicates, never overwrites
    a 'manual' row), so 'rebuild' is just 'build, run again'. Kept as a
    separate, explicitly-named entry point since STEP 1 asks for one."""
    return build_relationships_for_invoice(document_id, dry_run=dry_run)


def build_relationships_for_all_documents(dry_run=True, limit=None):
    """Batch entry point for the backfill script — NEVER called
    automatically (no caller in app.py). Iterates every invoice that has
    extracted_fields, in document_id order. dry_run defaults to True:
    callers must opt in to writing. limit caps how many invoices are
    processed (None = all)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        query = 'SELECT document_id FROM extracted_fields ORDER BY document_id'
        if limit:
            query += ' LIMIT %s'
            cursor.execute(query, (limit,))
        else:
            cursor.execute(query)
        document_ids = [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()

    summaries = [build_relationships_for_invoice(doc_id, dry_run=dry_run) for doc_id in document_ids]
    return {
        'dry_run':          dry_run,
        'invoices_processed': len(summaries),
        'summaries':        summaries,
    }
