"""Runs every extraction regression test in this directory and reports a
single pass/fail summary — invoice, PO, GR.

Usage:
    python tests/extraction/run_all.py
Exits 0 if every suite passes, 1 if any fail.
"""
import subprocess
import sys
import os

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEWS_DIR = os.path.join(os.path.dirname(THIS_DIR), 'reviews')
SUITES = ['test_invoice.py', 'test_po.py', 'test_gr.py', 'test_currency.py', 'test_ap_upgrade.py',
          'test_entity_and_line_items.py', 'test_ai_router.py', 'test_authenticity_ai.py',
          'test_authenticity_siblings.py', 'test_authenticity_scoring.py']
REVIEWS_SUITES = ['test_send_back_validation.py', 'test_send_back_routes.py']

if __name__ == '__main__':
    results = {}
    for suite in SUITES:
        print(f'\n{"=" * 60}\n{suite}\n{"=" * 60}')
        proc = subprocess.run([sys.executable, os.path.join(THIS_DIR, suite)])
        results[suite] = proc.returncode
    for suite in REVIEWS_SUITES:
        print(f'\n{"=" * 60}\n{suite}\n{"=" * 60}')
        proc = subprocess.run([sys.executable, os.path.join(REVIEWS_DIR, suite)])
        results[suite] = proc.returncode

    print(f'\n{"=" * 60}\nSUMMARY\n{"=" * 60}')
    failed = [s for s, code in results.items() if code != 0]
    for suite, code in results.items():
        print(f'  {"PASS" if code == 0 else "FAIL"}  {suite}')

    sys.exit(1 if failed else 0)
