"""Enterprise V3 Phase 2 (STEP 11) — safe, manual backfill for the
deterministic relationship builder (helpers/relationship_builder.py).

Never run automatically (no caller in app.py). Always dry-run unless
--apply is explicitly passed. No AI calls — the builder is deterministic
field comparison only. Never overwrites a manually-created relationship
(relationship_source='manual') — see helpers/document_relationships.py::
upsert_relationship().

Usage:
    python scripts/backfill_document_relationships.py --dry-run
    python scripts/backfill_document_relationships.py --dry-run --limit 20
    python scripts/backfill_document_relationships.py --document-id 123 --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.relationship_builder import build_relationships_for_invoice, build_relationships_for_all_documents


def _print_summary(summary):
    if not summary.get('invoice_found', True):
        print(f"  document_id={summary['document_id']}: no extracted_fields row (not an invoice, or not yet extracted) — skipped")
        return

    candidates = summary['candidates']
    print(f"  document_id={summary['document_id']}: "
          f"{summary['po_candidates_considered']} PO candidate(s), {summary['gr_candidates_considered']} GR candidate(s)")
    if not candidates:
        print('    (no candidate scored high enough to link)')
    for c in candidates:
        alloc = f"qty={c['matched_quantity']} amt={c['matched_amount']}" if c.get('matched_quantity') is not None else 'unallocated (ambiguous)'
        print(f"    [{c['action']:>14}] {c['relationship_type']:<12} "
              f"{c['parent_type']}:{c['parent_id']} -> {c['child_type']}:{c['child_id']}  "
              f"confidence={c['confidence_score']:.1f}  {alloc}")
        if c.get('error'):
            print(f"                     error: {c['error']}")


def main():
    parser = argparse.ArgumentParser(description='Backfill document_relationships via the deterministic builder.')
    parser.add_argument('--document-id', type=int, default=None, help='Process a single invoice document_id.')
    parser.add_argument('--limit', type=int, default=None, help='Cap how many invoices to process (batch mode only).')
    parser.add_argument('--apply', action='store_true', help='Actually write relationships. Without this, always dry-run.')
    parser.add_argument('--dry-run', action='store_true', help='Explicit dry-run (default behavior; accepted for clarity).')
    args = parser.parse_args()

    dry_run = not args.apply

    if args.document_id is not None:
        print(f"{'DRY RUN' if dry_run else 'APPLYING'} - document_id={args.document_id}")
        summary = build_relationships_for_invoice(args.document_id, dry_run=dry_run)
        _print_summary(summary)
        return

    print(f"{'DRY RUN' if dry_run else 'APPLYING'} - batch"
          + (f" (limit={args.limit})" if args.limit else " (all invoices)"))
    result = build_relationships_for_all_documents(dry_run=dry_run, limit=args.limit)
    for summary in result['summaries']:
        _print_summary(summary)
    print(f"\n{result['invoices_processed']} invoice(s) processed."
          + (' Re-run with --apply to write these relationships.' if dry_run else ''))


if __name__ == '__main__':
    main()
