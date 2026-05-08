-- ============================================================================
-- Field Panel — Phase 1 schema
-- ============================================================================
-- 1. app_users: account table the existing auth-login.js / auth-signup.js
--    Netlify functions already query. They reference this table but the
--    table was never created — so login currently 404s on the table.
-- 2. field_time_entries_cloud: clock-in/out records for field crew.
--
-- Run once: https://supabase.com/dashboard/project/dltuvsdwrujjsmiotaxy/sql/new
-- ============================================================================

-- ── app_users ───────────────────────────────────────────────────────────────
-- Matches the schema auth-login.js + auth-signup.js expect:
--   id, company_id, username, display_name, role, password_salt, password_hash
-- Roles relevant to the field panel: 'field' | 'foreman' | 'pm' | 'admin'
-- (role is text so other portals can add their own values without migration.)
CREATE TABLE IF NOT EXISTS app_users (
  id              text PRIMARY KEY,
  company_id      text NOT NULL,
  username        text NOT NULL,
  display_name    text,
  role            text NOT NULL DEFAULT 'field',
  password_salt   text NOT NULL,
  password_hash   text NOT NULL,
  -- Field-panel specifics:
  employee_id     text,                       -- FK to BUDGET LIST EMPLOYEES sheet, NULL for office staff
  foreman_id      text REFERENCES app_users(id) ON DELETE SET NULL,
  email           text,
  phone           text,
  active          boolean NOT NULL DEFAULT true,
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now(),
  CONSTRAINT app_users_company_username_uniq UNIQUE (company_id, username)
);

CREATE INDEX IF NOT EXISTS app_users_company_role_idx
  ON app_users (company_id, role) WHERE active = true;
CREATE INDEX IF NOT EXISTS app_users_foreman_idx
  ON app_users (foreman_id) WHERE foreman_id IS NOT NULL;

-- ── field_time_entries_cloud ────────────────────────────────────────────────
-- One row per clock-in. time_out = NULL means user is still on the clock.
-- date is the workday in local time (Pacific) — derived at clock-in so a
-- midnight job stays attributed to the day it started.
CREATE TABLE IF NOT EXISTS field_time_entries_cloud (
  id              text PRIMARY KEY,             -- "fte-<user_id>-<epoch_ms>"
  user_id         text NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  username        text NOT NULL,                -- denormalized for fast list views
  display_name    text,                         -- denormalized
  date            date NOT NULL,                -- workday (Pacific)
  project_est     text,                         -- "26-248", or NULL for non-billable
  project_name    text,                         -- denormalized at clock-in time
  time_in         timestamptz NOT NULL,
  time_out        timestamptz,                  -- NULL = currently clocked in
  lunch_minutes   integer NOT NULL DEFAULT 0,
  notes           text,
  gps_in_lat      numeric(9,6),
  gps_in_lng      numeric(9,6),
  gps_out_lat     numeric(9,6),
  gps_out_lng     numeric(9,6),
  -- Approval workflow (foreman/PM signoff before payroll):
  approved_at     timestamptz,
  approved_by     text REFERENCES app_users(id) ON DELETE SET NULL,
  payroll_exported_at timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fte_user_date_idx
  ON field_time_entries_cloud (user_id, date DESC);
CREATE INDEX IF NOT EXISTS fte_open_idx
  ON field_time_entries_cloud (user_id) WHERE time_out IS NULL;
CREATE INDEX IF NOT EXISTS fte_project_date_idx
  ON field_time_entries_cloud (project_est, date DESC) WHERE project_est IS NOT NULL;

-- Prevent two open clocks for the same user (one user can't be on two
-- jobs simultaneously). Partial unique index — only enforced where
-- time_out is NULL.
CREATE UNIQUE INDEX IF NOT EXISTS fte_one_open_per_user_idx
  ON field_time_entries_cloud (user_id) WHERE time_out IS NULL;

-- ── RLS sketch (commented out for Phase 1) ──────────────────────────────────
-- For now, all server access goes through Netlify Functions using the
-- service-role key, and the functions enforce per-user filters in code.
-- Enable RLS in Phase 2 when we wire up Supabase Auth JWTs end-to-end.
--
-- ALTER TABLE field_time_entries_cloud ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY field_own_entries ON field_time_entries_cloud
--   FOR ALL USING (user_id = current_setting('request.jwt.claim.sub', true));
