"""Enterprise V3 Phase 2 — pure cumulative-matching calculators.

No DB access, no Flask, no imports from routes/ — every function here
takes plain dicts/values already fetched by the caller (routes/auditor.
py::_build_comparison_v2) and returns plain dicts/values, so this module
is independently unit-testable and has no risk of a circular import with
routes/auditor.py (which needs to call into this module).

All monetary/quantity arithmetic uses Decimal, never float, per the
allocation rules (STEP 2: "Do not use floating-point arithmetic for
monetary comparison").

Tolerance constants are defined once in helpers/relationship_builder.py
(the module that also needs them for candidate scoring) and re-exported
here, so there is exactly one place tolerances can drift.
"""
from decimal import Decimal

from helpers.relationship_builder import QUANTITY_TOLERANCE, AMOUNT_TOLERANCE


def _dec(value):
    """None-safe Decimal conversion. Converts via str() first — never
    Decimal(a_float) directly — so binary float imprecision (e.g.
    0.1 + 0.2 != 0.3) never leaks into monetary comparisons."""
    if value is None:
        return None
    return Decimal(str(value))


def _sum_dec(values):
    total = Decimal('0')
    for v in values:
        d = _dec(v)
        if d is not None:
            total += d
    return total


def _progress_status(ordered, actual, tolerance, verbs):
    """verbs: (over, full, partial, none) status label 4-tuple, e.g.
    ('OVER_INVOICED', 'FULLY_INVOICED', 'OPEN_PARTIALLY_INVOICED',
    'OPEN_NOT_INVOICED')."""
    over, full, partial, none = verbs
    if ordered is None or ordered <= 0:
        return None  # caller decides REVIEW_REQUIRED when this happens with real activity
    if actual > ordered + tolerance:
        return over
    if actual >= ordered - tolerance:
        return full
    if actual > 0:
        return partial
    return none


def compute_po_fulfilment(po, invoice_allocations, gr_quantities,
                           quantity_tolerance=QUANTITY_TOLERANCE, amount_tolerance=AMOUNT_TOLERANCE):
    """Cumulative fulfilment for ONE purchase order.

    po: {'po_id', 'po_number', 'quantity', 'total_amount'} (quantity/
      total_amount may be None/0 if extraction didn't capture them).
    invoice_allocations: list of {'document_id', 'matched_quantity',
      'matched_amount'} — one entry per invoice relationship linked to
      this PO. An entry with matched_quantity/matched_amount = None is
      an AMBIGUOUS allocation (the builder found multiple PO candidates
      for that invoice and could not deterministically split it — see
      helpers/relationship_builder.py) and is excluded from the
      cumulative totals (never guessed), but still surfaces as a warning
      via 'unallocated_invoice_count'.
    gr_quantities: list of received quantities (numbers), one per GR
      relationship linked to this PO (directly, or via a linked
      invoice's invoice_gr relationship).

    Returns a dict matching STEP 3's suggested po_fulfilment shape, plus
    an additive 'received_status' field (see STEP 4B: a PO can be
    reported as "OPEN_PARTIALLY_INVOICED / OPEN_PARTIALLY_RECEIVED" —
    'status' below carries the invoiced-progress axis, with an
    OVER_*/FULLY_FULFILLED override; 'received_status' carries the
    receipt-progress axis independently, so both axes stay visible).
    """
    ordered_quantity = _dec(po.get('quantity'))
    po_amount = _dec(po.get('total_amount'))

    allocated = [a for a in invoice_allocations if a.get('matched_quantity') is not None]
    unallocated_count = len(invoice_allocations) - len(allocated)

    invoiced_quantity = _sum_dec(a['matched_quantity'] for a in allocated)
    invoiced_amount = _sum_dec(a.get('matched_amount') for a in allocated if a.get('matched_amount') is not None)
    received_quantity = _sum_dec(gr_quantities)

    remaining_to_invoice = None
    remaining_to_receive = None
    remaining_amount = None
    if ordered_quantity is not None:
        remaining_to_invoice = max(Decimal('0'), ordered_quantity - invoiced_quantity)
        remaining_to_receive = max(Decimal('0'), ordered_quantity - received_quantity)
    if po_amount is not None:
        remaining_amount = max(Decimal('0'), po_amount - invoiced_amount)

    invoiced_status = _progress_status(
        ordered_quantity, invoiced_quantity, quantity_tolerance,
        ('OVER_INVOICED', 'FULLY_INVOICED', 'OPEN_PARTIALLY_INVOICED', 'OPEN_NOT_INVOICED'))
    received_status = _progress_status(
        ordered_quantity, received_quantity, quantity_tolerance,
        ('OVER_RECEIVED', 'FULLY_RECEIVED', 'OPEN_PARTIALLY_RECEIVED', 'OPEN_NOT_RECEIVED'))

    if invoiced_status is None or received_status is None:
        # No usable ordered_quantity on the PO itself, but invoices/GRs
        # exist against it — can't compute fulfilment meaningfully.
        status = 'REVIEW_REQUIRED' if (invoiced_quantity or received_quantity) else 'OPEN_NOT_INVOICED'
        received_status = received_status or 'OPEN_NOT_RECEIVED'
    elif invoiced_status == 'OVER_INVOICED':
        status = 'OVER_INVOICED'
    elif received_status == 'OVER_RECEIVED':
        status = 'OVER_RECEIVED'
    elif invoiced_status == 'FULLY_INVOICED' and received_status == 'FULLY_RECEIVED':
        status = 'FULLY_FULFILLED'
    else:
        status = invoiced_status

    return {
        'po_id':                        po.get('po_id'),
        'po_number':                    po.get('po_number'),
        'ordered_quantity':             float(ordered_quantity) if ordered_quantity is not None else None,
        'invoiced_quantity_cumulative': float(invoiced_quantity),
        'received_quantity_cumulative': float(received_quantity),
        'remaining_to_invoice':         float(remaining_to_invoice) if remaining_to_invoice is not None else None,
        'remaining_to_receive':         float(remaining_to_receive) if remaining_to_receive is not None else None,
        'po_amount':                    float(po_amount) if po_amount is not None else None,
        'invoiced_amount_cumulative':   float(invoiced_amount),
        'remaining_amount':             float(remaining_amount) if remaining_amount is not None else None,
        'status':                       status,
        'received_status':              received_status,
        'unallocated_invoice_count':    unallocated_count,
    }


def compute_invoice_result(invoice, po_allocations, gr_count,
                            quantity_tolerance=QUANTITY_TOLERANCE, amount_tolerance=AMOUNT_TOLERANCE):
    """Invoice-level PASS / REVIEW_REQUIRED verdict (STEP 4A).

    invoice: {'document_id', 'quantity', 'total_amount'}.
    po_allocations: list of {'po_id', 'matched_quantity', 'matched_amount',
      'remaining_before_this_invoice_quantity', 'remaining_before_this_
      invoice_amount', 'vendor_match'} — one entry per PO relationship
      this invoice participates in. 'remaining_before_this_invoice_*' is
      the PO's remaining capacity EXCLUDING this invoice's own
      allocation (i.e. capacity available to it at allocation time) —
      the caller computes this since it requires seeing every OTHER
      invoice linked to the same PO, which this pure function doesn't
      have visibility into on its own.
    gr_count: number of GR relationships (direct or via a linked PO)
      supporting this invoice.

    Returns {'status', 'matched_po_count', 'matched_gr_count',
    'invoice_quantity', 'allocated_quantity', 'invoice_amount',
    'allocated_amount', 'issues': [...], 'warnings': [...]}."""
    issues = []
    warnings = []

    invoice_quantity = _dec(invoice.get('quantity'))
    invoice_amount = _dec(invoice.get('total_amount'))

    if not po_allocations:
        issues.append('No reliable PO relationship found')
        allocated_quantity = Decimal('0')
        allocated_amount = Decimal('0')
    else:
        allocated_quantity = _sum_dec(a.get('matched_quantity') for a in po_allocations if a.get('matched_quantity') is not None)
        allocated_amount = _sum_dec(a.get('matched_amount') for a in po_allocations if a.get('matched_amount') is not None)

        for alloc in po_allocations:
            if alloc.get('vendor_match') is False:
                issues.append(f"Vendor mismatch against PO {alloc.get('po_number') or alloc.get('po_id')}")

            remaining_before = alloc.get('remaining_before_this_invoice_quantity')
            matched_qty = alloc.get('matched_quantity')
            if remaining_before is not None and matched_qty is not None:
                if _dec(matched_qty) > _dec(remaining_before) + quantity_tolerance:
                    issues.append(
                        f"Invoice quantity exceeds remaining quantity on PO {alloc.get('po_number') or alloc.get('po_id')}")

            remaining_before_amt = alloc.get('remaining_before_this_invoice_amount')
            matched_amt = alloc.get('matched_amount')
            if remaining_before_amt is not None and matched_amt is not None:
                if _dec(matched_amt) > _dec(remaining_before_amt) + amount_tolerance:
                    issues.append(
                        f"Invoice amount exceeds remaining amount on PO {alloc.get('po_number') or alloc.get('po_id')}")

        unallocated = [a for a in po_allocations if a.get('matched_quantity') is None]
        if unallocated and len(po_allocations) > 1:
            warnings.append('Allocation across multiple POs is ambiguous and requires manual confirmation')

    if gr_count == 0:
        # Missing GR support is a warning, not a blocking issue on its
        # own — mirrors the legacy engine's PARTIAL (not FAIL) treatment
        # of a missing GR (routes/auditor.py::_build_comparison).
        warnings.append('No Goods Receipt support found for this invoice')

    if invoice_quantity is not None and invoice_quantity > 0 and allocated_quantity == 0 and po_allocations:
        warnings.append('Invoice quantity is not yet allocated to any PO (ambiguous split)')

    status = 'REVIEW_REQUIRED' if issues else 'PASS'

    return {
        'status':              status,
        'matched_po_count':    len(po_allocations),
        'matched_gr_count':    gr_count,
        'invoice_quantity':    float(invoice_quantity) if invoice_quantity is not None else None,
        'allocated_quantity':  float(allocated_quantity),
        'invoice_amount':      float(invoice_amount) if invoice_amount is not None else None,
        'allocated_amount':    float(allocated_amount),
        'issues':              issues,
        'warnings':            warnings,
    }
