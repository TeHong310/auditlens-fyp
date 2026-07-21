"""Manual, opt-in test of Claude Vision extraction — TEST MODE ONLY.

This is NOT part of the automated test suite and is NEVER run
automatically by anything else in this repo. It makes ONE REAL call to
the Anthropic API and will consume real API credits — only run it when
you deliberately want to evaluate Claude's extraction quality against an
actual document.

Does NOT touch the production upload pipeline, the Gemini flow, OCR, the
database, or the frontend — it only calls helpers/claude_extractor.py's
extract_with_claude_test() and prints the result.

Usage:
    python scripts/test_claude_extraction.py <path-to-invoice-file> [document_type]

    document_type: invoice | po | gr (default: invoice)

Example:
    python scripts/test_claude_extraction.py "C:\\path\\to\\coilcraft_invoice.pdf"

Repeated runs against the SAME file (by content hash) reuse a local
result cache (scripts/.claude_test_cache/) instead of calling the API
again — delete that directory, or edit the file, to force a fresh call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.gemini_extractor import prepare_gemini_image_payload
from helpers.claude_extractor import (
    extract_with_claude_test, compute_file_hash,
    get_cached_test_result, save_test_result_to_cache,
)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    file_path = sys.argv[1]
    document_type = sys.argv[2] if len(sys.argv) > 2 else 'invoice'

    if document_type not in ('invoice', 'po', 'gr'):
        print(f"Invalid document_type {document_type!r} — must be invoice, po, or gr")
        sys.exit(1)

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        sys.exit(1)

    with open(file_path, 'rb') as f:
        file_bytes = f.read()

    file_hash = compute_file_hash(file_bytes)
    cached = get_cached_test_result(file_hash, document_type)

    if cached is not None:
        print(f"CACHE HIT (hash={file_hash[:12]}..., document_type={document_type}) — Claude API NOT called")
        result = cached
    else:
        print("=" * 60)
        print("WARNING: this will make a REAL Anthropic API call and")
        print("consume real API credits.")
        print("=" * 60)

        image = prepare_gemini_image_payload(file_bytes, os.path.basename(file_path))
        result = extract_with_claude_test(image, document_type)

        if result is None:
            print("\nDEBUG CLAUDE RESULT: extraction failed (see errors above — "
                  "check ANTHROPIC_API_KEY is set and valid)")
            sys.exit(1)

        save_test_result_to_cache(file_hash, document_type, result)

    line_items = result.get('line_items') or []
    print()
    print("DEBUG CLAUDE RESULT:")
    print(f"  vendor_name      = {result.get('vendor_name')}")
    print(f"  invoice_number   = {result.get('invoice_number')}")
    print(f"  po_number        = {result.get('po_number')}")
    print(f"  total_amount     = {result.get('total_amount')}")
    print(f"  currency         = {result.get('currency')}")
    print(f"  line_items count = {len(line_items)}")
    for i, item in enumerate(line_items, start=1):
        print(f"    [{i}] description={item.get('description')!r} "
              f"part_number={item.get('part_number')!r} "
              f"quantity={item.get('quantity')!r} amount={item.get('amount')!r}")


if __name__ == '__main__':
    main()
