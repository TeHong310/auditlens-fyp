-- Gemini is now the PRIMARY field extractor and detects the currency of a
-- document's total (RM/MYR/USD/...) so multi-currency invoices/POs are
-- never silently mixed. purchase_orders/goods_receipts already had a
-- currency column; extracted_fields (invoice) didn't.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_invoice_currency_column()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
ALTER TABLE extracted_fields
  ADD COLUMN IF NOT EXISTS currency VARCHAR(10);
