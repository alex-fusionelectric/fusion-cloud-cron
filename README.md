# fusion-cloud-cron

Cloud-side xlsm-to-Supabase sync. Runs on GitHub Actions on a schedule,
downloads each xlsm from a SharePoint share link, parses it, and writes
the rows to Supabase. Replaces the local `Sync-PortalData.ps1` chain
Alex's PC runs today, so the data stays fresh whether his PC is on or not.

Two workflows today:
- **BID LIST** (`sync-bid-list.yml`) — every 5 min, writes 1853 rows
  to `public.bids_cloud`.
- **BUDGET LIST** (`sync-budget-list.yml`) — every 30 min, writes 46
  project rows + 1 aggregates meta row to `public.budgets_cloud`.

## What's here

- `cloud-sync-bid-list.py` -- entry point. Downloads the xlsm,
  parses with the local pipeline's logic (via `sync_bid_list_compat`),
  writes to Supabase.
- `sync-bid-list.py` -- copy of the canonical local parser at
  `Fusion Software/fusion-pm panel/scripts/sync-bid-list.py`. If the
  local parser changes, copy the new version into this repo.
- `sync_bid_list_compat.py` -- importlib shim that lets
  `cloud-sync-bid-list.py` reuse the parser despite the hyphen in
  the filename.
- `requirements.txt` -- pandas + openpyxl, the parse-time deps.
- `.github/workflows/sync-bid-list.yml` -- the cron + run definition.

## One-time setup

1. **Create the Supabase table** (once, in the SQL editor). See
   `fusion-pm panel/backend/supabase/bids-cloud-table.sql` in the
   parent project.

2. **Push this directory to a new private GitHub repo.**

   ```
   cd "C:\Users\AlexToler\Documents\Fusion Software\fusion-cloud-cron"
   git init
   git add .
   git commit -m "Initial cloud-cron scaffold"
   git branch -M main
   git remote add origin git@github.com:<your-username>/fusion-cloud-cron.git
   git push -u origin main
   ```

3. **Add two repo secrets** (Settings -> Secrets and variables -> Actions):

   - `BID_LIST_URL` -- the SharePoint share URL for `BID LIST.xlsm`
     (the `https://...sharepoint.com/:x:/g/personal/.../FILE?e=...` form,
     with read-only "Anyone with the link" sharing).
   - `SUPABASE_SERVICE_KEY` -- service_role key from Supabase project
     settings. NOT the anon key.

4. **Trigger a first run** via the Actions tab -> Sync BID LIST to
   Supabase -> Run workflow. Confirm the run goes green; query
   `select count(*) from bids_cloud` in Supabase, expect ~1853.

After step 4 the cron runs every 5 minutes automatically.

## What this does NOT touch

- `public.dave_bids` -- still written by `Push-ToSupabase.ps1` on
  Alex's PC, still read by `/bid-panel/`. We only retire that pipeline
  after the front-end is pointed at `bids_cloud` (separate change).
- `public.bids` -- per-company table for the PM Panel. Different code
  path, irrelevant here.
- Any portal page -- nothing reads from `bids_cloud` until we cut over
  the Bid Panel + Bay Bid List in a follow-up change.

## Cutting over (later)

Once `bids_cloud` has been populated for a few days and a row-level
diff vs the local pipeline stays clean, point the front-end at it:

- `fusion-bid-list/site/bid-panel/index.html` -- swap
  `dave_bids` -> `bids_cloud` in the Supabase fetch URL.
- `fusion-bay-bid-list/src/index.html` -- replace the static
  `bids-data.js` import with a Supabase fetch against `bids_cloud`.

Then disable the local `Push-ToSupabase` step inside
`fusion-bid-list/AutoUpdate-Task.ps1`.
