"""One-shot agency-name normalization for prequal_approvals_cloud.

After the 2026-05-21 backfill the table had visible duplicates that
the frontend dedup-by-name couldn't merge (case-insensitive exact
match only):

  Berryessa                            → Berryessa Union School District
  Brentwood USD                        → Brentwood Unified School District
  CLPCCD                               → Chabot-Las Positas Community College District
  Menlo Park                           → Menlo Park City School District
  San Mateo Foster City School Distr.  → San Mateo-Foster City School District

This script PATCHes the source rows to the canonical form. Idempotent —
running it again does nothing once names are aligned.

Future cron ticks call _canonicalize_agency_name() in
parse-prequal-emails.py so new rows land canonical from the start.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

SUPABASE_URL = "https://dltuvsdwrujjsmiotaxy.supabase.co"
TABLE = "prequal_approvals_cloud"

# Canonical name on the right; left-side variants get rewritten.
# Keep keys in the EXACT form seen in the table (case + punctuation
# matter for the PATCH filter).
RENAMES = {
    "Berryessa":                            "Berryessa Union School District",
    "Brentwood USD":                        "Brentwood Unified School District",
    "CLPCCD":                               "Chabot-Las Positas Community College District",
    "Menlo Park":                           "Menlo Park City School District",
    "San Mateo Foster City School District": "San Mateo-Foster City School District",
}


def _patch(old_name, new_name, key):
    qs = f"agency_name=eq.{urllib.parse.quote(old_name)}"
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}?{qs}"
    body = json.dumps({"agency_name": new_name}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY required.")

    total_renamed = 0
    for old, new in RENAMES.items():
        try:
            rows = _patch(old, new, key)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] PATCH failed for {old!r}: {e}")
            continue
        n = len(rows or [])
        total_renamed += n
        print(f"  [{n:>2} rows] {old!r}  ->  {new!r}")

    print(f"\nDone. Renamed {total_renamed} row(s) across {len(RENAMES)} mapping(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
