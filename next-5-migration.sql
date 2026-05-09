-- ============================================================================
-- "Next 5" migration — run once in Supabase SQL editor
-- ============================================================================

-- (1) Photos: column on time entries + storage bucket
ALTER TABLE field_time_entries_cloud
  ADD COLUMN IF NOT EXISTS photo_urls text[] DEFAULT '{}';

INSERT INTO storage.buckets (id, name, public)
VALUES ('field-photos', 'field-photos', true)
ON CONFLICT (id) DO NOTHING;

DROP POLICY IF EXISTS "field-photos public read" ON storage.objects;
CREATE POLICY "field-photos public read" ON storage.objects
  FOR SELECT TO anon, authenticated
  USING (bucket_id = 'field-photos');

-- (2) Setup Bid address capture: column on prebid rows
ALTER TABLE prebid_bids_cloud
  ADD COLUMN IF NOT EXISTS project_address text;

-- (3) Field Panel legal acknowledgment: app_users tracking columns
ALTER TABLE app_users
  ADD COLUMN IF NOT EXISTS tracking_acknowledged_at timestamptz,
  ADD COLUMN IF NOT EXISTS tracking_policy_version  text,
  ADD COLUMN IF NOT EXISTS gps_consent              boolean;

-- Anon role grants for the new tables/columns the field panel reads
GRANT SELECT ON field_time_entries_cloud TO anon;
