-- 3-way audit comparison fields (Field Comparison table redesign):
-- PO Ref, Item/Description, Quantity. Regex-extracted, no Gemini call.
--
-- NOTE: this migration also runs automatically at app startup (see
-- app.py's _ensure_3way_comparison_columns()) since this repo has no
-- migration runner and Render never gets a .sql file run against it
-- otherwise. Kept here for local dev / documentation parity with the
-- rest of migrations/.
ALTER TABLE extracted_fields
  ADD COLUMN IF NOT EXISTS po_reference VARCHAR(100),
  ADD COLUMN IF NOT EXISTS item_description TEXT,
  ADD COLUMN IF NOT EXISTS quantity NUMERIC;

ALTER TABLE purchase_orders
  ADD COLUMN IF NOT EXISTS item_description TEXT,
  ADD COLUMN IF NOT EXISTS quantity NUMERIC;

ALTER TABLE goods_receipts
  ADD COLUMN IF NOT EXISTS po_reference VARCHAR(100),
  ADD COLUMN IF NOT EXISTS item_description TEXT,
  ADD COLUMN IF NOT EXISTS quantity NUMERIC;
