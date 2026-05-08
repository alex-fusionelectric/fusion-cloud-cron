-- Adds the columns setup-bid.js writes that previously didn't exist.
-- Without these the prebid_bids_cloud insert returns HTTP 400
-- "Could not find the '<col>' column of 'prebid_bids_cloud' in the schema
-- cache" and the bid setup silently fails (Dropbox folder + Gmail label
-- get created but nothing lands in the Setup Queue / watcher / email).
--
-- Run once: https://supabase.com/dashboard/project/dltuvsdwrujjsmiotaxy/sql/new

ALTER TABLE prebid_bids_cloud
  ADD COLUMN IF NOT EXISTS job_walk_at       timestamptz,
  ADD COLUMN IF NOT EXISTS job_walk_location text,
  ADD COLUMN IF NOT EXISTS opsplannum        text;
