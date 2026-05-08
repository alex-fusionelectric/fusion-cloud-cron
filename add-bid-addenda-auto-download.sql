-- Adds tracking columns to bid_addenda_cloud so the watcher's
-- monitor-active-sbx-bids routine can record which addenda it
-- auto-pulled and when. The frontend reads these to show "✓ Auto-
-- downloaded" badges in the addenda list, so Alex can tell which
-- arrived via the cron vs. were pulled manually.
--
-- Run once: https://supabase.com/dashboard/project/dltuvsdwrujjsmiotaxy/sql/new

ALTER TABLE bid_addenda_cloud
  ADD COLUMN IF NOT EXISTS auto_downloaded_at  timestamptz,
  ADD COLUMN IF NOT EXISTS auto_downloaded_by  text;  -- 'watcher' | 'manual' | NULL
