ALTER TABLE authenticity_checks
  ADD COLUMN IF NOT EXISTS signal_boxes JSONB;
