-- ============================================================================
-- project_locations_cloud  —  Street addresses + lat/lng for active projects.
-- ============================================================================
-- Lives in its own table so the projects_cloud sync (which wipes + reloads
-- from PROJECT LIST.xlsm every 15 min) doesn't blow away geocoded data.
--
-- Keyed by full_number (e.g. "2611-BAY") so it joins cleanly to both
-- projects_cloud and bid_setup pipeline tables.
--
-- Geocoding uses Nominatim (OpenStreetMap, free, no API key) on a nightly
-- cron — see fusion-cloud-cron/geocode-project-addresses.py.
--
-- Run once: https://supabase.com/dashboard/project/dltuvsdwrujjsmiotaxy/sql/new
-- ============================================================================

CREATE TABLE IF NOT EXISTS project_locations_cloud (
  full_number     text PRIMARY KEY,
  address         text NOT NULL,
  city            text,
  state           text DEFAULT 'CA',
  -- WGS-84 lat/lng. NULL until the geocoder runs.
  lat             numeric(9,6),
  lng             numeric(9,6),
  -- When was this row last (re-)geocoded? Used to skip already-resolved
  -- rows on subsequent runs and to re-resolve stale addresses on a TTL.
  geocoded_at     timestamptz,
  geocode_status  text,                  -- 'ok' | 'not_found' | 'rate_limited' | 'error'
  notes           text,                  -- free-form (e.g. "site has no street address; pinned to nearest intersection")
  set_by          text,                  -- app_users.id who entered/edited the address
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS plc_geocoded_idx ON project_locations_cloud (geocoded_at);
CREATE INDEX IF NOT EXISTS plc_pending_idx  ON project_locations_cloud (full_number) WHERE lat IS NULL;
