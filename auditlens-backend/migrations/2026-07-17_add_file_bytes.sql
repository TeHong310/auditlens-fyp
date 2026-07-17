-- Render's free tier filesystem is ephemeral (wiped on every redeploy/
-- restart), so uploaded files can no longer live only on local disk.
-- Store the original file bytes in Postgres instead, per table (matching
-- each table's existing file_path/file_name columns).
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_file_bytes_columns()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. This file is kept for local dev / documentation parity
-- with the rest of migrations/.
ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS file_bytes BYTEA,
  ADD COLUMN IF NOT EXISTS file_mime VARCHAR(100);

ALTER TABLE purchase_orders
  ADD COLUMN IF NOT EXISTS file_bytes BYTEA,
  ADD COLUMN IF NOT EXISTS file_mime VARCHAR(100);

ALTER TABLE goods_receipts
  ADD COLUMN IF NOT EXISTS file_bytes BYTEA,
  ADD COLUMN IF NOT EXISTS file_mime VARCHAR(100);
