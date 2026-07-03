"""One-off backfill: (re-)run anomaly detection for a document that
already exists in the DB. Needed when a document never went through
POST /documents/upload (the only place run_anomaly_detection() is
hooked in) — e.g. seeded directly via SQL — or when its vendor's
baseline has grown since it was originally uploaded.

Usage:
    python scripts/backfill_anomaly_detection.py <document_id>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_db_connection
from helpers.anomaly_detector import run_anomaly_detection


def backfill(document_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM anomalies WHERE invoice_document_id = %s', (document_id,))
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"Deleted {deleted_count} existing anomalies for document_id={document_id}")

    created_ids = run_anomaly_detection(document_id)

    if not created_ids:
        print(f"Created 0 anomalies for document_id={document_id}")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''SELECT anomaly_id, anomaly_type, severity FROM anomalies
           WHERE anomaly_id = ANY(%s) ORDER BY anomaly_id''',
        (created_ids,)
    )
    rows = cursor.fetchall()
    conn.close()

    print(f"Created {len(rows)} anomal{'y' if len(rows) == 1 else 'ies'}:")
    for anomaly_id, anomaly_type, severity in rows:
        print(f"  anomaly_id={anomaly_id} type={anomaly_type} severity={severity}")


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: python scripts/backfill_anomaly_detection.py <document_id>")
        sys.exit(1)
    try:
        doc_id = int(sys.argv[1])
    except ValueError:
        print(f"document_id must be an integer, got: {sys.argv[1]!r}")
        sys.exit(1)
    backfill(doc_id)
