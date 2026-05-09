-- ============================================================================
-- Edits + two-tier approval migration. Run once in Supabase SQL editor.
-- ============================================================================

-- (1) Audit table for edits to time entries. Every change to a pending
--     entry writes one row here so payroll has a paper trail of what
--     was modified and why.
CREATE TABLE IF NOT EXISTS field_time_entry_edits_cloud (
  id            text PRIMARY KEY,
  entry_id      text NOT NULL REFERENCES field_time_entries_cloud(id) ON DELETE CASCADE,
  edited_by     text REFERENCES app_users(id) ON DELETE SET NULL,
  edited_at     timestamptz NOT NULL DEFAULT now(),
  field_name    text NOT NULL,
  old_value     text,
  new_value     text,
  reason        text
);
CREATE INDEX IF NOT EXISTS fteec_entry_idx  ON field_time_entry_edits_cloud (entry_id);
CREATE INDEX IF NOT EXISTS fteec_editor_idx ON field_time_entry_edits_cloud (edited_by, edited_at DESC);

-- (2) Two-tier approval. Foreman approves their crew first; PM approves
--     for payroll. Existing approved_at / approved_by become the
--     PM-tier (final, ready-for-payroll). New columns track foreman tier.
ALTER TABLE field_time_entries_cloud
  ADD COLUMN IF NOT EXISTS foreman_approved_at timestamptz,
  ADD COLUMN IF NOT EXISTS foreman_approved_by text REFERENCES app_users(id) ON DELETE SET NULL;

-- Anon SELECT on the new audit table so the field panel can show
-- "edited X times" hints if we want them later.
GRANT SELECT ON field_time_entry_edits_cloud TO anon;
