-- side_jobs_cloud — individual side jobs (one row per actual job, not per
-- umbrella) parsed out of PROJECT LIST.xlsm's per-year tabs (2699B, 2699S,
-- 2599B, 2599S, 2499B, 2499S, etc.). The PROJECT LIST main tab only carries
-- the umbrella row per year+division; the actual job-level detail (job
-- status, contract amount, foreman, etc.) lives in those side-job tabs.
--
-- The sync strips inactive jobs (COMPLETE, PAID, NO BILLING) at write time
-- so this table only ever holds CURRENT side jobs — no client-side filter
-- needed in the field panel. When a job's status flips to COMPLETE in the
-- xlsm, it disappears from this table on the next sync.
--
-- Run once: https://supabase.com/dashboard/project/dltuvsdwrujjsmiotaxy/sql/new

CREATE TABLE IF NOT EXISTS side_jobs_cloud (
  job_number        text PRIMARY KEY,        -- e.g. "2699-104"
  parent_tab        text NOT NULL,            -- e.g. "2699B" — the xlsm tab
  year              integer NOT NULL,         -- 2026
  division          text NOT NULL,            -- BAY | SAC
  job_suffix        text,                     -- "104" portion of "2699-104"
  project_name      text,
  est_number        text,
  customer          text,
  contact           text,
  project_manager   text,
  foreman           text,
  job_status        text,                     -- CURRENT only after sync filter
  bonus_status      text,
  work_description  text,
  contract_amount   numeric,
  award_date        date,
  payload           jsonb,
  generated_at      timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sjc_div_year_idx ON side_jobs_cloud (division, year DESC);
CREATE INDEX IF NOT EXISTS sjc_parent_idx   ON side_jobs_cloud (parent_tab);
